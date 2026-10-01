"""Number platform: room temperature offset (calibration)."""

from collections.abc import Callable

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import new_device_signal
from .coordinator import KumoDeviceCoordinator
from .entity import KumoEntity
from .hub import KumoConfigEntry
from .pykumo2.domain.commands import (
    OFFSET_MAX_C,
    OFFSET_MIN_C,
    Batch,
    Command,
    SetRoomTempOffset,
    SetSetpoints,
)
from .pykumo2.domain.state import DeviceState

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the offset entity where a transport can write it."""
    hub = entry.runtime_data
    if not hub.supports_room_temp_offset:
        return
    async_add_entities(KumoRoomTempOffset(c) for c in hub.coordinators.values())

    @callback
    def _add(serial: str) -> None:
        async_add_entities([KumoRoomTempOffset(hub.coordinators[serial])])

    entry.async_on_unload(async_dispatcher_connect(hass, new_device_signal(entry.entry_id), _add))


def offset_command(value: float) -> Callable[[DeviceState], Command]:
    """Offset plus a setpoint re-send (the unit otherwise shifts them), as one batch."""

    def build(state: DeviceState) -> Command:
        offset = SetRoomTempOffset(value)
        if state.sp_heat is None and state.sp_cool is None:
            return offset
        return Batch((offset, SetSetpoints(heat=state.sp_heat, cool=state.sp_cool)))

    return build


class KumoRoomTempOffset(KumoEntity, NumberEntity):
    """Room temperature offset; v1 unique_id kept."""

    _attr_translation_key = "local_temperature_calibration"
    _attr_device_class = NumberDeviceClass.TEMPERATURE_DELTA
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = OFFSET_MIN_C
    _attr_native_max_value = OFFSET_MAX_C
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: KumoDeviceCoordinator) -> None:
        super().__init__(coordinator, "local_temperature_calibration")

    @property
    def native_value(self) -> float | None:
        return self.state_data.room_temp_offset

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_execute(offset_command(value))
