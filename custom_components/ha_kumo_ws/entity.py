"""Shared entity base and device info."""

from collections.abc import Callable, Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, UNIQUE_ID_PREFIX, new_device_signal
from .coordinator import KumoDeviceCoordinator
from .hub import KumoConfigEntry, KumoHub
from .pykumo2.domain.state import DeviceState


def device_info(coordinator: KumoDeviceCoordinator) -> DeviceInfo:
    """DeviceInfo for one indoor unit."""
    device, state = coordinator.device, coordinator.data
    info = DeviceInfo(
        identifiers={(DOMAIN, device.serial)},
        name=device.name,
        manufacturer=MANUFACTURER,
        model=device.model or state.model_number,
        serial_number=device.serial,
        sw_version=state.firmware_version,
    )
    if device.mac:
        info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(device.mac))}
    return info


def unique_id(serial: str, key: str | None = None) -> str:
    """Stable unique_id; keys match the v1 suffixes after migration."""
    base = f"{UNIQUE_ID_PREFIX}_{serial}"
    return base if key is None else f"{base}_{key}"


class KumoEntity(CoordinatorEntity[KumoDeviceCoordinator]):
    """Entity bound to one unit's coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: KumoDeviceCoordinator, key: str | None = None) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique_id(coordinator.serial, key)
        self._attr_device_info = device_info(coordinator)

    @property
    def state_data(self) -> DeviceState:
        return self.coordinator.data

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.device_available


class KumoHubEntity(Entity):
    """Entity on the hub service device (cloud account level)."""

    _attr_has_entity_name = True

    def __init__(self, hub: KumoHub, key: str) -> None:
        entry_id = hub.entry.entry_id
        self.hub = hub
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_hub")},
            name=hub.entry.title,
            manufacturer=MANUFACTURER,
            model="Kumo Cloud",
            entry_type=DeviceEntryType.SERVICE,
        )


class KumoLocalEntity(KumoEntity):
    """Entity that only works over the local transport."""

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.link_is_local


@callback
def async_setup_units(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    make: Callable[[KumoDeviceCoordinator], Iterable[Entity]],
) -> None:
    """Add make(coordinator) entities for every unit now and for each new unit."""
    hub = entry.runtime_data

    @callback
    def _add(serial: str) -> None:
        async_add_entities(make(hub.coordinators[serial]))

    for serial in hub.coordinators:
        _add(serial)
    entry.async_on_unload(async_dispatcher_connect(hass, new_device_signal(entry.entry_id), _add))
