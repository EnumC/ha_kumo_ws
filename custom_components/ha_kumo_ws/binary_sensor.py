"""Binary sensor platform: unit flags, compressor and connectivity."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KumoDeviceCoordinator
from .entity import KumoEntity, async_setup_units
from .hub import KumoConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KumoBinarySensorDescription(BinarySensorEntityDescription):
    """Unit flag, optionally limited by setup method or CN105."""

    is_on_fn: Callable[[KumoDeviceCoordinator], bool | None]
    local_only: bool = False
    cloud_only: bool = False
    always_available: bool = False
    cn105: bool = False


BINARY_SENSORS: tuple[KumoBinarySensorDescription, ...] = (
    KumoBinarySensorDescription(
        key="filter_dirty",
        translation_key="filter_dirty",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: c.data.filter_dirty,
    ),
    KumoBinarySensorDescription(
        key="defrost",
        translation_key="defrost",
        is_on_fn=lambda c: c.data.defrost,
    ),
    KumoBinarySensorDescription(
        key="standby",
        translation_key="standby",
        is_on_fn=lambda c: c.data.standby,
    ),
    KumoBinarySensorDescription(
        key="compressor_running",
        translation_key="compressor_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on_fn=lambda c: None if c.data.cn105 is None else c.data.cn105.operating,
        local_only=True,
        cn105=True,
    ),
    KumoBinarySensorDescription(
        key="local_reachable",
        translation_key="local_reachable",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda c: c.link_is_local,
        local_only=True,
        always_available=True,
    ),
    KumoBinarySensorDescription(
        key="cloud_connected",
        translation_key="cloud_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda c: c.data.connected,
        cloud_only=True,
        always_available=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors that fit this setup method."""
    hub = entry.runtime_data
    descriptions = [
        d
        for d in BINARY_SENSORS
        if (hub.local_capable or not d.local_only)
        and (hub.has_cloud or not d.cloud_only)
        and (hub.cn105_enabled or not d.cn105)
    ]
    async_setup_units(
        hass,
        entry,
        async_add_entities,
        lambda c: [KumoBinarySensor(c, d) for d in descriptions],
    )


class KumoBinarySensor(KumoEntity, BinarySensorEntity):
    """Flag read from the unit's state or link."""

    entity_description: KumoBinarySensorDescription

    def __init__(
        self, coordinator: KumoDeviceCoordinator, description: KumoBinarySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.entity_description.always_available or super().available

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator)
