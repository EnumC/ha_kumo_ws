"""Number platform for Mitsubishi Comfort."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MitsubishiComfortCoordinator
from .pykumo2 import MitsubishiComfortClient

OFFSET_MIN_C = -5.0
OFFSET_MAX_C = 5.0
OFFSET_STEP_C = 0.5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Mitsubishi Comfort number entities."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: MitsubishiComfortCoordinator = stored["coordinator"]
    async_add_entities(
        entity
        for serial in coordinator.data
        for entity in _build_numbers(coordinator, stored["client"], serial)
    )


def _build_numbers(
    coordinator: MitsubishiComfortCoordinator,
    client: MitsubishiComfortClient,
    serial: str,
):
    yield MitsubishiComfortLocalTempCalibrationNumber(coordinator, client, serial)


class _BaseMitsubishiNumber(CoordinatorEntity[MitsubishiComfortCoordinator], NumberEntity):
    """Shared behavior for Mitsubishi Comfort number entities."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: MitsubishiComfortCoordinator,
        client: MitsubishiComfortClient,
        serial: str,
        name_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        device = coordinator.data.get(serial)
        base_name = device.name if device else serial
        self._attr_unique_id = f"mitsubishi_comfort_{serial}_{name_suffix}"
        self._attr_name = f"{base_name} {name_suffix.replace('_', ' ').title()}"

    @property
    def device(self):
        return self.coordinator.data.get(self._serial)

    @property
    def device_info(self):
        device = self.device
        if not device:
            return None
        return {
            "identifiers": {(DOMAIN, device.serial)},
            "name": device.name,
            "manufacturer": "Mitsubishi Electric",
            "model": device.model_number or device.raw.get("modelNumber"),
            "serial_number": device.serial_number or device.serial,
        }


class MitsubishiComfortLocalTempCalibrationNumber(_BaseMitsubishiNumber):
    """Expose local room temperature calibration."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE_DELTA
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = OFFSET_MIN_C
    _attr_native_max_value = OFFSET_MAX_C
    _attr_native_step = OFFSET_STEP_C
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: MitsubishiComfortCoordinator,
        client: MitsubishiComfortClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "local_temperature_calibration")

    @property
    def native_value(self):
        device = self.device
        return device.room_temp_offset if device else None

    async def async_set_native_value(self, value: float) -> None:
        device = self.device
        prior_sp_cool = device.sp_cool if device else None
        prior_sp_heat = device.sp_heat if device else None

        await self._client.async_set_room_temp_offset(self._serial, float(value))
        if device:
            device.room_temp_offset = float(value)
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))

        commands: dict[str, float] = {}
        if prior_sp_cool is not None:
            commands["spCool"] = prior_sp_cool
        if prior_sp_heat is not None:
            commands["spHeat"] = prior_sp_heat
        if not commands:
            return

        await self._client.async_send_command(self._serial, commands)
        if device:
            if "spCool" in commands:
                device.sp_cool = commands["spCool"]
            if "spHeat" in commands:
                device.sp_heat = commands["spHeat"]
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        self.coordinator.register_command_hold(
            self._serial,
            set(commands.keys()),
            duration=10.0,
        )
