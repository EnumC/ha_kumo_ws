"""Kumo Cloud codec: commands to send-command/relay bodies, payloads to state values."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

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
from ..domain.state import DeviceState
from ..errors import CommandNotSupportedError
from ..util.coerce import as_bool, as_float, as_int, as_str

_LOGGER = logging.getLogger(__name__)

_LOCAL_ONLY = (SetTempSource, InjectRoomTemp, RebootAdapter)

_FLOATS = {
    "spHeat": "sp_heat",
    "spCool": "sp_cool",
    "roomTemp": "room_temp",
    "humidity": "humidity",
    "roomTempOffset": "room_temp_offset",
    "roomTempDisplayOffset": "room_temp_offset",
}
_INTS = {"rssi": "wifi_rssi", "routerRssi": "wifi_rssi"}
_STRS = {
    "twoFiguresCode": "error_code",
    "modelNumber": "model_number",
    "firmwareVersion": "firmware_version",
}
_DISPLAY = {
    "filter": "filter_dirty",
    "defrost": "defrost",
    "standby": "standby",
    "hotAdjust": "hot_adjust",
}
# Adapter settings that mask the profile; cloud names for the local adapter keys.
_OVERLAY_KEYS = (
    "autoModeDisable",
    "modeDry",
    "modeHeat",
    "minSetpoint",
    "maxSetpoint",
    "minSetPoint",
    "maxSetPoint",
)
_IGNORED_EVENTS = frozenset({"acoil_update", "profile_update"})


@dataclass(frozen=True, slots=True)
class CloudRequest:
    """One REST call: send-command or relay-command."""

    kind: Literal["send", "relay"]
    body: dict[str, Any]


class CloudCodec:
    """Encodes commands for, and decodes payloads from, Kumo Cloud v3."""

    def supports(self, cmd: Command) -> bool:
        if isinstance(cmd, Batch):
            return all(self.supports(sub) for sub in cmd.commands)
        return not isinstance(cmd, _LOCAL_ONLY)

    def encode(self, cmd: Command, state: DeviceState) -> list[CloudRequest]:
        """REST requests for cmd; consecutive send-commands in a batch are merged."""
        serial = state.serial
        if isinstance(cmd, Batch):
            requests: list[CloudRequest] = []
            for sub in cmd.commands:
                for request in self.encode(sub, state):
                    last = requests[-1] if requests else None
                    if request.kind == "send" and last is not None and last.kind == "send":
                        last.body["commands"].update(request.body["commands"])
                    else:
                        requests.append(request)
            return requests
        if isinstance(cmd, SetRoomTempOffset):
            relay = {"roomTempOffset": cmd.celsius}
            return [CloudRequest("relay", {"serial": serial, "adapter": {"status": relay}})]
        commands = _send_commands(cmd, state)
        return [CloudRequest("send", {"deviceSerial": serial, "commands": commands})]

    def decode_event(
        self, event: str, payload: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """Socket event to (serial, values); None for events with no state values."""
        serial = as_str(payload.get("deviceSerial"))
        if serial is None or event in _IGNORED_EVENTS:
            return None
        match event:
            case "device_update":
                return serial, self.decode_device(payload)
            case "device_status_v2":
                status = as_str(payload.get("status"))
                return serial, {} if status is None else {"connected": status == "connected"}
            case "adapter_update":
                return serial, self.decode_adapter(payload)[0]
        _LOGGER.debug("Ignoring socket event %s", event)
        return None

    def decode_device(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Device fields shared by device_update, /devices/{s} and zone adapters."""
        values: dict[str, Any] = {}
        mode = decode_operation_mode(payload.get("operationMode"))
        power = as_bool(payload.get("power"))
        if power is False:
            mode["power"] = False
        elif power is True and mode.get("power") is not False:
            mode["power"] = True
        values.update(mode)
        values.update(_scalars(payload))
        parsed: dict[str, Any] = {
            "fan_speed": FanSpeed.parse(payload.get("fanSpeed")),
            "vane": VaneDirection.parse(payload.get("airDirection")),
            "temp_source": TempSource.parse(payload.get("tempSource")),
            "active_thermistor": TempSource.parse(payload.get("activeThermistor")),
            "connected": as_bool(payload.get("connected")),
        }
        values.update({k: v for k, v in parsed.items() if v is not None})
        display = payload.get("displayConfig")
        if isinstance(display, Mapping):
            for key, field in _DISPLAY.items():
                flag = as_bool(display.get(key))
                if flag is not None:
                    values[field] = flag
        return values

    def decode_adapter(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """adapter_update or /devices/{s}/status to (values, overlay). Secrets are dropped."""
        values = _scalars(payload)
        name = as_str(payload.get("zoneName"))
        if name is not None:
            values["name"] = name
        overlay = {k: payload[k] for k in _OVERLAY_KEYS if payload.get(k) is not None}
        return values, overlay

    def decode_profile(
        self,
        payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        overlay: Mapping[str, Any] | None = None,
    ) -> Capabilities | None:
        """profile_update or /devices/{s}/profile (a one-item list) to Capabilities."""
        if isinstance(payload, Sequence):
            payload = payload[0] if payload else {}
        if not isinstance(payload, Mapping) or not payload:
            return None
        return Capabilities.from_profile(payload, overlay)

    def decode_zone(self, zone: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """/v3/sites/{id}/zones item to (serial, values)."""
        adapter = zone.get("adapter")
        if not isinstance(adapter, Mapping):
            return None
        serial = as_str(adapter.get("deviceSerial"))
        if serial is None:
            return None
        values = self.decode_device(adapter)
        name = as_str(zone.get("name"))
        if name is not None:
            values["name"] = name
        return serial, values


def _send_commands(cmd: Command, state: DeviceState) -> dict[str, Any]:
    match cmd:
        case SetPower(on=False) | SetMode(mode=HvacMode.OFF):
            return {"power": 0}
        case SetPower(on=True):
            # Re-send the last mode like the app; bare power=1 is a fallback.
            if state.mode is not None and state.mode is not HvacMode.OFF:
                return {"power": 1, "operationMode": state.mode.value}
            return {"power": 1}
        case SetMode(mode=mode):
            return {"power": 1, "operationMode": mode.value}
        case SetSetpoints(heat=heat, cool=cool):
            commands: dict[str, Any] = {}
            if heat is not None:
                commands["spHeat"] = heat
            if cool is not None:
                commands["spCool"] = cool
            return commands
        case SetFanSpeed(speed=speed):
            return {"fanSpeed": speed.value}
        case SetVane(direction=direction):
            return {"airDirection": direction.value}
    raise CommandNotSupportedError(f"{type(cmd).__name__} is local only")


def _scalars(payload: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, field in _FLOATS.items():
        number = as_float(payload.get(key))
        if number is not None:
            values[field] = number
    for key, field in _INTS.items():
        integer = as_int(payload.get(key))
        if integer is not None:
            values[field] = integer
    for key, field in _STRS.items():
        text = as_str(payload.get(key))
        if text is not None:
            values[field] = text
    return values
