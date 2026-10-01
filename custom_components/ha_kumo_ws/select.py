"""Select platform: which sensor the unit regulates on."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KumoDeviceCoordinator
from .entity import KumoLocalEntity, async_setup_units
from .hub import KumoConfigEntry
from .pykumo2.domain.commands import SetTempSource
from .pykumo2.domain.enums import SETTABLE_TEMP_SOURCES, TempSource

PARALLEL_UPDATES = 1
SENSOR_SLOTS = {
    TempSource.SENSOR0: 0,
    TempSource.SENSOR1: 1,
    TempSource.SENSOR2: 2,
    TempSource.SENSOR3: 3,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the temperature source select where local control exists."""
    if not entry.runtime_data.local_capable:
        return
    async_setup_units(hass, entry, async_add_entities, lambda c: [KumoTempSourceSelect(c)])


class KumoTempSourceSelect(KumoLocalEntity, SelectEntity):
    """Temperature source; sensor slots without a paired sensor are hidden."""

    _attr_translation_key = "temperature_source"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: KumoDeviceCoordinator) -> None:
        super().__init__(coordinator, "temperature_source")

    @property
    def current_option(self) -> str | None:
        source = self.state_data.temp_source
        return source.value if source in SETTABLE_TEMP_SOURCES else None

    @property
    def options(self) -> list[str]:
        paired = {s.index for s in self.state_data.sensors if s.uuid}
        current = self.state_data.temp_source
        return [
            source.value
            for source in TempSource
            if source in SETTABLE_TEMP_SOURCES
            and (source not in SENSOR_SLOTS or SENSOR_SLOTS[source] in paired or source is current)
        ]

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(SetTempSource(TempSource(option)))
