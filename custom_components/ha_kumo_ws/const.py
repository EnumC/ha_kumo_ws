"""Constants for the Mitsubishi Comfort integration."""

from homeassistant.const import Platform

DOMAIN = "mitsubishi_comfort"
PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR]

CONF_SITE_IDS = "site_ids"
CONF_REFRESH_ON_CONNECT = "refresh_on_connect"
