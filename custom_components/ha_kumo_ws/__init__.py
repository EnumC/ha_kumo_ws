"""Home Assistant integration for Mitsubishi Comfort (Kumo Cloud)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .pykumo2 import AuthenticationError, MitsubishiComfortClient

from .const import CONF_REFRESH_ON_CONNECT, CONF_SITE_IDS, DOMAIN, PLATFORMS
from .coordinator import MitsubishiComfortCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up via config.yaml (not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mitsubishi Comfort from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    refresh_on_connect = entry.data.get(CONF_REFRESH_ON_CONNECT, True)
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    site_ids: list[str] = entry.data.get(CONF_SITE_IDS, [])

    client = MitsubishiComfortClient(username=username, password=password, site_ids=site_ids)
    coordinator = MitsubishiComfortCoordinator(
        hass=hass,
        client=client,
        site_ids=site_ids,
        refresh_on_connect=refresh_on_connect,
    )

    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_start_socket()
    except AuthenticationError as exc:
        await client.close()
        raise ConfigEntryAuthFailed("Failed to authenticate with Kumo Cloud") from exc
    except Exception as exc:
        await client.close()
        raise ConfigEntryNotReady(exc) from exc

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    stored = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    coordinator: MitsubishiComfortCoordinator | None = stored.get("coordinator")
    client: MitsubishiComfortClient | None = stored.get("client")

    if coordinator:
        await coordinator.async_stop()
    if client:
        await client.close()

    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry on options update."""
    await hass.config_entries.async_reload(entry.entry_id)
