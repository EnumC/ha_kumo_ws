# Mitsubishi Comfort Home Assistant WebSocket Integration (Custom Component)

This repo is based off the amazing work of [dlarrick/hass-kumo](https://github.com/dlarrick/hass-kumo), [jjustinwilson/comfort_HA](https://github.com/jjustinwilson/comfort_HA), and [ventz/kumo-cloud-v3-api-comfort-client](https://github.com/ventz/kumo-cloud-v3-api-comfort-client).

This version has been updated and rewritten to use WebSocket for live updates (cloud push) instead of polling, which result in much faster updates.

## Features
- Config flow: username/password, site selection (multi-site supported)
- Live updates via websocket; REST for device discovery
- Climate entities with fan/swing control, dual setpoints in Auto mode, optimistic updates, and stale-update guards
- Exposes RSSI, error codes, serial number, and model number for each device.

## Project Layout
- `/custom_components/ha_kumo_ws/` — Home Assistant custom component
  - `climate.py` — climate entity
  - `sensor.py` — RSSI / twoFiguresCode sensors
  - `coordinator.py` — REST + socket coordinator with stale-update holds
  - `config_flow.py` — credentials + site selector
  - `pykumo2/` — async HTTP + socket client
- `pykumo2_smoke.py` — standalone smoke test (auth, site, devices, socket stream)

## Getting Started (Home Assistant)

### HACS (recommended)

Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=EnumC&repository=ha_kumo_ws&category=integration)

Alternatively, add this repo manually in HACS: 

- In HACS, click "Custom repositories" in the menu on the top right
- Enter `https://github.com/EnumC/ha_kumo_ws` as the repository
- Select "Integration" as the category
- Click "Add"
- Search for "Mitsubishi Comfort" in the HACS store and install.

### Setting up

- In Settings -> Devices -> "Add Integration", Select "Mitsubishi Comfort (WS)"
- Enter your username and password
- Select the site you want to monitor
- Click "Submit"

## Smoke Test
Use your `.env` (`KUMO_USERNAME`, `KUMO_PASSWORD`, optional `KUMO_SITE_IDS`) and run:
```bash
uv run python pykumo2_smoke.py
```
This authenticates, lists sites/devices, and streams socket events for 10s.
