"""Transport-neutral commands and their validation."""

import dataclasses
import math
from dataclasses import dataclass
from typing import Any

from .capabilities import Capabilities, SetpointRange
from .enums import FanSpeed, HvacMode, TempSource, VaneDirection
from .state import DeviceState

INJECT_MIN_C = -10.0
INJECT_MAX_C = 50.0
OFFSET_MIN_C = -5.0
OFFSET_MAX_C = 5.0


@dataclass(frozen=True, slots=True)
class SetPower:
    on: bool


@dataclass(frozen=True, slots=True)
class SetMode:
    mode: HvacMode


@dataclass(frozen=True, slots=True)
class SetSetpoints:
    heat: float | None = None
    cool: float | None = None


@dataclass(frozen=True, slots=True)
class SetFanSpeed:
    speed: FanSpeed


@dataclass(frozen=True, slots=True)
class SetVane:
    direction: VaneDirection


@dataclass(frozen=True, slots=True)
class SetTempSource:
    source: TempSource


@dataclass(frozen=True, slots=True)
class InjectRoomTemp:
    celsius: float


@dataclass(frozen=True, slots=True)
class SetRoomTempOffset:
    celsius: float


@dataclass(frozen=True, slots=True)
class RebootAdapter:
    pass


@dataclass(frozen=True, slots=True)
class Batch:
    commands: tuple["Command", ...]


type Command = (
    SetPower
    | SetMode
    | SetSetpoints
    | SetFanSpeed
    | SetVane
    | SetTempSource
    | InjectRoomTemp
    | SetRoomTempOffset
    | RebootAdapter
    | Batch
)


def round_half(value: float) -> float:
    """Round to the nearest 0.5, halves up."""
    return math.floor(value * 2 + 0.5) / 2


def _finite(value: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return float(value)


def _setpoint(value: float, rng: SetpointRange) -> float:
    return rng.clamp(round_half(rng.clamp(_finite(value, "setpoint"))))


class CommandValidator:
    """Checks commands against capabilities and normalizes their values."""

    def validate(self, cmd: Command, caps: Capabilities, state: DeviceState) -> Command:
        """Return a normalized command or raise ValueError."""
        match cmd:
            case SetMode(mode=mode):
                if mode not in caps.hvac_modes:
                    raise ValueError(f"unsupported mode: {mode}")
                return cmd
            case SetSetpoints():
                return self._setpoints(cmd, caps, state)
            case SetFanSpeed(speed=speed):
                if speed not in caps.fan_speeds:
                    raise ValueError(f"unsupported fan speed: {speed}")
                return cmd
            case SetVane(direction=direction):
                if direction not in caps.vane_directions:
                    raise ValueError(f"unsupported vane direction: {direction}")
                return cmd
            case SetTempSource(source=source):
                if not source.is_settable:
                    raise ValueError(f"temp source {source} is not settable")
                return cmd
            case InjectRoomTemp(celsius=celsius):
                value = _finite(celsius, "room temperature")
                if not INJECT_MIN_C <= value <= INJECT_MAX_C:
                    raise ValueError(f"room temperature out of range: {value}")
                return InjectRoomTemp(round(value, 1))
            case SetRoomTempOffset(celsius=celsius):
                value = _finite(celsius, "offset")
                if not OFFSET_MIN_C <= value <= OFFSET_MAX_C:
                    raise ValueError(f"offset out of range: {value}")
                return SetRoomTempOffset(round_half(value))
            case Batch(commands=commands):
                out: list[Command] = []
                for sub in commands:
                    validated = self.validate(sub, caps, state)
                    out.append(validated)
                    state = _with_values(state, optimistic_values(validated, state))
                return Batch(tuple(out))
        return cmd

    def _setpoints(self, cmd: SetSetpoints, caps: Capabilities, state: DeviceState) -> Command:
        mode = state.mode
        heat, cool = cmd.heat, cmd.cool
        if heat is None and cool is None:
            raise ValueError("no setpoint given")
        if mode in (HvacMode.HEAT, HvacMode.AUTO) and heat is None:
            heat = state.sp_heat
        if mode in (HvacMode.COOL, HvacMode.AUTO) and cool is None:
            cool = state.sp_cool
        if mode in (HvacMode.HEAT, HvacMode.AUTO) and heat is None:
            raise ValueError(f"{mode} mode needs a heat setpoint")
        if mode in (HvacMode.COOL, HvacMode.AUTO) and cool is None:
            raise ValueError(f"{mode} mode needs a cool setpoint")
        return SetSetpoints(
            heat=None if heat is None else _setpoint(heat, caps.heat_range(mode)),
            cool=None if cool is None else _setpoint(cool, caps.cool_range(mode)),
        )


def _with_values(state: DeviceState, values: dict[str, Any]) -> DeviceState:
    return dataclasses.replace(state, **values)


def optimistic_values(cmd: Command, state: DeviceState) -> dict[str, Any]:
    """DeviceState field changes expected once cmd applies (for the hold table)."""
    match cmd:
        case SetPower(on=on):
            return {"power": on}
        case SetMode(mode=HvacMode.OFF):
            return {"power": False}
        case SetMode(mode=mode):
            return {"power": True, "mode": mode}
        case SetSetpoints(heat=heat, cool=cool):
            values: dict[str, Any] = {}
            if heat is not None:
                values["sp_heat"] = heat
            if cool is not None:
                values["sp_cool"] = cool
            return values
        case SetFanSpeed(speed=speed):
            return {"fan_speed": speed}
        case SetVane(direction=direction):
            return {"vane": direction}
        case SetTempSource(source=source):
            return {"temp_source": source}
        case InjectRoomTemp(celsius=celsius):
            return {"room_temp": celsius}
        case SetRoomTempOffset(celsius=celsius):
            return {"room_temp_offset": celsius}
        case Batch(commands=commands):
            merged: dict[str, Any] = {}
            for sub in commands:
                merged.update(optimistic_values(sub, _with_values(state, merged)))
            return merged
    return {}
