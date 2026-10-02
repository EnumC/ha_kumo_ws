"""Mitsubishi Comfort (Kumo): local-first with Kumo Cloud fallback."""

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    CN105_ENTITY_CODES,
    CONF_CONNECTION_MODE,
    CONF_REFRESH_ON_CONNECT,
    CONF_SETUP_METHOD,
    CONF_SITE_IDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    PLATFORMS,
)
from .hub import KumoConfigEntry, KumoHub
from .pykumo2.errors import AuthenticationError, KumoError
from .repairs import async_setup_repairs
from .storage import async_remove_store

_LOGGER = logging.getLogger(__name__)

ENTRY_PLATFORMS = [*PLATFORMS, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SELECT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

LEGACY_UNIQUE_ID_SUFFIXES = {"RSSI": "wifi_rssi", "Two-Figures Code": "two_figures_code"}


async def async_setup_entry(hass: HomeAssistant, entry: KumoConfigEntry) -> bool:
    """Build the hub, start it and forward platforms."""
    hub = KumoHub(hass, entry)
    try:
        await hub.async_start()
    except (ConfigEntryAuthFailed, ConfigEntryNotReady):
        await hub.async_stop()
        raise
    except AuthenticationError as err:
        await hub.async_stop()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="cloud_auth_failed"
        ) from err
    except KumoError as err:
        await hub.async_stop()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cloud_unavailable",
            translation_placeholders={"error": type(err).__name__},
        ) from err
    entry.runtime_data = hub
    _sync_cn105_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, ENTRY_PLATFORMS)
    async_setup_repairs(hass, entry)
    return True


@callback
def _sync_cn105_entities(hass: HomeAssistant, entry: KumoConfigEntry) -> None:
    """Disable CN105 entities whose codes are not selected; re-enable them once they are."""
    hub = entry.runtime_data
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        key = next((k for k in CN105_ENTITY_CODES if entity.unique_id.endswith(f"_{k}")), None)
        if key is None:
            continue
        if hub.cn105_provides(key):
            if entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity.entity_id, disabled_by=None)
        elif entity.disabled_by is None:
            registry.async_update_entity(
                entity.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )


async def async_unload_entry(hass: HomeAssistant, entry: KumoConfigEntry) -> bool:
    """Unload platforms and stop the hub."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, ENTRY_PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the credential Store."""
    await async_remove_store(hass, entry.unique_id or entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 (cloud only) -> v2 (setup methods, options)."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        old = dict(entry.data)
        data = {
            CONF_SETUP_METHOD: "cloud_ws",
            CONF_USERNAME: old.get(CONF_USERNAME),
            CONF_PASSWORD: old.get(CONF_PASSWORD),
            CONF_SITE_IDS: list(old.get(CONF_SITE_IDS) or []),
        }
        options = {
            **DEFAULT_OPTIONS,
            CONF_CONNECTION_MODE: "cloud_only",
            **entry.options,
            CONF_REFRESH_ON_CONNECT: bool(old.get(CONF_REFRESH_ON_CONNECT, True)),
        }
        await er.async_migrate_entries(hass, entry.entry_id, _migrate_unique_id)
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=2, minor_version=1
        )
        _LOGGER.debug("Migrated %s to version 2", entry.entry_id)
    return True


@callback
def _migrate_unique_id(entity_entry: er.RegistryEntry) -> dict[str, Any] | None:
    for old, new in LEGACY_UNIQUE_ID_SUFFIXES.items():
        suffix = f"_{old}"
        if entity_entry.unique_id.endswith(suffix):
            return {"new_unique_id": entity_entry.unique_id.removesuffix(suffix) + f"_{new}"}
    return None
