"""Sensor platform for Mitsubishi Comfort."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MitsubishiComfortCoordinator
from .pykumo2 import MitsubishiComfortClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Mitsubishi Comfort sensors."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: MitsubishiComfortCoordinator = stored["coordinator"]
    async_add_entities(
        entity
        for serial in coordinator.data
        for entity in _build_sensors(coordinator, stored["client"], serial)
    )


def _build_sensors(
    coordinator: MitsubishiComfortCoordinator,
    client: MitsubishiComfortClient,
    serial: str,
):
    yield MitsubishiComfortRssiSensor(coordinator, client, serial)
    yield MitsubishiComfortTwoFiguresCodeSensor(coordinator, client, serial)


class _BaseMitsubishiSensor(CoordinatorEntity[MitsubishiComfortCoordinator], SensorEntity):
    """Shared behavior for Mitsubishi Comfort sensors."""

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
        self._attr_name = f"{base_name} {name_suffix}"

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


class MitsubishiComfortRssiSensor(_BaseMitsubishiSensor):
    """Expose RSSI as a sensor."""

    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_icon = "mdi:wifi"

    def __init__(
        self,
        coordinator: MitsubishiComfortCoordinator,
        client: MitsubishiComfortClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "RSSI")

    @property
    def native_value(self):
        device = self.device
        return device.rssi if device else None


class MitsubishiComfortTwoFiguresCodeSensor(_BaseMitsubishiSensor):
    """Expose twoFiguresCode as a sensor."""

    _attr_icon = "mdi:numeric"

    def __init__(
        self,
        coordinator: MitsubishiComfortCoordinator,
        client: MitsubishiComfortClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator, client, serial, "Two-Figures Code")

    @property
    def native_value(self):
        device = self.device
        return device.two_figures_code if device else None
