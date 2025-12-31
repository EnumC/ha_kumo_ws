"""Constants for the Mitsubishi Comfort integration."""

from homeassistant.const import Platform

DOMAIN = "ha_kumo_ws"
PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER]

CONF_SITE_IDS = "site_ids"
CONF_REFRESH_ON_CONNECT = "refresh_on_connect"
