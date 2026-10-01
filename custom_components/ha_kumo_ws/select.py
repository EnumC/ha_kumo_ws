"""Select platform: which sensor the unit regulates on."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_RT_ENTITY, DOMAIN
from .coordinator import KumoDeviceCoordinator
from .entity import KumoLocalEntity, async_setup_units
from .hub import KumoConfigEntry
from .pykumo2.domain.commands import SetTempSource
from .pykumo2.domain.enums import TempSource

PARALLEL_UPDATES = 1
API_SETUP_REQUIRED = "api_setup_required"
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
    """Temperature source; unpaired slots and unset are hidden unless current."""

    _attr_translation_key = "temperature_source"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: KumoDeviceCoordinator) -> None:
        super().__init__(coordinator, "temperature_source")

    @property
    def _api_mapped(self) -> bool:
        coordinator = self.coordinator
        return bool(coordinator.hub.remote_temp_mapping(coordinator.serial).get(CONF_RT_ENTITY))

    @property
    def current_option(self) -> str | None:
        source = self.state_data.temp_source
        return None if source is None else source.value

    @property
    def options(self) -> list[str]:
        paired = {s.index for s in self.state_data.sensors if s.uuid}
        current = self.state_data.temp_source
        options = []
        for source in TempSource:
            if source is TempSource.API and not (self._api_mapped or source is current):
                options.append(API_SETUP_REQUIRED)
            elif source is current or (
                source.is_settable
                and (source not in SENSOR_SLOTS or SENSOR_SLOTS[source] in paired)
            ):
                options.append(source.value)
        return options

    async def async_select_option(self, option: str) -> None:
        if option in (API_SETUP_REQUIRED, TempSource.API) and not self._api_mapped:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=API_SETUP_REQUIRED)
        source = TempSource(option)
        if not source.is_settable:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="temp_source_not_settable"
            )
        await self.coordinator.async_execute(SetTempSource(source))
