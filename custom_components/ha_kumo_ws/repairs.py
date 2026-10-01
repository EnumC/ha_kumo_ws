"""Repair issues raised from link state and the remote temperature feeder."""

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, new_device_signal
from .coordinator import KumoDeviceCoordinator
from .pykumo2.domain.enums import LinkState

if TYPE_CHECKING:
    from .hub import KumoConfigEntry

ISSUE_LOCAL_AUTH_STALE = "local_auth_stale"
ISSUE_REMOTE_TEMP_LOST = "remote_temp_source_lost"


def _issue_id(key: str, serial: str) -> str:
    return f"{key}_{serial}"


@callback
def async_raise_unit_issue(
    hass: HomeAssistant, key: str, coordinator: KumoDeviceCoordinator
) -> None:
    """Create a warning for one unit; fixing it is done in the options flow."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(key, coordinator.serial),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders={"name": coordinator.device.name},
    )


@callback
def async_clear_unit_issue(hass: HomeAssistant, key: str, serial: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(key, serial))


@callback
def async_setup_repairs(hass: HomeAssistant, entry: KumoConfigEntry) -> None:
    """Raise local_auth_stale for local-only units whose credentials stopped working."""
    hub = entry.runtime_data

    @callback
    def _watch(coordinator: KumoDeviceCoordinator) -> None:
        @callback
        def _check() -> None:
            link = coordinator.link
            if link.state is LinkState.AUTH_STALE and not link.policy.has_cloud:
                async_raise_unit_issue(hass, ISSUE_LOCAL_AUTH_STALE, coordinator)
            else:
                async_clear_unit_issue(hass, ISSUE_LOCAL_AUTH_STALE, coordinator.serial)

        _check()
        entry.async_on_unload(coordinator.async_add_listener(_check))
        entry.async_on_unload(
            lambda: async_clear_unit_issue(hass, ISSUE_LOCAL_AUTH_STALE, coordinator.serial)
        )

    if not hub.local_capable:
        return
    for coordinator in hub.coordinators.values():
        _watch(coordinator)

    @callback
    def _on_new_device(serial: str) -> None:
        _watch(hub.coordinators[serial])

    entry.async_on_unload(
        async_dispatcher_connect(hass, new_device_signal(entry.entry_id), _on_new_device)
    )
