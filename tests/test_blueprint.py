"""Static checks and end-to-end scenarios for the blueprint.

Run:  python -m pytest tests   (or)   python tests/test_blueprint.py
Needs: PyYAML, Jinja2 (and pytest if you use it).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ha_sim import BLUEPRINT, TZ, Input, Sim, flatten_triggers, input_definitions, load_blueprint  # noqa: E402

START = datetime(2026, 10, 18, 20, 0, 0, tzinfo=TZ)
MIN = 60

REMOTE = "aaaabbbbccccdddd"


def remote(subtype, kind="remote_button_short_press"):
    return [{"trigger": "device", "domain": "zha", "device_id": REMOTE, "type": kind, "subtype": subtype}]


BASE_INPUTS = {
    "occupancy_sensors": ["binary_sensor.motion_a", "binary_sensor.motion_b"],
    "lights": ["light.ceiling"],
    "mode_helper": "input_select.room_mode",
    "hold_helper": "input_datetime.room_hold",
    "button_on": remote("turn_on"),
    "button_off": remote("turn_off"),
    "button_toggle": remote("toggle") + remote("toggle_2"),  # two triggers in one group
    "button_auto": remote("turn_on", "remote_button_long_press"),
    "button_pause": remote("turn_off", "remote_button_long_press"),
}

BASE_STATES = {
    "binary_sensor.motion_a": "off",
    "binary_sensor.motion_b": "off",
    "light.ceiling": ("off", {"brightness": None}),
    "light.nightlight": ("off", {}),
    "input_select.room_mode": ("Auto", {"options": ["Auto", "Manual on", "Manual off", "Paused"]}),
    "input_datetime.room_hold": ("2026-10-18 00:00:00", {"has_date": True, "has_time": True,
                                                        "timestamp": START.timestamp() - 3600}),
    "input_boolean.night_mode": "off",
    "input_boolean.movie_mode": "off",
    "media_player.tv": "off",
    "sensor.lux": "5",
}

SCENES = {
    "scene.ambient": {"light.ceiling": ("on", {"brightness": 13})},
    "scene.reading": {"light.ceiling": ("on", {"brightness": 200})},
}


def make(inputs=None, states=None, settle=True, **kw):
    sim = Sim(BASE_INPUTS | (inputs or {}), start=START, states=BASE_STATES | (states or {}),
              scenes=SCENES, **kw)
    if settle:
        sim.advance(10 * MIN)  # let startup last_changed age past the vacancy delay
    sim.calls.clear()
    return sim


def mode(sim):
    return sim.state("input_select.room_mode")


def motion(sim, on=True, sensor="binary_sensor.motion_a"):
    sim.device(sensor, "on" if on else "off")


# ============================================================ static checks
def test_inputs_and_references():
    text = BLUEPRINT.read_text(encoding="utf-8")
    bp = load_blueprint()
    defs = input_definitions(bp)
    used = set(re.findall(r"!input\s+(\w+)", text))
    assert used == set(defs), (set(defs) - used, used - set(defs))
    required = {k for k, d in defs.items() if "default" not in d}
    assert required == {"occupancy_sensors", "lights", "mode_helper"}, required
    assert bp["blueprint"]["homeassistant"]["min_version"] == "2024.10.0"
    assert bp["mode"] == "restart"
    for name, d in defs.items():
        assert d.get("name"), f"{name} has no name"


def test_fixed_trigger_count_matches():
    bp = load_blueprint()
    fixed = [t for t in bp["triggers"] if set(t) != {"triggers"}]
    wrappers = [t for t in bp["triggers"] if set(t) == {"triggers"}]
    assert bp["triggers"][: len(fixed)] == fixed, "button wrappers must come after all fixed triggers"
    assert bp["variables"]["fixed_trigger_count"] == len(fixed)
    assert [w["triggers"].name for w in wrappers] == [g[0] for g in bp["variables"]["button_groups"]]
    assert [g[1].name for g in bp["variables"]["button_groups"]] == [g[0] for g in bp["variables"]["button_groups"]]


def test_profile_selection_is_identical_in_trigger_and_variables():
    bp = load_blueprint()
    for_tpl = bp["triggers"][1]["for"]["seconds"]
    profile_tpl = bp["variables"]["profile"]
    loop = profile_tpl[: profile_tpl.rindex("{{ ns.id }}")]
    assert for_tpl.startswith(loop), "keep the profile loop of `for:` and `profile` identical"
    assert bp["variables"]["delay"] == for_tpl


def test_no_bare_boolean_template_outputs():
    # HA keeps a bare `false` as the string "false", which is truthy.
    text = BLUEPRINT.read_text(encoding="utf-8")
    assert not re.search(r"^\s+(true|false)\s*$", text, re.M)


# ================================================================ scenarios
def test_motion_on_and_vacancy_off_with_two_sensors():
    sim = make({"default_brightness": 80})
    motion(sim)
    assert sim.state("light.ceiling") == "on"
    assert sim.attr("light.ceiling", "brightness") == 204
    assert mode(sim) == "Auto", "own light change must not count as manual"
    motion(sim, sensor="binary_sensor.motion_b")
    motion(sim, False)
    sim.advance(4 * MIN)
    motion(sim, False, "binary_sensor.motion_b")
    sim.advance(299)
    assert sim.state("light.ceiling") == "on", "B cleared < 300 s ago"
    sim.advance(2)
    assert sim.state("light.ceiling") == "off"
    assert mode(sim) == "Auto"


def test_lux_and_sun_conditions():
    sim = make({"lux_sensor": "sensor.lux", "lux_threshold": 50}, {"sensor.lux": "400"})
    motion(sim)
    assert sim.state("light.ceiling") == "off", "too bright"
    sim = make({"lux_sensor": "sensor.lux", "missing_lux_policy": "block"}, {"sensor.lux": "unavailable"})
    motion(sim)
    assert sim.state("light.ceiling") == "off"
    sim = make({"lux_sensor": "sensor.lux"}, {"sensor.lux": "unavailable"})
    motion(sim)
    assert sim.state("light.ceiling") == "on", "missing lux allowed by default"
    sim = make({"sun_mode": "below", "sun_elevation": 3}, sun_elevation=20)
    motion(sim)
    assert sim.state("light.ceiling") == "off"
    sim = make({"sun_mode": "below", "sun_elevation": 3}, sun_elevation=-2)
    motion(sim)
    assert sim.state("light.ceiling") == "on"


def test_dashboard_off_while_occupied_holds_until_empty_and_hold_expired():
    sim = make()
    motion(sim)
    sim.user("light.ceiling", "off")
    assert mode(sim) == "Manual off"
    hold_until = sim.attr("input_datetime.room_hold", "timestamp")
    assert abs(hold_until - (sim.now.timestamp() + 30 * MIN)) < 1
    motion(sim, False)
    motion(sim)  # still ignored
    assert sim.state("light.ceiling") == "off"
    motion(sim, False)
    sim.advance(10 * MIN)
    assert mode(sim) == "Manual off", "room empty but hold not expired"
    sim.advance(21 * MIN)
    assert mode(sim) == "Auto"
    assert sim.state("light.ceiling") == "off"
    motion(sim)
    assert sim.state("light.ceiling") == "on"


def test_manual_on_survives_gaps_then_vacancy_policy():
    sim = make({"return_policy": "vacancy"})
    sim.user("light.ceiling", "on", {"brightness": 255})
    assert mode(sim) == "Manual on"
    motion(sim)
    motion(sim, False)
    sim.advance(200)
    motion(sim)
    motion(sim, False)
    sim.advance(299)
    assert sim.state("light.ceiling") == "on"
    sim.advance(2)
    assert sim.state("light.ceiling") == "off"
    assert mode(sim) == "Auto"


def test_hand_switched_light_in_empty_room_is_not_raced_off():
    sim = make()
    sim.user("light.ceiling", "on")
    sim.advance(5)
    assert (sim.state("light.ceiling"), mode(sim)) == ("on", "Manual on")


def test_light_left_on_by_another_automation_goes_off_after_delay():
    from ha_sim import Context
    sim = make()
    sim.set("light.ceiling", "on", context=Context(parent_id="other_automation"), keep_attrs=True)
    assert mode(sim) == "Auto"
    sim.advance(4 * MIN)
    assert sim.state("light.ceiling") == "on"
    sim.advance(3 * MIN)
    assert sim.state("light.ceiling") == "off"


def test_dimming_in_ui_starts_manual_on():
    sim = make()
    motion(sim)
    sim.user("light.ceiling", "on", {"brightness": 40})
    assert mode(sim) == "Manual on"


def test_self_caused_changes_are_ignored_even_with_unattributed_detection():
    sim = make({"detect_unattributed_changes": True, "warn_seconds": 20})
    motion(sim)
    assert mode(sim) == "Auto"
    motion(sim, False)
    sim.advance(400)
    assert sim.state("light.ceiling") == "off"
    assert mode(sim) == "Auto", [r for r in sim.runs]
    # Restart-path switch-off (watchdog, no parent context) is also ours.
    motion(sim)
    sim.restart_ha()
    sim.device("binary_sensor.motion_a", "off")
    sim.timers.clear()  # pretend the restart also lost this `for:`
    sim.advance(10 * MIN)
    assert sim.state("light.ceiling") == "off"
    assert mode(sim) == "Auto"


def test_unattributed_device_change_detection():
    sim = make()
    sim.device("light.ceiling", "on")
    assert mode(sim) == "Auto", "disabled by default"
    sim = make({"detect_unattributed_changes": True})
    sim.device("light.ceiling", "on")
    assert mode(sim) == "Manual on"
    sim.device("light.ceiling", "on", {"brightness": 10})  # attribute-only report
    assert mode(sim) == "Manual on"


def test_buttons():
    sim = make({"default_brightness": 60})
    sim.press("button_on")
    assert (sim.state("light.ceiling"), mode(sim)) == ("on", "Manual on")
    sim.press("button_toggle")
    assert (sim.state("light.ceiling"), mode(sim)) == ("off", "Manual off")
    sim.press("button_toggle")
    assert (sim.state("light.ceiling"), mode(sim)) == ("on", "Manual on")
    sim.press("button_pause")
    assert mode(sim) == "Paused"
    sim.press("button_off")  # inside paused: only switches the light
    assert (sim.state("light.ceiling"), mode(sim)) == ("off", "Paused")
    motion(sim)
    assert sim.state("light.ceiling") == "off", "paused ignores motion"
    sim.press("button_auto")
    assert mode(sim) == "Auto"
    assert sim.state("light.ceiling") == "on", "auto applied immediately while occupied"


def test_second_trigger_in_a_button_group_is_recognised():
    sim = make()
    groups = [t for t in sim.config["triggers"] if set(t) == {"triggers"}]
    fixed = len([t for t in sim.config["triggers"] if set(t) != {"triggers"}])
    idx = fixed + len(groups[0]["triggers"]) + len(groups[1]["triggers"]) + 1  # 2nd toggle trigger
    sim._fire(idx, {"platform": "device"}, None)
    assert sim.runs[-1]["event"] == "button_toggle"
    assert sim.state("light.ceiling") == "on"


def test_pause_can_turn_lights_off():
    sim = make({"pause_action": "off"})
    motion(sim)
    sim.press("button_pause")
    assert (sim.state("light.ceiling"), mode(sim)) == ("off", "Paused")


def test_dashboard_mode_changes():
    sim = make()
    sim.user("input_select.room_mode", "Manual on")
    assert sim.state("light.ceiling") == "on"
    sim.user("input_select.room_mode", "Manual off")
    assert sim.state("light.ceiling") == "off"
    motion(sim)
    assert sim.state("light.ceiling") == "off"
    sim.user("input_select.room_mode", "Auto")
    assert sim.state("light.ceiling") == "on", "occupied → auto switches on"


def test_dim_warning_then_off_and_motion_restores():
    sim = make({"warn_seconds": 30, "warn_brightness": 20, "default_brightness": 90})
    motion(sim)
    motion(sim, False)
    sim.advance(301)
    assert sim.state("light.ceiling") == "on"
    assert sim.attr("light.ceiling", "brightness") == 51
    motion(sim)  # someone waves
    assert sim.attr("light.ceiling", "brightness") == 230
    motion(sim, False)
    sim.advance(300 + 31)
    assert sim.state("light.ceiling") == "off"
    assert mode(sim) == "Auto"


def test_idle_scene_and_wake_up():
    sim = make({"default_idle_scene": "scene.ambient", "default_brightness": 100})
    motion(sim)
    motion(sim, False)
    sim.advance(301)
    assert sim.attr("light.ceiling", "brightness") == 13
    motion(sim)
    assert sim.attr("light.ceiling", "brightness") == 255
    assert mode(sim) == "Auto"


def test_night_profile_by_time_with_own_lights_and_delay():
    sim = make({"default_brightness": 100,
                "p2_from": "23:00:00", "p2_until": "06:00:00", "p2_lights": ["light.nightlight"],
                "p2_brightness": 5, "p2_vacancy_delay": 60})
    sim.advance(160 * MIN)  # 22:50 (after settling until 20:10)
    motion(sim)
    assert sim.state("light.ceiling") == "on"
    sim.advance(11 * MIN)  # crosses 23:00 while occupied → profile refresh
    assert sim.state("light.ceiling") == "off"
    assert sim.state("light.nightlight") == "on"
    assert sim.attr("light.nightlight", "brightness") == 13
    motion(sim, False)
    sim.advance(61)
    assert sim.state("light.nightlight") == "off", "night vacancy delay is 60 s"
    assert mode(sim) == "Auto"


def test_profile_by_entity_and_priority():
    sim = make({"default_brightness": 100,
                "p1_from": "18:00:00", "p1_until": "23:59:00", "p1_brightness": 50,
                "p2_entities": ["input_boolean.night_mode"], "p2_brightness": 10, "p2_auto_on": True})
    motion(sim)
    assert sim.attr("light.ceiling", "brightness") == 128, "profile 1 by time"
    sim.user("input_boolean.night_mode", "on")
    assert sim.attr("light.ceiling", "brightness") == 26, "profile 2 wins and refreshes"
    assert mode(sim) == "Auto"


def test_unused_profile_windows_do_not_reapply_at_midnight():
    sim = make({"default_brightness": 70})
    sim.advance(3 * 60 * MIN + 45 * MIN)  # 23:55
    motion(sim)
    sim.user("light.ceiling", "on", {"brightness": 10})
    sim.user("input_select.room_mode", "Auto")  # keep the dimmed light, back in auto
    sim.advance(10 * MIN)  # crosses 00:00 while occupied
    assert sim.attr("light.ceiling", "brightness") == 10
    assert not [r for r in sim.runs if r["event"] == "profile_changed"]


def test_profile_without_auto_on_is_vacancy_only():
    sim = make({"default_auto_on": False})
    motion(sim)
    assert sim.state("light.ceiling") == "off"
    sim.press("button_on")
    sim.press("button_auto")  # back to auto, light stays on while occupied
    assert (sim.state("light.ceiling"), mode(sim)) == ("on", "Auto")
    motion(sim, False)
    sim.advance(301)
    assert sim.state("light.ceiling") == "off"


def test_restart_recovers_lost_timer():
    sim = make()
    motion(sim)
    motion(sim, False)
    sim.advance(100)
    sim.restart_ha()  # timers lost, last_changed reset
    assert sim.state("light.ceiling") == "on"
    sim.advance(5 * MIN + 61)
    assert sim.state("light.ceiling") == "off", "watchdog switched off after restart"


def test_manual_mode_persists_through_restart_and_then_recovers():
    sim = make()
    sim.press("button_off")
    sim.restart_ha(downtime=40 * MIN)  # hold expired during downtime
    assert mode(sim) == "Manual off", "room not yet known to be empty"
    sim.advance(6 * MIN)
    assert mode(sim) == "Auto"


def test_unavailable_sensor_policy():
    sim = make()
    motion(sim)
    sim.device("binary_sensor.motion_b", "unavailable")
    motion(sim, False)
    sim.advance(20 * MIN)
    assert sim.state("light.ceiling") == "on", "fail-safe keeps lights on"
    sim = make({"unavailable_policy": "ignore"})
    motion(sim)
    sim.device("binary_sensor.motion_b", "unavailable")
    motion(sim, False)
    sim.advance(301)
    assert sim.state("light.ceiling") == "off"


def test_keep_awake_entity():
    sim = make({"keep_awake_entities": ["media_player.tv"]})
    motion(sim)
    sim.device("media_player.tv", "playing")
    motion(sim, False)
    sim.advance(30 * MIN)
    assert sim.state("light.ceiling") == "on"
    sim.device("media_player.tv", "idle")
    sim.advance(301)
    assert sim.state("light.ceiling") == "off"


def test_blocking_entity():
    sim = make({"blocking_entities": ["input_boolean.movie_mode"]}, {"input_boolean.movie_mode": "on"})
    motion(sim)
    assert sim.state("light.ceiling") == "off"


def test_switch_lights_and_scene():
    sim = make({"lights": ["switch.lamp_plug", "light.ceiling"], "default_brightness": 50},
               {"switch.lamp_plug": "off"})
    motion(sim)
    assert sim.state("switch.lamp_plug") == "on" and sim.state("light.ceiling") == "on"
    assert all(c[0] != "light.turn_on" or "switch.lamp_plug" not in c[1]["entity_id"] for c in sim.calls)
    motion(sim, False)
    sim.advance(301)
    assert sim.state("switch.lamp_plug") == "off" and sim.state("light.ceiling") == "off"
    sim = make({"default_scene": "scene.reading"})
    motion(sim)
    assert sim.attr("light.ceiling", "brightness") == 200


def test_without_hold_helper_hold_counts_from_mode_change():
    sim = make({"hold_helper": ""})
    sim.press("button_off")
    sim.advance(31 * MIN)
    assert mode(sim) == "Auto"
    assert not any(c[0] == "input_datetime.set_datetime" for c in sim.calls)


def test_never_policy():
    sim = make({"return_policy": "never"})
    sim.press("button_on")
    sim.advance(3 * 60 * MIN)
    assert (sim.state("light.ceiling"), mode(sim)) == ("on", "Manual on")


def test_configuration_errors_are_reported():
    sim = make(states={"input_select.room_mode": ("auto", {"options": ["auto", "manual_on"]})}, settle=False)
    sim.restart_ha()
    assert sim.notifications and "manual_off" in sim.notifications[-1]["message"]
    motion(sim)
    assert sim.state("light.ceiling") == "off", "does nothing while misconfigured"


def test_minimal_configuration():
    sim = Sim({"occupancy_sensors": ["binary_sensor.motion_a"], "lights": ["light.ceiling"],
               "mode_helper": "input_select.room_mode"}, start=START, states=BASE_STATES)
    sim.advance(10 * MIN)
    motion(sim)
    assert sim.state("light.ceiling") == "on"
    motion(sim, False)
    sim.advance(301)
    assert sim.state("light.ceiling") == "off"


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(err).__name__}: {err}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
