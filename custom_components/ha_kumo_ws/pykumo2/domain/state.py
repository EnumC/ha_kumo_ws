"""Immutable device state and patch application."""

import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..transport import TransportKind
from .capabilities import Capabilities
from .enums import FanSpeed, HvacMode, LinkState, TempSource, VaneDirection
from .holds import HoldTable

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WirelessSensor:
    """A paired wireless (BLE) temperature/humidity sensor."""

    index: int
    uuid: str | None = None
    temperature: float | None = None
    humidity: float | None = None
    battery: int | None = None
    rssi: int | None = None
    tx_power: int | None = None


@dataclass(frozen=True, slots=True)
class Cn105Telemetry:
    """Fields read over the CN105 bus; None where the unit did not answer."""

    room_temperature: float | None = None
    outdoor_temperature: float | None = None
    compressor_runtime_minutes: int | None = None
    operating: bool | None = None
    compressor_frequency: int | None = None
    sub_mode: str | None = None
    stage: str | None = None
    auto_sub_mode: str | None = None
    read_at: float | None = None


@dataclass(frozen=True, slots=True)
class DeviceState:
    """Latest known state of one indoor unit. Temperatures are Celsius."""

    serial: str
    name: str | None = None
    model_number: str | None = None
    firmware_version: str | None = None
    power: bool | None = None
    mode: HvacMode | None = None
    auto_active: HvacMode | None = None
    sp_heat: float | None = None
    sp_cool: float | None = None
    fan_speed: FanSpeed | None = None
    vane: VaneDirection | None = None
    temp_source: TempSource | None = None
    room_temp: float | None = None
    humidity: float | None = None
    mhk2_humidity: float | None = None
    active_thermistor: TempSource | None = None
    filter_dirty: bool | None = None
    defrost: bool | None = None
    standby: bool | None = None
    hot_adjust: bool | None = None
    wifi_rssi: int | None = None
    run_state: str | None = None
    room_temp_offset: float | None = None
    error_code: str | None = None
    sensors: tuple[WirelessSensor, ...] = ()
    cn105: Cn105Telemetry | None = None
    connected: bool | None = None
    link_state: LinkState | None = None
    source: TransportKind | None = None
    updated_at: float | None = None
    command_capabilities: Capabilities | None = None

    @property
    def effective_mode(self) -> HvacMode | None:
        """OFF when powered off, else the operating mode."""
        return HvacMode.OFF if self.power is False else self.mode

    @property
    def effective_humidity(self) -> float | None:
        """Unit humidity, else the first wireless sensor, else the MHK2."""
        if self.humidity is not None:
            return self.humidity
        for sensor in self.sensors:
            if sensor.humidity is not None:
                return sensor.humidity
        return self.mhk2_humidity


_PATCHABLE = frozenset(f.name for f in dataclasses.fields(DeviceState)) - {
    "command_capabilities",
    "serial",
    "source",
    "updated_at",
}


@dataclass(frozen=True, slots=True)
class StatePatch:
    """Field values read from one transport at one time."""

    source: TransportKind
    values: Mapping[str, Any]
    at: float


def apply_patch(
    state: DeviceState,
    patch: StatePatch,
    holds: HoldTable | None,
    now: float,
    since: int | None = None,
) -> DeviceState:
    """Return state with patch applied; held keys keep their optimistic value."""
    unknown = patch.values.keys() - _PATCHABLE
    if unknown:
        _LOGGER.debug("Ignoring unknown state keys for %s: %s", state.serial, sorted(unknown))
    values = {k: v for k, v in patch.values.items() if k in _PATCHABLE}
    if holds is not None:
        values = holds.filter(state.serial, values, now, since)
    return dataclasses.replace(state, **values, source=patch.source, updated_at=patch.at)
