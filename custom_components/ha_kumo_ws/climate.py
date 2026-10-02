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
from .smart_control import SMART_FAN_MODE, SmartController

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
    _attr_translation_key = "kumo"
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
    def _controller(self) -> SmartController | None:
        return self.coordinator.hub.controllers.get(self.coordinator.serial)

    @property
    def _parked_mode(self) -> HvacMode | None:
        controller = self._controller
        return None if controller is None else controller.parked_mode

    @property
    def _mode(self) -> HvacMode | None:
        """Parked mode while parked, else the unit mode."""
        return self._parked_mode or self.state_data.mode

    @callback
    def _cancel_park(self) -> None:
        if (controller := self._controller) is not None:
            controller.async_cancel()
        elif self.coordinator.hub.park_records.pop(self.coordinator.serial) is not None:
            self.coordinator.async_update_listeners()

    def _validated(self, mode: HVACMode) -> Command:
        command = _mode_command(mode)
        if isinstance(command, SetMode) and command.mode not in self._modes:
            raise _invalid("unsupported_mode", mode=str(mode))
        return command

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode.OFF, *(ha for mode, ha in TO_HA.items() if mode in self._modes)]

    @property
    def hvac_mode(self) -> HVACMode | None:
        if (parked := self._parked_mode) is not None:
            return TO_HA[parked]
        mode = self.state_data.effective_mode
        if mode is HvacMode.OFF:
            return HVACMode.OFF
        return None if mode is None else TO_HA.get(mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        if self._parked_mode is not None:
            return HVACAction.IDLE
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
        state, mode = self.state_data, self._mode
        if mode is HvacMode.HEAT:
            return state.sp_heat
        if mode is HvacMode.COOL or (
            mode is HvacMode.DRY and self.coordinator.capabilities.uses_setpoint_in_dry
        ):
            return state.sp_cool
        return None

    @property
    def target_temperature_low(self) -> float | None:
        return self.state_data.sp_heat if self._mode is HvacMode.AUTO else None

    @property
    def target_temperature_high(self) -> float | None:
        return self.state_data.sp_cool if self._mode is HvacMode.AUTO else None

    def _range(self) -> SetpointRange:
        caps, mode = self.coordinator.capabilities, self._mode
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
        modes = [speed.value for speed in self.coordinator.capabilities.fan_speeds]
        if (controller := self._controller) is not None and controller.options.smart_fan:
            modes.append(SMART_FAN_MODE)
        return modes

    @property
    def fan_mode(self) -> str | None:
        if (controller := self._controller) is not None and controller.smart_fan_active:
            return SMART_FAN_MODE
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
        """The parked mode keeps the unit parked; any other mode stops parking."""
        command = self._validated(hvac_mode)
        if (parked := self._parked_mode) is not None:
            if FROM_HA.get(hvac_mode) is parked:
                return
            self._cancel_park()
        await self.coordinator.async_execute(command)

    async def async_turn_on(self) -> None:
        """Power on in the last mode; a parked unit resumes in its parked mode."""
        if (controller := self._controller) is not None and controller.parked_mode is not None:
            await controller.async_resume()
            return
        await self.coordinator.async_execute(SetPower(True))

    async def async_turn_off(self) -> None:
        self._cancel_park()
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
        parked = self._parked_mode
        if parked is not None and hvac_mode is not None and FROM_HA.get(hvac_mode) is parked:
            hvac_mode = None
        if hvac_mode is None:
            await self.coordinator.async_execute(
                lambda state: _setpoints(self._parked_mode or state.mode, temp, low, high),
                coalesce="set_temperature",
            )
            return
        mode_cmd = self._validated(hvac_mode)
        self._cancel_park()

        def build(state: DeviceState) -> Command:
            target = (parked or state.mode) if hvac_mode == HVACMode.OFF else FROM_HA[hvac_mode]
            return Batch((mode_cmd, _setpoints(target, temp, low, high)))

        await self.coordinator.async_execute(build)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Smart hands the speed to the controller; any other speed hands it back."""
        controller = self._controller
        if fan_mode == SMART_FAN_MODE and controller is not None and controller.options.smart_fan:
            await controller.async_set_smart_fan(True)
            return
        if fan_mode == SMART_FAN_MODE or (speed := FanSpeed.parse(fan_mode)) is None:
            raise _invalid("unsupported_fan_mode", mode=fan_mode)
        if controller is not None and controller.smart_fan_selected:
            await controller.async_set_smart_fan(False)
        await self.coordinator.async_execute(SetFanSpeed(speed))

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if (vane := VaneDirection.parse(swing_mode)) is None:
            raise _invalid("unsupported_swing_mode", mode=swing_mode)
        await self.coordinator.async_execute(SetVane(vane))
