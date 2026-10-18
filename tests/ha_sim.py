"""A tiny Home Assistant look-alike that runs the blueprint for real.

It loads the blueprint, substitutes inputs, and then executes the actual
triggers, variables, conditions and actions with HA-like semantics:

* HA template functions/tests used by the blueprint, the HA result parser
  (literal_eval, so a bare `false` stays the string "false"), sequential
  variable rendering, and an immutable sandbox like HA's.
* A state machine with last_changed and contexts (id/parent_id/user_id),
  service calls that write states with the run's context.
* State triggers (to / not_to / match-all, `for:` timers that cancel when
  the state changes), template triggers (fire on false -> true, never at
  setup), time triggers, nested button trigger lists with `trigger.idx`.
* mode: restart: a run that passes conditions cancels a running one.

It is not Home Assistant. It exists to exercise the decision logic and catch
template errors; the real acceptance test is still a running HA instance.
"""
from __future__ import annotations

import itertools
import re
from ast import literal_eval
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, Undefined, nodes  # noqa: F401
from jinja2.sandbox import ImmutableSandboxedEnvironment

BLUEPRINT = Path(__file__).resolve().parent.parent / "adaptive_motion_lighting.yaml"
TZ = timezone(timedelta(hours=1))
_ids = itertools.count(1)


# --------------------------------------------------------------------------- YAML
class Input:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"!input {self.name}"


class BlueprintLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node):
    out = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in out:
            raise AssertionError(f"duplicate YAML key {key!r} (line {key_node.start_mark.line + 1})")
        out[key] = loader.construct_object(value_node, deep=True)
    return out


BlueprintLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)
BlueprintLoader.add_constructor("!input", lambda loader, node: Input(loader.construct_scalar(node)))


def load_blueprint(path: Path = BLUEPRINT) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def input_definitions(bp: dict) -> dict[str, dict]:
    defs = {}
    for key, value in bp["blueprint"]["input"].items():
        if "input" in value:  # section
            defs.update(value["input"])
        else:
            defs[key] = value
    return defs


def substitute(obj: Any, values: dict) -> Any:
    if isinstance(obj, Input):
        return values[obj.name]
    if isinstance(obj, dict):
        return {k: substitute(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, values) for v in obj]
    return obj


def flatten_triggers(triggers: list) -> list:
    flat = []
    for t in triggers:
        if set(t) == {"triggers"}:
            flat.extend(t["triggers"] or [])
        else:
            flat.append(t)
    return flat


# --------------------------------------------------------------------- states
@dataclass
class Context:
    user_id: str | None = None
    parent_id: str | None = None
    id: str = field(default_factory=lambda: f"ctx{next(_ids)}")

    def as_dict(self):
        return {"id": self.id, "parent_id": self.parent_id, "user_id": self.user_id}


@dataclass
class State:
    entity_id: str
    state: str
    attributes: dict
    last_changed: datetime
    last_updated: datetime
    context: Context

    @property
    def domain(self):
        return self.entity_id.split(".")[0]

    @property
    def name(self):
        return self.attributes.get("friendly_name", self.entity_id)


class TemplateError(Exception):
    pass


# ------------------------------------------------------------------ templates
def _parse_result(text: str) -> Any:
    text = text.strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)", text):
        return float(text)
    try:
        result = literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return text
    if isinstance(result, (str, complex)):
        return text
    return result


class Sim:
    """Runs one blueprint automation."""

    def __init__(self, inputs: dict, *, start: datetime, states: dict | None = None,
                 scenes: dict | None = None, sun_elevation: float = -10.0) -> None:
        self.bp = load_blueprint()
        defs = input_definitions(self.bp)
        missing = [k for k, d in defs.items() if "default" not in d and k not in inputs]
        assert not missing, f"required inputs not given: {missing}"
        unknown = set(inputs) - set(defs)
        assert not unknown, f"unknown inputs: {unknown}"
        self.inputs = {k: d.get("default") for k, d in defs.items()} | inputs
        self.config = substitute({k: v for k, v in self.bp.items() if k != "blueprint"}, self.inputs)
        self.triggers = flatten_triggers(self.config["triggers"])
        self.now = start
        self.states: dict[str, State] = {}
        self.scenes = scenes or {}
        self.calls: list[tuple[str, dict, dict]] = []
        self.notifications: list[dict] = []
        self.runs: list[dict] = []  # every trigger that executed (passed conditions)
        self.skipped: list[dict] = []  # triggers whose plan was empty
        self.errors: list[str] = []
        self.timers: list[dict] = []  # pending `for:` triggers and delays
        self.current = None  # context of the running script (mode: restart)
        self.this = {"entity_id": "automation.test_room", "state": "on",
                     "attributes": {"friendly_name": "Test room lighting"},
                     "context": Context().as_dict()}
        self.env = self._make_env()
        self.set("sun.sun", "below_horizon" if sun_elevation < 0 else "above_horizon",
                 {"elevation": sun_elevation})
        for entity_id, value in (states or {}).items():
            state, attrs = value if isinstance(value, tuple) else (value, {})
            self.set(entity_id, state, attrs)
        self.trigger_vars = self._render_vars(self.config.get("trigger_variables", {}), {"this": self.this})
        self.template_armed = {}
        for idx, trig in enumerate(self.triggers):
            if trig["trigger"] == "template":
                # Template triggers never fire if already true at setup.
                self.template_armed[idx] = not self._truthy(self._render(trig["value_template"], self._trigger_scope()))

    # -------------------------------------------------------------- template env
    def _make_env(self):
        env = ImmutableSandboxedEnvironment(undefined=Undefined, extensions=["jinja2.ext.loopcontrols"])
        sim = self

        class AllStates:
            def __getitem__(self, name):
                if "." in name:
                    return sim.states.get(name)
                return None

            __getattr__ = __getitem__

            def __call__(self, entity_id):
                state = sim.states.get(entity_id)
                return state.state if state else "unknown"

        def is_state(entity_id, value):
            state = sim.states.get(entity_id)
            if state is None:
                return False
            return state.state in value if isinstance(value, list) else state.state == value

        def state_attr(entity_id, attr):
            state = sim.states.get(entity_id) if entity_id else None
            return None if state is None else state.attributes.get(attr)

        def is_state_attr(entity_id, attr, value):
            return state_attr(entity_id, attr) == value

        def has_value(entity_id):
            state = sim.states.get(entity_id) if entity_id else None
            return state is not None and state.state not in ("unknown", "unavailable")

        def as_timestamp(value, default=None):
            if isinstance(value, datetime):
                return value.timestamp()
            try:
                return datetime.fromisoformat(str(value)).timestamp()
            except ValueError:
                if default is None:
                    raise TemplateError(f"as_timestamp got invalid input {value!r}")
                return default

        def _float(value, default=None):
            try:
                return float(value)
            except (TypeError, ValueError):
                if default is None:
                    raise TemplateError(f"float got invalid input {value!r}")
                return default

        def _int(value, default=None, base=10):
            try:
                if isinstance(value, str):
                    return int(float(value)) if "." in value else int(value, base)
                return int(value)
            except (TypeError, ValueError):
                if default is None:
                    raise TemplateError(f"int got invalid input {value!r}")
                return default

        def expand(*args):
            out = []
            for a in args:
                for e in a if isinstance(a, list) else [a]:
                    if e in sim.states:
                        out.append(sim.states[e])
            return out

        env.globals.update(states=AllStates(), is_state=is_state, state_attr=state_attr,
                           is_state_attr=is_state_attr, has_value=has_value,
                           now=lambda: sim.now, as_timestamp=as_timestamp, expand=expand)
        env.filters.update(float=_float, int=_int, as_timestamp=as_timestamp)
        env.tests.update(is_state=is_state, is_state_attr=is_state_attr, has_value=has_value,
                         match=lambda value, pattern: bool(re.match(pattern, str(value))))
        return env

    def _render(self, value: Any, scope: dict) -> Any:
        if isinstance(value, str):
            if "{{" not in value and "{%" not in value:
                return value
            try:
                return _parse_result(self.env.from_string(value).render(**scope))
            except TemplateError:
                raise
            except Exception as err:  # noqa: BLE001
                raise TemplateError(f"{type(err).__name__}: {err}\n--- template ---\n{value}") from err
        if isinstance(value, list):
            return [self._render(v, scope) for v in value]
        if isinstance(value, dict):
            return {k: self._render(v, scope) for k, v in value.items()}
        return value

    def _render_vars(self, variables: dict, scope: dict) -> dict:
        out = dict(scope)
        for key, value in variables.items():
            if key in out:  # HA never overrides run variables
                continue
            out[key] = self._render(value, out)
        return out

    @staticmethod
    def _truthy(value):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "on", "yes", "enable", "1")
        return bool(value)

    def _trigger_scope(self, extra=None):
        return {**getattr(self, "trigger_vars", {}), "this": self.this, **(extra or {})}

    # ------------------------------------------------------------ state machine
    def set(self, entity_id, state, attrs=None, *, context: Context | None = None, keep_attrs=False):
        old = self.states.get(entity_id)
        attributes = dict(old.attributes) if (keep_attrs and old) else {}
        attributes.update(attrs or {})
        changed = old is None or old.state != state
        new = State(entity_id, state, attributes,
                    self.now if changed else old.last_changed, self.now, context or Context())
        self.states[entity_id] = new
        if hasattr(self, "trigger_vars"):
            self._on_state_changed(entity_id, old, new)
        return new

    def user(self, entity_id, state, attrs=None, user_id="user1"):
        """A person changes something in the HA UI."""
        return self.set(entity_id, state, attrs, context=Context(user_id=user_id), keep_attrs=True)

    def device(self, entity_id, state, attrs=None):
        """A device reports a change that did not come from HA."""
        return self.set(entity_id, state, attrs, context=Context(), keep_attrs=True)

    def press(self, group: str):
        """Fire the first trigger of a button input (button_on, button_off...)."""
        offset = None
        idx = 0
        for t in self.config["triggers"]:
            if set(t) == {"triggers"}:
                if t is self._button_wrapper(group):
                    offset = idx
                idx += len(t["triggers"] or [])
            else:
                idx += 1
        assert offset is not None, f"no triggers configured for {group}"
        self._fire(offset, {"platform": "device"}, Context())

    def _button_wrapper(self, group):
        groups = [t for t in self.config["triggers"] if set(t) == {"triggers"}]
        order = ["button_on", "button_off", "button_toggle", "button_auto", "button_pause"]
        return groups[order.index(group)]

    def _on_state_changed(self, entity_id, old, new):
        for idx, trig in enumerate(self.triggers):
            if trig["trigger"] != "state":
                continue
            ids = trig["entity_id"]
            ids = ids if isinstance(ids, list) else [ids]
            if entity_id not in ids:
                continue
            # A state change cancels a pending `for:`; attribute-only updates do not.
            if old is None or old.state != new.state:
                self.timers = [t for t in self.timers
                               if not (t["kind"] == "for" and t["idx"] == idx and t["entity_id"] == entity_id)]
            if not self._state_matches(trig, old, new):
                continue
            data = {"platform": "state", "entity_id": entity_id, "from_state": old, "to_state": new}
            if "for" in trig:
                seconds = self._render(trig["for"]["seconds"], self._trigger_scope({"trigger": data}))
                self.timers.append({"kind": "for", "idx": idx, "entity_id": entity_id, "state": new.state,
                                    "due": self.now + timedelta(seconds=float(seconds)), "data": data,
                                    "context": new.context})
            else:
                self._fire(idx, data, new.context)
        self._check_templates()

    @staticmethod
    def _state_matches(trig, old, new):
        old_v = old.state if old else None
        new_v = new.state if new else None
        has_filter = any(k in trig for k in ("to", "from", "not_to", "not_from"))

        def match(value, spec):
            if spec is None:
                return True
            return value in spec if isinstance(spec, list) else value == spec

        if "to" in trig and not match(new_v, trig["to"]):
            return False
        if "not_to" in trig and match(new_v, trig["not_to"]):
            return False
        if "from" in trig and not match(old_v, trig["from"]):
            return False
        if has_filter and old_v == new_v:
            return False
        return True

    def _check_templates(self):
        for idx, trig in enumerate(self.triggers):
            if trig["trigger"] != "template":
                continue
            value = self._truthy(self._render(trig["value_template"], self._trigger_scope()))
            if not value:
                self.template_armed[idx] = True
            elif self.template_armed[idx]:
                self.template_armed[idx] = False
                self._fire(idx, {"platform": "template"}, None)

    # -------------------------------------------------------------------- time
    def advance(self, seconds: float, step: float = 1.0):
        """Advance the clock, firing timers, time triggers and template triggers."""
        end = self.now + timedelta(seconds=seconds)
        while self.now < end:
            nxt = min([end, self.now + timedelta(seconds=step)]
                      + [t["due"] for t in self.timers if t["due"] > self.now])
            prev = self.now
            self.now = nxt
            for t in sorted([t for t in self.timers if t["due"] <= self.now], key=lambda t: t["due"]):
                if t not in self.timers:
                    continue
                self.timers.remove(t)
                if t["kind"] == "for":
                    self._fire(t["idx"], t["data"], t["context"])
                else:
                    self._resume(t)
            for idx, trig in enumerate(self.triggers):
                if trig["trigger"] == "time":
                    for at in trig["at"]:
                        h, m, s = (int(x) for x in at.split(":"))
                        target = self.now.replace(hour=h, minute=m, second=s, microsecond=0)
                        if prev < target <= self.now:
                            self._fire(idx, {"platform": "time", "now": self.now}, None)
                            break
            if prev.minute != self.now.minute or prev.hour != self.now.hour:
                self._check_templates()  # now() templates re-render once per minute

    def restart_ha(self, downtime: float = 60):
        """Simulate an HA restart: timers lost, last_changed reset, start event."""
        self.timers = []
        self.current = None
        self.now += timedelta(seconds=downtime)
        for s in self.states.values():
            s.last_changed = s.last_updated = self.now
        for idx, trig in enumerate(self.triggers):
            if trig["trigger"] == "template":
                self.template_armed[idx] = not self._truthy(self._render(trig["value_template"], self._trigger_scope()))
        for idx, trig in enumerate(self.triggers):
            if trig["trigger"] == "homeassistant":
                self._fire(idx, {"platform": "homeassistant", "event": "start"}, None)

    # -------------------------------------------------------------------- runs
    def _fire(self, idx: int, data: dict, ctx: Context | None):
        trig = self.triggers[idx]
        trigger = {"id": trig.get("id", str(idx)), "idx": str(idx), **data}
        run_ctx = Context(parent_id=ctx.id if ctx else None)
        scope = self._render_vars(self.config["variables"], {**self.trigger_vars, "this": self.this, "trigger": trigger})
        ok = all(self._condition(c, scope) for c in self.config.get("conditions", []))
        record = {"trigger": trigger["id"], "event": scope["event"], "plan": scope["plan"], "time": self.now}
        if not ok:
            self.skipped.append(record)
            return
        self.runs.append(record)
        self.this = {**self.this, "context": run_ctx.as_dict()}
        # mode: restart: the running script is cancelled.
        self.timers = [t for t in self.timers if t["kind"] != "delay"]
        self.current = run_ctx
        gen = self._run_sequence(self.config["actions"], scope, run_ctx)
        self._step(gen, run_ctx)

    def _step(self, gen, ctx, resume=False):
        try:
            seconds = gen.send(True) if resume else next(gen)
        except StopIteration:
            if self.current is ctx:
                self.current = None
            return
        self.timers.append({"kind": "delay", "gen": gen, "ctx": ctx,
                            "due": self.now + timedelta(seconds=seconds)})

    def _resume(self, timer):
        if self.current is timer["ctx"]:
            self._step(timer["gen"], timer["ctx"], resume=True)

    class _Stop(Exception):
        pass

    def _condition(self, cond, scope):
        if isinstance(cond, str):
            return self._truthy(self._render(cond, scope))
        if cond.get("condition") == "template":
            return self._truthy(self._render(cond["value_template"], scope))
        raise NotImplementedError(cond)

    def _run_sequence(self, steps, scope, ctx):
        try:
            yield from self._sequence(steps, scope, ctx)
        except Sim._Stop:
            return

    def _sequence(self, steps, scope, ctx):
        for step in steps:
            if self.current is not ctx:
                raise Sim._Stop()  # cancelled by a newer run (mode: restart)
            if "variables" in step:
                for key, value in step["variables"].items():
                    scope[key] = self._render(value, scope)
            elif "if" in step:
                if all(self._condition(c, scope) for c in step["if"]):
                    yield from self._sequence(step["then"], scope, ctx)
                elif "else" in step:
                    yield from self._sequence(step["else"], scope, ctx)
            elif "choose" in step:
                for option in step["choose"]:
                    if all(self._condition(c, scope) for c in option["conditions"]):
                        yield from self._sequence(option["sequence"], scope, ctx)
                        break
                else:
                    yield from self._sequence(step.get("default", []), scope, ctx)
            elif "delay" in step:
                seconds = float(self._render(step["delay"]["seconds"], scope))
                if seconds > 0:
                    yield seconds
            elif "stop" in step:
                raise Sim._Stop()
            elif "action" in step:
                self._call(step["action"], self._render(step.get("target", {}), scope),
                           self._render(step.get("data", {}), scope), ctx)
            else:
                raise NotImplementedError(step)

    def _call(self, action, target, data, ctx):
        if isinstance(data, str):
            raise AssertionError(f"{action}: data rendered to a string: {data!r}")
        ids = target.get("entity_id", [])
        ids = ids if isinstance(ids, list) else [ids]
        assert ids or action.startswith("persistent_notification"), f"{action} called without targets"
        self.calls.append((action, {"entity_id": ids}, data))
        domain, service = action.split(".")
        for entity_id in ids:
            entity_domain = entity_id.split(".")[0]
            if action == "light.turn_on":
                assert entity_domain == "light", entity_id
                attrs = {}
                if "brightness_pct" in data:
                    attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
                elif entity_id not in self.states or self.states[entity_id].state != "on":
                    previous = self.states.get(entity_id)
                    attrs["brightness"] = (previous.attributes.get("brightness") if previous else None) or 255
                if "color_temp_kelvin" in data:
                    attrs["color_temp_kelvin"] = data["color_temp_kelvin"]
                self.set(entity_id, "on", attrs, context=ctx, keep_attrs=True)
            elif service == "turn_off" and domain in ("light", "switch", "homeassistant"):
                assert domain == "homeassistant" or entity_domain == domain, entity_id
                self.set(entity_id, "off", context=ctx, keep_attrs=True)
            elif action == "switch.turn_on":
                assert entity_domain == "switch", entity_id
                self.set(entity_id, "on", context=ctx, keep_attrs=True)
            elif action == "scene.turn_on":
                for light, (state, attrs) in self.scenes[entity_id].items():
                    self.set(light, state, attrs, context=ctx, keep_attrs=True)
            elif action == "input_select.select_option":
                options = self.states[entity_id].attributes["options"]
                assert data["option"] in options, f"invalid option {data['option']!r}"
                self.set(entity_id, data["option"], context=ctx, keep_attrs=True)
            elif action == "input_datetime.set_datetime":
                ts = float(data["timestamp"])
                self.set(entity_id, datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S"),
                         {"timestamp": ts}, context=ctx, keep_attrs=True)
            else:
                raise NotImplementedError(action)
        if action == "persistent_notification.create":
            self.notifications.append(data)

    # ----------------------------------------------------------------- helpers
    def state(self, entity_id):
        return self.states[entity_id].state

    def attr(self, entity_id, name):
        return self.states[entity_id].attributes.get(name)

    def last_plan(self):
        return self.runs[-1]["plan"] if self.runs else None
