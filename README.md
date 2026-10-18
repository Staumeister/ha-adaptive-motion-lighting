# Adaptive Motion Lighting with Manual Override

**Version 1.1.0** · Home Assistant **2024.10+** · MIT licence

This Home Assistant blueprint gives you motion and presence lighting that never argues with you. Lights come on when you walk in and go off once the room is really empty. If you switch a light by hand, the automation stays out of the way until you have left.

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FStaumeister%2Fha-adaptive-motion-lighting%2Fblob%2Fmain%2Fadaptive_motion_lighting.yaml)

## Features

- **Several sensors per room.** Lights go off only after *all* motion, occupancy and presence sensors have been clear for the vacancy delay.
- **Four modes**, stored in a dropdown helper you can put on a dashboard:
  - `auto`: normal motion lighting.
  - `manual_on` / `manual_off`: you are in charge. Motion is ignored until the room is empty and/or a hold time has passed.
  - `paused`: no automatic control at all until you resume.
- **Manual overrides can come from**:
  - IKEA remotes paired with ZHA (TRÅDFRI, RODRET, SOMRIG, BILRESA): just pick them;
  - any other remote or button (ZHA, Zigbee2MQTT, Hue, Matter, Shelly, …) through HA's normal triggers;
  - dashboard, app or voice changes made by a HA user, including dimming;
  - optionally, wall switches or bulb-bound remotes that change the light outside HA.
- **Lighting profiles.** There is a default profile plus two optional ones (for example *Evening* and *Night*). A profile is chosen by a time window that may cross midnight and/or by entities such as a `schedule` helper, an `input_boolean` "night mode" or `sun.sun`. Each profile can set:
  - its own lights (for example only a night light);
  - brightness and colour temperature, or a scene;
  - a vacant-room scene;
  - its own vacancy delay;
  - whether motion switches the lights on at all.
- **Warning before off.** Lights can dim for a while before switching off, so you can wave to keep them on.
- **Vacant-room scene.** Instead of switching off, the room can drop to an ambient glow. The next motion brings back the full profile.
- **Keep-awake entities.** A playing TV, a PC plug or a bed sensor keeps the lights on.
- **Blocking entities.** "Movie mode" or "guest sleeping" can stop lights switching on automatically.
- **Darkness conditions.** Motion can be limited to dark conditions by sun elevation and/or an illuminance sensor. Lux is only checked while the lights are off, so the lamp cannot block itself.
- **Plain switches work too.** Lights, light groups and `switch` entities (smart plugs) can be mixed.
- **Survives restarts and reloads.** Nothing relies on a long-running wait; a watchdog finishes any switch-off that a restart dropped.
- **Fail-safe.** An unavailable sensor keeps lights on by default. A misconfigured helper raises a notification instead of misbehaving.

## Setup

### 1. Create the helpers

Go to **Settings → Devices & services → Helpers → Create helper**.

| Helper | Type | Settings |
|---|---|---|
| Mode (required) | **Dropdown** | Options `auto`, `manual_on`, `manual_off`, `paused`. Case and spaces are ignored, so `Auto`, `Manual on`, `Manual off`, `Paused` also work. |
| Hold-until (optional) | **Date and/or time** | **Date and time** |

Every automation needs its own helper(s). In YAML, for example as a package:

```yaml
input_select:
  living_room_light_mode:
    name: Living room light mode
    icon: mdi:motion-sensor
    options: [auto, manual_on, manual_off, paused]

input_datetime:
  living_room_light_hold:
    name: Living room light hold until
    has_date: true
    has_time: true
```

Leave out `initial:`, so the helpers keep their values across restarts.

**Do I need the hold helper?** Without it, the minimum hold time is counted from the moment the mode last changed. With it, every manual action (each button press or dashboard change) restarts the hold time.

### 2. Import the blueprint

Choose one of these:

- Click the import button above.
- In Home Assistant, go to **Settings → Automations & scenes → Blueprints → Import blueprint** and paste the link to the blueprint file. The link to the repository's front page does not work.
  ```
  https://github.com/Staumeister/ha-adaptive-motion-lighting/blob/main/adaptive_motion_lighting.yaml
  ```
- Copy `adaptive_motion_lighting.yaml` to `config/blueprints/automation/<your-name>/`.

Then open the blueprint and choose **Create automation**.

### 3. Fill in the essentials

Pick the sensors, the lights and the mode helper. Everything else is optional and sits in collapsed sections. Disable any other automation that controls the same lights.

## How it decides

| You do / sensors see | In `auto` | In `manual_on` / `manual_off` | In `paused` |
|---|---|---|---|
| Motion | Lights on (if the profile allows it and it is dark enough, with no blockers) | ignored | ignored |
| All sensors clear for the delay | Warning dim, then off, or the vacant-room scene | Back to `auto`, lights off (according to *Return to auto*) | nothing |
| Button ON / OFF / TOGGLE | → `manual_on` / `manual_off` | Restarts the hold time | Switches lights, stays paused |
| Change a light in HA | → manual mode | Restarts the hold time | ignored |
| Button AUTO | Applies auto now: on if occupied, off if empty | → `auto` | → `auto` |
| Button PAUSE | → `paused` (optionally lights off) | → `paused` | – |
| Profile changes | Re-applies lighting to lights that are on (optional) | – | – |

**Return to auto** options:

- **After the hold time AND once the room is empty** (default). Best for most rooms.
- **Once the room is empty.**
- **After the hold time**, even if the room is occupied.
- **Never.** Only a button or the dashboard brings back `auto`.

Whichever you choose, "empty" means empty for the full vacancy delay *since the override started*. Switching a light on from the app while nobody is in the room is therefore not undone straight away. Lights that something else switched on in an empty room get the full delay before auto switches them off.

## Recipes

### Connecting a remote or buttons

There are two ways to connect buttons. You can use both together.

**IKEA remotes paired with ZHA (no setup).** In **Buttons and remotes**, pick your remotes in **IKEA remotes paired with ZHA**. That's it: the buttons work straight away. Supported:

- TRÅDFRI on/off switch (E1743)
- TRÅDFRI remote control, round with 5 buttons (E1524/E1810)
- RODRET (E2201)
- SOMRIG (E2213)
- BILRESA with buttons (E2489) and with scroll wheel (E2490), when paired with ZHA

Zigbee2MQTT and Matter (BILRESA over Thread) are planned for a later version. Until then, connect them with triggers as described below.

Default buttons:

| Action | Button |
| --- | --- |
| Manual ON | I / ▲ / one dot / button 1 – press |
| Manual OFF | O / ▼ / two dots / button 2 – press |
| Manual TOGGLE | Centre button of the round remote, or the BILRESA scroll wheel – press |
| Resume AUTO | I / ▲ / one dot / button 1 – long press |
| PAUSE | O / ▼ / two dots / button 2 – long press |

To change them, pick other presses from each action's dropdown, including double presses, long presses and the round remote's ◀ ▶ buttons. You can pick several per action. Presses you don't assign are ignored.

Note:

- **BILRESA with scroll wheel:** pressing the wheel sends *on* and *off* alternately; both count as the centre button press. Turning the wheel does nothing here.

**Other remotes: triggers.** Each action (**Manual ON**, **OFF**, **TOGGLE**, **Resume AUTO**, **PAUSE**) also takes a list of ordinary Home Assistant triggers, so any remote from any integration works. You can add several triggers to one action, for example two remotes.

In the blueprint form, the trigger picker may not offer button triggers such as *"First button short press"*. If not, build the trigger in an empty automation and copy it over:

1. Create a new, empty automation. You won't save it.
2. **Add trigger → Device →** your remote, then choose the button and press type.
3. Open the trigger's **⋮** menu → **Edit in YAML** and copy the text. For a ZHA remote it looks like this:
   ```yaml
   trigger: device
   domain: zha
   device_id: 1234abcd...
   type: remote_button_short_press
   subtype: button_1
   ```
4. In your blueprint automation, open **Buttons and remotes**, add a trigger to the action you want, then use **⋮ → Edit in YAML** to replace its content with the copied text.
5. Repeat for each button. In the scratch automation you can change just the `type` and `subtype` lines. Discard the scratch automation when you're done.

**No device triggers for your remote?** Use its raw events instead:

- **ZHA:** in **Developer tools → Events**, listen to `zha_event` and press a button. Note `device_id`, `command` and, if shown, `endpoint_id`, then use:
  ```yaml
  trigger: event
  event_type: zha_event
  event_data:
    device_id: YOUR_DEVICE_ID
    command: YOUR_COMMAND   # e.g. on, off, toggle, short_release, long_press
    endpoint_id: 1          # only if your remote reports it
  ```
- **Zigbee2MQTT, Hue, Matter, Shelly:** use a *State* trigger on the remote's `event.*` or `sensor.*_action` entity, with `to:` set to the action name.

**Testing:** press a button, then open the automation's **Traces**. The `plan` variable should show `why: button on`, `button off` and so on.

### Night light after 23:00

In **Profile 2**:

- From `23:00`, until `06:30`.
- Lights: `light.hallway_nightlight`.
- Brightness `5 %`, colour temperature `2200 K`.
- Vacancy delay `60 s`.

If someone is in the room at 23:00, the main lights fade out and the night light takes over (*Apply profile changes to lights that are on*).

### Bedtime by schedule or "sleep mode"

In **Profile 2**, set the entities to `schedule.bedtime` or `input_boolean.sleep_mode`, leave the time window unused, and turn off **Motion switches lights on**. At night, motion then never wakes you up, but lights you switch on by hand still go off once the room is empty.

### Bathroom with mmWave

Use a vacancy delay of `60 s`, a warning of `15 s` and the return policy **Once the room is empty**.

### Living room with a TV

Add `media_player.tv` to **Keep-awake entities**. The lights stay on during a film even when you are sitting completely still.

### Evening ambient

In **Profile 1**, set the entities to `sun.sun` (active below the horizon) and the vacant-room scene to `scene.living_room_glow`.

## Troubleshooting

- **Traces.** Each run computes a `plan` variable with a `why` field, for example `room vacant` or `light changed by hand`. Open the automation's **Traces** and look at *Changed variables*.
- **Keep more traces.** Runs that have nothing to do stop at the condition, but they still use trace slots. To keep more, open the automation in YAML mode and add:
  ```yaml
  trace:
    stored_traces: 30
  ```
- **Lights never go off.** Check for an `unavailable` sensor (with the fail-safe policy), an active keep-awake entity, or a mode other than `auto`.
- **Changes by a voice assistant don't count as manual.** Some integrations don't attach a HA user to their commands. Enable the experimental *outside Home Assistant* detection, or let the voice command set the mode helper instead.
- **Wall switch cuts power to the bulbs.** Software cannot fix that. Use a decoupled or smart switch, or bind the switch to the mode helper.

## Known limitations

- **Experimental outside-HA detection.** It relies on contexts. A device that reports a spontaneous on/off with no HA origin is taken as manual.
- **Minute precision for some steps.** Hold-time expiry and recovery after a restart are checked once a minute. Normal switch-off is precise to the second.
- **Scene contents.** Scenes should only contain this room's lights. Other lights in a scene are not tracked.
- **Button template triggers.** Button inputs are identified by position, and their contents are rendered as variables. Use device, event or state triggers there, not template triggers.

## Development

The `tests/` folder contains `ha_sim.py`, a small Home Assistant look-alike. It runs the blueprint's real triggers, templates, conditions and actions. It models state contexts, `for:` timers, template-trigger arming, `trigger.idx`, restart-mode cancellation and HA restarts. `test_blueprint.py` covers more than 30 scenarios.

```bash
pip install pyyaml jinja2 pytest
```

```bash
python -m pytest tests
```

The simulator is not Home Assistant. Before a release, also import the blueprint into a real instance and walk through the scenarios in `tests/test_blueprint.py`.

**Editing rules** (also stated at the top of the YAML):

- Keep `fixed_trigger_count` in sync with the fixed trigger list.
- Keep the profile-selection loop identical in the `for:` template and the `profile` variable. A test enforces this.
- Never output a bare `true` / `false` from a variable. HA keeps that as a (truthy) string, so use `{{ false }}`. A test enforces this too.

## Changelog

**1.1.0**

- IKEA remotes paired with ZHA (TRÅDFRI on/off switch, TRÅDFRI round remote, RODRET, SOMRIG, BILRESA) can be picked directly. Their buttons work straight away, and dropdowns let you change what each press does. No YAML needed.
- The existing trigger inputs are unchanged, so current automations keep working. After updating, the default buttons only apply once you pick a remote.

**1.0.0** is the first public release.

- Four modes (auto, manual on, manual off, paused) with hold time and four return-to-auto policies.
- Any remote through trigger inputs; dashboard, app and voice changes detected as manual overrides; optional detection of changes made outside HA.
- Default lighting plus two optional profiles, activated by time and/or entities, each with its own lights, brightness, colour temperature, scenes, vacant-room scene, vacancy delay and auto-on setting.
- Dim-before-off warning, keep-awake and blocking entities, lux and sun-elevation conditions, switch support, several lights.
- Restart-safe: precise `for:` triggers plus an event-driven watchdog, and a notification if the configuration is wrong.
