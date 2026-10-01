"""Local unit API codec: commands to request bodies, responses to state values."""

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

from ..domain.capabilities import Capabilities
from ..domain.commands import (
    Batch,
    Command,
    InjectRoomTemp,
    RebootAdapter,
    SetFanSpeed,
    SetMode,
    SetPower,
    SetRoomTempOffset,
    SetSetpoints,
    SetTempSource,
    SetVane,
)
from ..domain.enums import FanSpeed, HvacMode, TempSource, VaneDirection, decode_operation_mode
from ..domain.state import DeviceState, WirelessSensor
from ..errors import CommandNotSupportedError, LocalProtocolError
from ..transport import Node
from ..util.coerce import as_bool, as_float, as_int, as_str, dig

POSSIBLE_SENSORS = 4

_QUERY_PATHS: dict[str, tuple[str, ...]] = {
    "status": ("indoorUnit", "status"),
    "profile": ("indoorUnit", "profile"),
    "adapter": ("adapter", "status"),
    "mhk2": ("mhk2", "status"),
}

_OVERLAY_KEYS = (
    "autoModePrevention",
    "userHasModeDry",
    "userHasModeHeat",
    "userMinCoolSetPoint",
    "userMaxHeatSetPoint",
)

_STATUS_BOOLS = {
    "filterDirty": "filter_dirty",
    "defrost": "defrost",
    "standby": "standby",
    "hotAdjust": "hot_adjust",
}


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def _nest(path: tuple[str, ...], leaf: Any) -> bytes:
    for key in reversed(path):
        leaf = {key: leaf}
    return _dumps({"c": leaf})


def _status(key: str, value: Any) -> bytes:
    return _nest(("indoorUnit", "status"), {key: value})


def _number(value: float) -> int | float:
    rounded = round(float(value), 1)
    return int(rounded) if rounded.is_integer() else rounded


class LocalCodec:
    """Encodes commands for, and decodes responses from, the local unit API."""

    def __init__(self, local_room_temp_offset: bool = False) -> None:
        self._local_room_temp_offset = local_room_temp_offset

    def supports(self, cmd: Command) -> bool:
        match cmd:
            case SetRoomTempOffset():
                return self._local_room_temp_offset
            case Batch(commands=commands):
                return all(self.supports(sub) for sub in commands)
        return True

    def encode(self, cmd: Command, state: DeviceState, caps: Capabilities) -> list[bytes]:
        """Request bodies for cmd, one key per body."""
        match cmd:
            case SetPower(on=False) | SetMode(mode=HvacMode.OFF):
                return [_status("mode", HvacMode.OFF.value)]
            case SetPower(on=True):
                return [_status("mode", _resume_mode(state, caps).value)]
            case SetMode(mode=mode):
                return [_status("mode", mode.value)]
            case SetSetpoints(heat=heat, cool=cool):
                bodies = []
                if heat is not None:
                    bodies.append(_status("spHeat", round(float(heat), 1)))
                if cool is not None:
                    bodies.append(_status("spCool", round(float(cool), 1)))
                return bodies
            case SetFanSpeed(speed=speed):
                label = "Low" if speed is FanSpeed.LOW and caps.raw_low_label else speed.value
                return [_status("fanSpeed", label)]
            case SetVane(direction=direction):
                return [_status("vaneDir", direction.value)]
            case SetTempSource(source=source):
                if not source.is_settable:
                    raise ValueError(f"temp source {source} is not settable")
                return [_status("tempSource", source.value)]
            case InjectRoomTemp(celsius=celsius):
                if state.temp_source is not TempSource.API:
                    raise CommandNotSupportedError("roomTemp needs tempSource=api")
                return [_status("roomTemp", round(float(celsius), 1))]
            case SetRoomTempOffset(celsius=celsius):
                if not self._local_room_temp_offset:
                    raise CommandNotSupportedError("local roomTempOffset is disabled")
                return [_nest(("adapter", "status"), {"roomTempOffset": _number(celsius)})]
            case RebootAdapter():
                return [_nest(("adapter", "status"), {"runState": "reboot"})]
            case Batch(commands=commands):
                bodies = []
                for sub in commands:
                    bodies.extend(self.encode(sub, state, caps))
                    state = _advance(state, sub)
                return bodies
        raise CommandNotSupportedError(f"unknown command: {cmd!r}")

    def query(self, node: Node, index: int = 0) -> bytes:
        """Read body for node; index selects the sensor slot."""
        if node == "sensors":
            if not 0 <= index < POSSIBLE_SENSORS:
                raise ValueError(f"sensor index out of range: {index}")
            return _nest(("sensors", str(index)), {})
        path = _QUERY_PATHS.get(node)
        if path is None:
            raise ValueError(f"node {node} has no JSON query")
        return _nest(path, {})

    def decode_status(self, response: Mapping[str, Any]) -> dict[str, Any]:
        """indoorUnit.status to DeviceState values."""
        status = _node(response, "indoorUnit", "status")
        values: dict[str, Any] = decode_operation_mode(status.get("mode"))
        for key, field in (("spHeat", "sp_heat"), ("spCool", "sp_cool"), ("roomTemp", "room_temp")):
            number = as_float(status.get(key))
            if number is not None:
                values[field] = number
        for key, field in _STATUS_BOOLS.items():
            flag = as_bool(status.get(key))
            if flag is not None:
                values[field] = flag
        parsed: dict[str, Any] = {
            "fan_speed": FanSpeed.parse(status.get("fanSpeed")),
            "vane": VaneDirection.parse(status.get("vaneDir")),
            "temp_source": TempSource.parse(status.get("tempSource")),
            "active_thermistor": TempSource.parse(status.get("activeThermistor")),
        }
        values.update({k: v for k, v in parsed.items() if v is not None})
        return values

    def decode_sensors(self, response: Mapping[str, Any]) -> dict[str, Any]:
        """sensors.N (one or more slots) to a sensors tuple; empty slots are skipped."""
        node = dig(response, "r", "sensors")
        if not isinstance(node, Mapping):
            raise LocalProtocolError("response has no sensors node")
        sensors: list[WirelessSensor] = []
        for index in range(POSSIBLE_SENSORS):
            raw = node.get(str(index))
            if not isinstance(raw, Mapping) or not raw.get("uuid"):
                continue
            sensors.append(
                WirelessSensor(
                    index=index,
                    uuid=as_str(raw.get("uuid")),
                    temperature=as_float(raw.get("temperature")),
                    humidity=as_float(raw.get("humidity")),
                    battery=as_int(raw.get("battery")),
                    rssi=as_int(raw.get("rssi")),
                    tx_power=as_int(raw.get("txPower")),
                )
            )
        return {"sensors": tuple(sensors)}

    def decode_profile(
        self, response: Mapping[str, Any], overlay: Mapping[str, Any] | None = None
    ) -> Capabilities:
        """indoorUnit.profile (plus adapter overlay) to Capabilities."""
        return Capabilities.from_profile(_node(response, "indoorUnit", "profile"), overlay)

    def decode_adapter(self, response: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """adapter.status to (state values, capabilities overlay). The password is dropped."""
        status = _node(response, "adapter", "status")
        values: dict[str, Any] = {}
        rssi = as_int(dig(status, "localNetwork", "stationMode", "RSSI"))
        if rssi is not None:
            values["wifi_rssi"] = rssi
        run_state = as_str(status.get("runState"))
        if run_state is not None:
            values["run_state"] = run_state
        offset = as_float(status.get("roomTempOffset"))
        if offset is not None:
            values["room_temp_offset"] = offset
        firmware = as_str(dig(response, "r", "adapter", "info", "firmwareVersion"))
        if firmware is not None:
            values["firmware_version"] = firmware
        overlay = {k: status[k] for k in _OVERLAY_KEYS if status.get(k) is not None}
        return values, overlay

    def decode_mhk2(self, response: Mapping[str, Any]) -> dict[str, Any]:
        """mhk2.status to values; empty when no MHK2 is attached."""
        humidity = as_float(dig(response, "r", "mhk2", "status", "indoorHumid"))
        return {} if humidity is None else {"mhk2_humidity": humidity}


def _node(response: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    node = dig(response, "r", *path)
    if not isinstance(node, Mapping):
        raise LocalProtocolError(f"response has no {'.'.join(path)} node")
    return node


def _resume_mode(state: DeviceState, caps: Capabilities) -> HvacMode:
    if state.mode is not None and state.mode is not HvacMode.OFF:
        return state.mode
    for mode in (HvacMode.HEAT, HvacMode.COOL):
        if mode in caps.hvac_modes:
            return mode
    return HvacMode.COOL


def _advance(state: DeviceState, cmd: Command) -> DeviceState:
    match cmd:
        case SetTempSource(source=source):
            return dataclasses.replace(state, temp_source=source)
        case SetMode(mode=mode) if mode is not HvacMode.OFF:
            return dataclasses.replace(state, mode=mode)
    return state
