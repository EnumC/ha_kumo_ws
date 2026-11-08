# Mitsubishi Comfort Home Assistant WebSocket Integration (Custom Component)


The most comprehensive Comfort integration for HACS yet. This repo is based off the amazing work of [dlarrick/hass-kumo](https://github.com/dlarrick/hass-kumo), [jjustinwilson/comfort_HA](https://github.com/jjustinwilson/comfort_HA), and [ventz/kumo-cloud-v3-api-comfort-client](https://github.com/ventz/kumo-cloud-v3-api-comfort-client).

This custom component has been updated and rewritten to use WebSocket for live updates (cloud push) instead of polling, which result in much faster updates. If you have local unit credentials, it also exposes low level unit attributes and have the capability to configure custom temperature sources.

## Update
- Sept 2026: Unfortunately the original hass-kumo stopped working again due to api changes. You can switch back to this ws version if you're impacted. If you have valid local unit credentials, this integration can also ingest it now and will route requests locally for requests where this would be faster. To do so, choose "Set up using existing local unit credential backup" during setup and paste the contents of `kumo_cache.json`, which hass-kumo saves in your Home Assistant configuration folder. Keep a copy of that file until you are set up on the ws integration.

- ~~The original [dlarrick/hass-kumo](https://github.com/dlarrick/hass-kumo) plugin has been updated in v0.4.1 and works again! I recommend trying that one first as direct local communication will always be faster than bouncing the request off of the cloud API. If you run into issues, give this repo a try.~~
- ~~There are some additional sensors and actions exposed in the websocket version that has not been ported to the upstream repo yet, such as setting offset temp, and some fixes for compatibility with matterbridge. If you need those features, you may want to continue using the websocket version (or open a PR to implement them in the upstream repo!)~~


## Getting Started (Home Assistant)

### HACS (recommended)

Install the integration from the Home Assistant Community Store.

[![Install the integration from the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=EnumC&repository=ha_kumo_ws&category=integration)

Alternatively, add this repo manually in HACS: 

- In HACS, search for "Mitsubishi Comfort" in the HACS store and install.

### Setting up

- In Settings -> Devices -> "Add Integration", Select "Mitsubishi Comfort (WS)"
- Enter your username and password
- Select the site you want to monitor
- Optionally choose a Home Assistant temperature sensor for each unit
- Click "Submit"

## Features
- Config flow: username/password, site selection (multi-site supported)
- Live updates via websocket; REST for device discovery
- Intelligently avoid race conditions by queuing and invalidating requests/updates.
- Climate entities with fan/swing control, dual setpoints in Auto mode, and guards against stale update values.
- NEW (local required): Selectable temperature source (on device thermistor, wireless sensor (PAC-USWHS003-TH-1, must pair in official app first), or from a Home Assistant sensor entity). Home Assistant sensor entity will also be available without local creds in a future update, so stay tuned!
- NEW: Smart control (dynamic fan control, automatic off and resume). See below.
- NEW: hvac_action from the unit's real states (standby, defrost, hot adjust, and CN105 sub mode and compressor flag when enabled). See below.
- Exposes RSSI, error codes, serial number, and model number for each device.

## Smart control

### Dynamic fan control
- When enabled, the climate entity gets a "Dynamic" fan mode. Picking it lets the integration set the fan speed; picking any other speed hands control back to you.
- The speed scales with how far the room is from the setpoint: the quietest speed at or past the setpoint, the strongest at "Full speed at" or more.
- Speed changes are rate limited and use a hysteresis, so the fan does not hunt between two speeds. The unit's own fan control is left alone during defrost and hot adjust.
- A speed changed from the remote while Dynamic is active is set back after the hold time.

### Automatic off
- Mitsubishi units keep the indoor fan running after the room reaches the setpoint, and there is no fan-off command. When enabled, the unit is powered off once the room has passed the setpoint by the off margin for the dwell time, and powered back on in the same mode once it drifts back past the restart margin.
- While held off, the climate entity keeps showing the intended mode with action Idle.
- The hold survives Home Assistant restarts. If the mapped temperature sensor stays unavailable for 15 minutes, the unit is powered back on.
- Turning the unit off or changing the mode from Home Assistant, or powering it on externally, cancels the hold. Turn the unit off from Home Assistant rather than the remote: an off from the remote cannot be seen while the unit is already held off.

### Options

| Option | Default | Range |
|---|---|---|
| Dynamic fan control | Off | |
| Full speed at | 2.0 C from setpoint | 1.0-5.0 C |
| Fan hysteresis | 0.25 C | 0.0-1.0 C |
| Fan hold (minimum time between speed changes) | 120 s | 30-900 s |
| Automatic off | Off | |
| Off margin (past setpoint before turning off) | 0.5 C | 0.0-2.0 C |
| Restart margin (past setpoint before turning back on) | 1.0 C | 0.5-3.0 C |
| Dwell (time past the off margin before turning off) | 3 min | 1-30 min |
| Idle dwell (shorter dwell when the unit reports standby or a compressor stop) | 1 min | 0-10 min |
| Minimum on time | 10 min | 0-60 min |
| Minimum off time | 5 min | 3-60 min |


## Remote temperature sensor
A Home Assistant temperature sensor can be mapped to a unit under "Remote temperature" in the integration options. Units with local credentials receive the sensor reading directly. Units reached only through the cloud are steered, as described below.
- With "Manage the temperature source" on, the unit is switched to the sensor and back to its previous source when the sensor is unavailable for the unavailable timeout (default 30 min) or the mapping is removed. If the unit drops the setting on its own (adapter reset), it is switched back.
- The unit is only switched to the sensor while the sensor is reporting. "Stale after" is derived from the sensor's recorded history (1.5 times its longest reporting gap over the last day, 5 to 30 min); but the value can be overwritten with a custom stale timeout.

### Cloud-only units
The Kumo Cloud cannot accept a remote temperature, so a unit reached only through the cloud is steered instead. The integration keeps your target temperature in Home Assistant and adjusts the unit setpoint so the room sensor reaches it.
- The integration learns the difference between the unit's own reading and the room sensor while the unit is running, and adds that difference to the setpoint it sends.
- A slow correction removes any remaining miss. The climate entity shows the room sensor temperature and your target, not the shifted setpoint.
- Smart control uses the room sensor and your target as well.
- If the sensor is unavailable for the unavailable timeout, or the mapping is removed, the unit setpoint is set back to your target.
- The tuning values are in the collapsed "Cloud steering" section of the unit's remote temperature options.
- The setup flow asks for a sensor for each unit. A room sensor is recommended for ductless units, which otherwise measure the air returning into the unit, and it improves automatic off.

## HVAC action
| Unit state | hvac_action |
|---|---|
| Off | Off |
| Held off by automatic off | Idle |
| Defrost (status, or CN105 0x09 sub mode) | Defrosting |
| Hot adjust (status), or CN105 0x09 preheat / warmup | Preheating |
| Fan mode | Fan |
| Standby (status), CN105 0x09 standby, or CN105 0x06 compressor stopped | Idle |
| Dry | Drying |
| Heat / Cool | Heating / Cooling |
| Auto | Heating or Cooling from the active side, unknown when it cannot be told |

The Compressor sensor needs CN105 code 0x06. The 0x03 runtime counter is still exposed as a sensor but is no longer used to infer compressor activity, because it rises whenever the unit is on.

## Project Layout
- `/custom_components/ha_kumo_ws/` — Home Assistant custom component
  - `climate.py` — climate entity
  - `sensor.py` — RSSI / twoFiguresCode sensors
  - `coordinator.py` — REST + socket coordinator with stale-update holds
  - `config_flow.py` — credentials + site selector
  - `pykumo2/` — async HTTP + socket client
- `pykumo2_smoke.py` — standalone smoke test (auth, site, devices, socket stream)

## Smoke Test
Use your `.env` (`KUMO_USERNAME`, `KUMO_PASSWORD`, optional `KUMO_SITE_IDS`) and run:
```bash
uv run python pykumo2_smoke.py
```
This authenticates, lists sites/devices, and streams socket events for 10s.
