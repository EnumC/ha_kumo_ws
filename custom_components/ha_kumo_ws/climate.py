"""Climate platform: one entity per indoor unit, driven by Capabilities."""

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .action import unit_action
from .cn105_task import fresh_telemetry
from .const import CONF_TARGET_TEMP_STEP, DOMAIN, new_device_signal
from .coordinator import KumoDeviceCoordinator
from .entity import KumoEntity
from .hub import KumoConfigEntry
from .pykumo2.domain.capabilities import SetpointRange
from .pykumo2.domain.commands import (
    Batch,
    Command,
    SetFanSpeed,
    SetMode,
    SetPower,
    SetSetpoints,
    SetVane,
)
from .pykumo2.domain.enums import FanSpeed, HvacMode, VaneDirection
from .pykumo2.domain.state import DeviceState

PARALLEL_UPDATES = 0

TO_HA: dict[HvacMode, HVACMode] = {
    HvacMode.HEAT: HVACMode.HEAT,
    HvacMode.COOL: HVACMode.COOL,
    HvacMode.AUTO: HVACMode.HEAT_COOL,
    HvacMode.DRY: HVACMode.DRY,
    HvacMode.VENT: HVACMode.FAN_ONLY,
}
FROM_HA: dict[HVACMode, HvacMode] = {ha: mode for mode, ha in TO_HA.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one climate entity per unit, and for units found later."""
    hub = entry.runtime_data
    async_add_entities(KumoClimate(c) for c in hub.coordinators.values())

    @callback
    def _add(serial: str) -> None:
        async_add_entities([KumoClimate(hub.coordinators[serial])])

    entry.async_on_unload(async_dispatcher_connect(hass, new_device_signal(entry.entry_id), _add))


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def _mode_command(mode: HVACMode) -> Command:
    if mode == HVACMode.OFF:
        return SetPower(False)
    if (target := FROM_HA.get(mode)) is None:
        raise _invalid("unsupported_mode", mode=str(mode))
    return SetMode(target)


def _setpoints(
    mode: HvacMode | None, temp: float | None, low: float | None, high: float | None
) -> SetSetpoints:
    """Setpoint command for mode; never carries a value for the wrong side."""
    if mode is HvacMode.AUTO:
        if low is None and high is None:
            raise _invalid("range_required")
        return SetSetpoints(heat=low, cool=high)
    if mode is HvacMode.HEAT and (heat := low if temp is None else temp) is not None:
        return SetSetpoints(heat=heat)
    cool = high if temp is None else temp
    if mode in (HvacMode.COOL, HvacMode.DRY) and cool is not None:
        return SetSetpoints(cool=cool)
    raise _invalid("no_setpoint_for_mode", mode=str(mode))


class KumoClimate(KumoEntity, ClimateEntity):
    """Indoor unit; every write goes through the coordinator command queue."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: KumoDeviceCoordinator) -> None:
        super().__init__(coordinator)
        fahrenheit = coordinator.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        option = coordinator.config_entry.options.get(CONF_TARGET_TEMP_STEP, "auto")
        whole = option == "whole" or (option == "auto" and fahrenheit)
        self._attr_target_temperature_step = 1.0 if whole else 0.5
        if whole and fahrenheit:
            self._attr_precision = PRECISION_WHOLE

    @property
    def _modes(self) -> frozenset[HvacMode]:
        return self.coordinator.capabilities.hvac_modes

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode.OFF, *(ha for mode, ha in TO_HA.items() if mode in self._modes)]

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self.state_data.effective_mode
        if mode is HvacMode.OFF:
            return HVACMode.OFF
        return None if mode is None else TO_HA.get(mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        return unit_action(self.state_data, fresh_telemetry(self.coordinator))

    @property
    def supported_features(self) -> ClimateEntityFeature:
        caps = self.coordinator.capabilities
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if caps.hvac_modes & {HvacMode.HEAT, HvacMode.COOL, HvacMode.DRY}:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if HvacMode.AUTO in caps.hvac_modes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if caps.fan_speeds:
            features |= ClimateEntityFeature.FAN_MODE
        if caps.vane_directions:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def current_temperature(self) -> float | None:
        return self.state_data.room_temp

    @property
    def current_humidity(self) -> float | None:
        return self.state_data.effective_humidity

    @property
    def target_temperature(self) -> float | None:
        state = self.state_data
        if state.mode is HvacMode.HEAT:
            return state.sp_heat
        if state.mode is HvacMode.COOL or (
            state.mode is HvacMode.DRY and self.coordinator.capabilities.uses_setpoint_in_dry
        ):
            return state.sp_cool
        return None

    @property
    def target_temperature_low(self) -> float | None:
        state = self.state_data
        return state.sp_heat if state.mode is HvacMode.AUTO else None

    @property
    def target_temperature_high(self) -> float | None:
        state = self.state_data
        return state.sp_cool if state.mode is HvacMode.AUTO else None

    def _range(self) -> SetpointRange:
        caps, mode = self.coordinator.capabilities, self.state_data.mode
        if mode is HvacMode.HEAT:
            return caps.heat_range(mode)
        if mode in (HvacMode.COOL, HvacMode.DRY):
            return caps.cool_range(mode)
        if mode is HvacMode.AUTO:
            return SetpointRange(caps.heat_range(mode).low, caps.cool_range(mode).high)
        limits = caps.setpoints
        ranges = (limits.heat, limits.cool, limits.auto)
        return SetpointRange(min(r.low for r in ranges), max(r.high for r in ranges))

    @property
    def min_temp(self) -> float:
        return self._range().low

    @property
    def max_temp(self) -> float:
        return self._range().high

    @property
    def fan_modes(self) -> list[str]:
        return [speed.value for speed in self.coordinator.capabilities.fan_speeds]

    @property
    def fan_mode(self) -> str | None:
        speed = self.state_data.fan_speed
        return None if speed is None else speed.value

    @property
    def swing_modes(self) -> list[str]:
        return [vane.value for vane in self.coordinator.capabilities.vane_directions]

    @property
    def swing_mode(self) -> str | None:
        vane = self.state_data.vane
        return None if vane is None else vane.value

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_execute(_mode_command(hvac_mode))

    async def async_turn_on(self) -> None:
        """Power on in the last mode."""
        await self.coordinator.async_execute(SetPower(True))

    async def async_turn_off(self) -> None:
        await self.coordinator.async_execute(SetPower(False))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Setpoints for the target (or current) mode; a mode change goes first."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        hvac_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        if temp is None and low is None and high is None:
            if hvac_mode is not None:
                await self.async_set_hvac_mode(hvac_mode)
            return
        if hvac_mode is None:
            await self.coordinator.async_execute(
                lambda state: _setpoints(state.mode, temp, low, high),
                coalesce="set_temperature",
            )
            return
        mode_cmd = _mode_command(hvac_mode)

        def build(state: DeviceState) -> Command:
            target = state.mode if hvac_mode == HVACMode.OFF else FROM_HA[hvac_mode]
            return Batch((mode_cmd, _setpoints(target, temp, low, high)))

        await self.coordinator.async_execute(build)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if (speed := FanSpeed.parse(fan_mode)) is None:
            raise _invalid("unsupported_fan_mode", mode=fan_mode)
        await self.coordinator.async_execute(SetFanSpeed(speed))

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if (vane := VaneDirection.parse(swing_mode)) is None:
            raise _invalid("unsupported_swing_mode", mode=swing_mode)
        await self.coordinator.async_execute(SetVane(vane))
