"""Climate platform for Mitsubishi Comfort."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.event import async_call_later

from .pykumo2 import MitsubishiComfortClient

from .const import DOMAIN
from .coordinator import MitsubishiComfortCoordinator

_LOGGER = logging.getLogger(__name__)

HVAC_TO_API = {
    HVACMode.OFF: "off",
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.AUTO: "auto",
    HVACMode.DRY: "dry",
    HVACMode.FAN_ONLY: "vent",
}

API_TO_HVAC = {
    "off": HVACMode.OFF,
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "auto": HVACMode.AUTO,
    "autoHeat": HVACMode.AUTO,
    "autoCool": HVACMode.AUTO,
    "dry": HVACMode.DRY,
    "vent": HVACMode.FAN_ONLY,
}

FAN_MODES = ["superQuiet", "quiet", "low", "powerful", "superPowerful", "auto"]
SWING_MODES = [
    "auto",
    "horizontal",
    "midhorizontal",
    "midpoint",
    "midvertical",
    "vertical",
    "swing",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities from a config entry."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: MitsubishiComfortCoordinator = stored["coordinator"]
    async_add_entities(
        MitsubishiComfortClimateEntity(
            coordinator=coordinator,
            client=stored["client"],
            serial=serial,
        )
        for serial in coordinator.data
    )


class MitsubishiComfortClimateEntity(CoordinatorEntity[MitsubishiComfortCoordinator], ClimateEntity):
    """Representation of a Mitsubishi Comfort indoor unit."""

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.AUTO,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
    ]
    _attr_target_temperature_low = None
    _attr_target_temperature_high = None
    _attr_min_temp = 10.0
    _attr_max_temp = 32.0
    _attr_target_temperature_step = 0.5
    _attr_fan_modes = FAN_MODES
    _attr_swing_modes = SWING_MODES

    def __init__(
        self,
        coordinator: MitsubishiComfortCoordinator,
        client: MitsubishiComfortClient,
        serial: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._serial = serial
        device = coordinator.data.get(serial)
        name = device.name if device else serial
        self._attr_unique_id = f"mitsubishi_comfort_{serial}"
        self._attr_name = name

    @property
    def device(self):
        return self.coordinator.data.get(self._serial)

    @property
    def available(self) -> bool:
        device = self.device
        return device.connected if device else False

    @property
    def hvac_action(self) -> HVACAction | None:
        device = self.device
        if not device or not device.power:
            return HVACAction.OFF
        if device.display_config:
            if device.display_config.get("defrost"):
                return HVACAction.DEFROSTING
            if device.display_config.get("standby"):
                return HVACAction.IDLE
        if device.operation_mode in ("cool", "autoCool"):
            return HVACAction.COOLING
        if device.operation_mode in ("heat", "autoHeat"):
            return HVACAction.HEATING
        if device.operation_mode == "dry":
            return HVACAction.DRYING
        if device.operation_mode == "vent":
            return HVACAction.FAN
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        device = self.device
        return device.room_temp if device else None

    @property
    def target_temperature(self) -> float | None:
        device = self.device
        if self.hvac_mode == HVACMode.AUTO:
            return None
        return device.target_temperature() if device else None

    @property
    def target_temperature_low(self) -> float | None:
        device = self.device
        if not device:
            return None
        if self.hvac_mode == HVACMode.AUTO:
            return device.sp_heat
        return None

    @property
    def target_temperature_high(self) -> float | None:
        device = self.device
        if not device:
            return None
        if self.hvac_mode == HVACMode.AUTO:
            return device.sp_cool
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        device = self.device
        if not device:
            return None
        if not device.power:
            return HVACMode.OFF
        return API_TO_HVAC.get(device.operation_mode, HVACMode.AUTO)

    @property
    def fan_mode(self) -> str | None:
        device = self.device
        if device and device.fan_speed in FAN_MODES:
            return device.fan_speed
        return "auto"

    @property
    def supported_features(self) -> int:
        base = ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.SWING_MODE
        if self.hvac_mode == HVACMode.AUTO:
            return base | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return base | ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def swing_mode(self) -> str | None:
        device = self.device
        if device and device.air_direction in SWING_MODES:
            return device.air_direction
        return "auto"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.device
        if not device:
            return {}
        return {
            "serial": device.serial,
            "serial_number": device.serial_number,
            "humidity": device.humidity,
            "sp_cool": device.sp_cool,
            "sp_heat": device.sp_heat,
            "schedule_owner": device.schedule_owner,
            "rssi": device.rssi,
            "two_figures_code": device.two_figures_code,
            "defrost": device.display_config.get("defrost") if device.display_config else None,
            "standby": device.display_config.get("standby") if device.display_config else None,
        }

    @property
    def device_info(self) -> dict[str, Any] | None:
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

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature (Celsius) and optionally HVAC mode."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        temp_low = kwargs.get("target_temp_low")
        temp_high = kwargs.get("target_temp_high")
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if temperature is None and temp_low is None and temp_high is None:
            return

        device = self.device
        # If explicit range temps are provided, default to auto mode
        if hvac_mode is None and (temp_low is not None or temp_high is not None):
            hvac_mode = HVACMode.AUTO

        api_mode = HVAC_TO_API.get(hvac_mode or (device.operation_mode if device else None))
        commands: dict[str, Any] = {"power": 1}

        if api_mode == "cool":
            commands["spCool"] = temperature
            commands["operationMode"] = "cool"
        elif api_mode == "heat":
            commands["spHeat"] = temperature
            commands["operationMode"] = "heat"
        elif api_mode == "auto":
            if temp_high is not None:
                commands["spCool"] = temp_high
            elif temperature is not None:
                commands["spCool"] = temperature
            if temp_low is not None:
                commands["spHeat"] = temp_low
            elif temperature is not None:
                commands["spHeat"] = temperature
            commands["operationMode"] = "auto"
        elif api_mode == "dry":
            commands["operationMode"] = "dry"
        elif api_mode == "vent":
            commands["operationMode"] = "vent"
        else:
            if temperature is not None:
                commands["spCool"] = temperature
                commands["spHeat"] = temperature

        await self._client.async_send_command(self._serial, commands)

        # Optimistically update in-memory state to avoid UI flicker
        if device:
            if "spCool" in commands:
                device.sp_cool = commands["spCool"]
            if "spHeat" in commands:
                device.sp_heat = commands["spHeat"]
            if "operationMode" in commands:
                device.operation_mode = commands["operationMode"]
            device.power = True
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))

        # Protect setpoints/mode from stale socket updates for a short window
        self.coordinator.register_command_hold(
            self._serial,
            {"spCool", "spHeat", "operationMode", "power"},
            duration=10.0,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        api_mode = HVAC_TO_API[hvac_mode]
        commands = {"operationMode": api_mode, "power": 0 if hvac_mode == HVACMode.OFF else 1}
        await self._client.async_send_command(self._serial, commands)
        device = self.device
        if device:
            device.operation_mode = api_mode
            device.power = commands["power"] == 1
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        self.coordinator.register_command_hold(
            self._serial,
            {"operationMode", "power"},
            duration=10.0,
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if not fan_mode:
            _LOGGER.warning("Ignoring invalid fan mode request: %s", fan_mode)
            return
        fan_mode = fan_mode if fan_mode in FAN_MODES else fan_mode.lower()
        if fan_mode not in FAN_MODES:
            _LOGGER.warning("Ignoring invalid fan mode request: %s", fan_mode)
            return
        await self._client.async_send_command(self._serial, {"fanSpeed": fan_mode})
        # Optimistic update to avoid snap-back while waiting for socket
        device = self.device
        if device:
            device.fan_speed = fan_mode
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        self.coordinator.register_command_hold(
            self._serial,
            {"fanSpeed"},
            duration=10.0,
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if not swing_mode:
            _LOGGER.warning("Ignoring invalid swing mode request: %s", swing_mode)
            return
        swing_mode = swing_mode if swing_mode in SWING_MODES else swing_mode.lower()
        if swing_mode not in SWING_MODES:
            _LOGGER.warning("Ignoring invalid swing mode request: %s", swing_mode)
            return
        await self._client.async_send_command(self._serial, {"airDirection": swing_mode})
        device = self.device
        if device:
            device.air_direction = swing_mode
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        self.coordinator.register_command_hold(
            self._serial,
            {"airDirection"},
            duration=10.0,
        )
