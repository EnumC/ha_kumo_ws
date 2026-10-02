"""HVAC action from unit status and CN105 telemetry."""

from homeassistant.components.climate.const import HVACAction

from .pykumo2.domain.enums import HvacMode
from .pykumo2.domain.state import Cn105Telemetry, DeviceState

_ACTIONS = {
    HvacMode.HEAT: HVACAction.HEATING,
    HvacMode.COOL: HVACAction.COOLING,
    HvacMode.DRY: HVACAction.DRYING,
    HvacMode.VENT: HVACAction.FAN,
}
_AUTO_SUB_MODES = {"AUTO_COOL": HvacMode.COOL, "AUTO_HEAT": HvacMode.HEAT}


def unit_action(state: DeviceState, telemetry: Cn105Telemetry | None) -> HVACAction | None:
    """Current action from real unit states; telemetry is fresh CN105 data or None."""
    if state.power is None:
        return None
    if not state.power:
        return HVACAction.OFF
    sub_mode = None if telemetry is None else telemetry.sub_mode
    if state.defrost or sub_mode == "DEFROST":
        return HVACAction.DEFROSTING
    if state.hot_adjust or sub_mode in ("PREHEAT", "WARMUP"):
        return HVACAction.PREHEATING
    if state.mode is HvacMode.VENT:
        return HVACAction.FAN
    if (
        state.standby
        or sub_mode in ("STANDBY", "OFF")
        or (telemetry is not None and telemetry.operating is False)
    ):
        return HVACAction.IDLE
    mode: HvacMode | None = state.mode
    if mode is HvacMode.AUTO:
        auto_sub_mode = None if telemetry is None else telemetry.auto_sub_mode
        mode = state.auto_active or _AUTO_SUB_MODES.get(auto_sub_mode or "")
    return None if mode is None else _ACTIONS.get(mode)
