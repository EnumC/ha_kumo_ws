"""SmartController: fan speed from demand, and automatic off past the setpoint with resume."""

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Self

from homeassistant.components.climate.const import HVACAction
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .action import unit_action
from .cn105_task import fresh_telemetry
from .const import (
    CONF_FP_DWELL,
    CONF_FP_ENABLED,
    CONF_FP_FAN_HOLD,
    CONF_FP_FULL_AT,
    CONF_FP_HYSTERESIS,
    CONF_FP_IDLE_DWELL,
    CONF_FP_MARGIN,
    CONF_FP_MIN_OFF,
    CONF_FP_MIN_ON,
    CONF_FP_OFF_MARGIN,
    CONF_FP_SMART_FAN,
    DOMAIN,
    FP_LIMITS,
)
from .coordinator import KumoDeviceCoordinator
from .pykumo2.domain.commands import SetFanSpeed, SetMode, SetPower
from .pykumo2.domain.enums import FanSpeed, HvacMode
from .pykumo2.domain.state import DeviceState
from .remote_temp import state_celsius

_LOGGER = logging.getLogger(__name__)

SMART_FAN_MODE = "dynamic"
SETTLE_S = 30.0
EVAL_S = 30
STALE_S = 900.0
PARK_RETRY_S = 300.0
RESUME_RETRY_S = 60.0
PARK_MODES = frozenset({HvacMode.HEAT, HvacMode.COOL, HvacMode.AUTO})
STORAGE_VERSION = 2
_UNIT_MANAGED = frozenset({HVACAction.DEFROSTING, HVACAction.PREHEATING})


def _default(key: str) -> float:
    return float(FP_LIMITS[key][0])


@dataclass(frozen=True, slots=True)
class ControlOptions:
    """Per-unit tunables; temperatures in C, durations in seconds."""

    smart_fan: bool = False
    full_speed_at: float = _default(CONF_FP_FULL_AT)
    fan_hysteresis: float = _default(CONF_FP_HYSTERESIS)
    fan_hold: float = _default(CONF_FP_FAN_HOLD)
    auto_off: bool = False
    off_margin: float = _default(CONF_FP_OFF_MARGIN)
    restart_margin: float = _default(CONF_FP_MARGIN)
    dwell: float = _default(CONF_FP_DWELL)
    idle_dwell: float = _default(CONF_FP_IDLE_DWELL)
    min_on: float = _default(CONF_FP_MIN_ON)
    min_off: float = _default(CONF_FP_MIN_OFF)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Self:
        """Stored unit options; missing keys use the defaults."""

        def value(key: str) -> float:
            return float(options.get(key, FP_LIMITS[key][0]))

        return cls(
            smart_fan=bool(options.get(CONF_FP_SMART_FAN)),
            full_speed_at=value(CONF_FP_FULL_AT),
            fan_hysteresis=value(CONF_FP_HYSTERESIS),
            fan_hold=value(CONF_FP_FAN_HOLD),
            auto_off=bool(options.get(CONF_FP_ENABLED)),
            off_margin=value(CONF_FP_OFF_MARGIN),
            restart_margin=value(CONF_FP_MARGIN),
            dwell=value(CONF_FP_DWELL),
            idle_dwell=value(CONF_FP_IDLE_DWELL),
            min_on=value(CONF_FP_MIN_ON),
            min_off=value(CONF_FP_MIN_OFF),
        )

    @property
    def active(self) -> bool:
        return self.smart_fan or self.auto_off


def demand(mode: HvacMode, state: DeviceState, temp: float) -> float | None:
    """Degrees C of conditioning still needed in mode; positive means more."""
    cool = None if state.sp_cool is None else temp - state.sp_cool
    heat = None if state.sp_heat is None else state.sp_heat - temp
    if mode is HvacMode.COOL:
        return cool
    if mode is HvacMode.HEAT:
        return heat
    sides = [side for side in (cool, heat) if side is not None]
    return max(sides) if sides else None


@dataclass(frozen=True, slots=True)
class Park:
    """One parked unit: the mode to resume in and the wall time it was parked."""

    mode: HvacMode
    at: float


class _ControlStore(Store[dict[str, Any]]):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version == 1:
            return {serial: {"park": park, "smart_fan": False} for serial, park in old_data.items()}
        return old_data


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return _ControlStore(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.fan_park", private=True)


def _park(raw: Any) -> Park | None:
    if not isinstance(raw, dict):
        return None
    mode = HvacMode.parse(raw.get("mode"))
    at = raw.get("at")
    if mode not in PARK_MODES or isinstance(at, bool) or not isinstance(at, int | float):
        return None
    assert mode is not None
    return Park(mode, float(at))


class ParkRecords:
    """Parked units and dynamic fan selections by serial, persisted across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _store(hass, entry_id)
        self._data: dict[str, Park] = {}
        self._smart: set[str] = set()

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self._data = {}
        self._smart = set()
        for serial, record in raw.items():
            if not isinstance(record, dict):
                continue
            if (park := _park(record.get("park"))) is not None:
                self._data[serial] = park
            if record.get("smart_fan") is True:
                self._smart.add(serial)

    def _raw(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for serial in sorted(self._data.keys() | self._smart):
            park = self._data.get(serial)
            raw[serial] = {
                "park": None if park is None else {"mode": park.mode.value, "at": park.at},
                "smart_fan": serial in self._smart,
            }
        return raw

    def get(self, serial: str) -> Park | None:
        return self._data.get(serial)

    async def async_set(self, serial: str, mode: HvacMode, at: float) -> None:
        self._data[serial] = Park(mode, at)
        await self._store.async_save(self._raw())

    def touch(self, serial: str, at: float) -> None:
        if (record := self._data.get(serial)) is not None:
            self._data[serial] = Park(record.mode, at)
            self._store.async_delay_save(self._raw)

    def pop(self, serial: str) -> Park | None:
        record = self._data.pop(serial, None)
        if record is not None:
            self._store.async_delay_save(self._raw)
        return record

    def serials(self) -> set[str]:
        return set(self._data)

    def smart_fan(self, serial: str) -> bool:
        return serial in self._smart

    async def async_set_smart_fan(self, serial: str, on: bool) -> None:
        if on:
            self._smart.add(serial)
        else:
            self._smart.discard(serial)
        await self._store.async_save(self._raw())


async def async_remove_records(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the smart control Store for entry_id."""
    await _store(hass, entry_id).async_remove()


async def async_release(
    coordinator: KumoDeviceCoordinator, records: ParkRecords, mode: HvacMode
) -> None:
    """Auto off was disabled while parked: resume in the parked mode, retrying until on."""
    warned = False
    while records.get(coordinator.serial) is not None and not coordinator.data.power:
        try:
            await coordinator.async_execute(SetMode(mode))
        except ServiceValidationError as err:
            _LOGGER.warning("Could not resume parked unit %s: %s", coordinator.serial, err)
            break
        except HomeAssistantError as err:
            if not warned:
                warned = True
                _LOGGER.warning("Could not resume parked unit %s: %s", coordinator.serial, err)
            await asyncio.sleep(RESUME_RETRY_S)
        else:
            break
    records.pop(coordinator.serial)


class SmartController:
    """Sets one unit's fan speed from demand, and powers it off past the setpoint and back on."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: KumoDeviceCoordinator,
        records: ParkRecords,
        *,
        options: ControlOptions,
        entity_id: str | None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._records = records
        self._serial = coordinator.serial
        self._clock = coordinator.hub.clock
        self.options = options
        self.entity_id = entity_id
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._busy = 0
        self._stopping = False
        self._epoch = 0
        now = self._clock.now()
        self._power = coordinator.data.power
        self._on_since = now
        self._over_since: float | None = None
        self._settle_until = 0.0
        self._retry_at = 0.0
        self._fan_at: float | None = None
        self._temp_lost_at: float | None = None

    @property
    def parked_mode(self) -> HvacMode | None:
        """Intended mode while auto off holds the unit off, else None."""
        record = self._records.get(self._serial)
        return None if record is None else record.mode

    @property
    def smart_fan_selected(self) -> bool:
        return self._records.smart_fan(self._serial)

    @property
    def smart_fan_active(self) -> bool:
        """The dynamic fan mode is enabled and selected."""
        return self.options.smart_fan and self.smart_fan_selected

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self._coordinator.async_add_listener(self._evaluate))
        self._unsubs.append(
            async_track_time_interval(
                self._hass, self._on_tick, timedelta(seconds=EVAL_S), cancel_on_shutdown=True
            )
        )
        if self.entity_id:
            self._unsubs.append(
                async_track_state_change_event(self._hass, [self.entity_id], self._on_sensor)
            )
        self._evaluate()

    async def async_stop(self) -> None:
        """Stop evaluating; a parked unit stays parked and its record persists."""
        self._stopping = True
        while self._unsubs:
            self._unsubs.pop()()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @callback
    def async_cancel(self) -> None:
        """Stop parking without a command (HA off, mode change, external power-on)."""
        self._epoch += 1
        if self._records.pop(self._serial) is not None:
            self._coordinator.async_update_listeners()

    async def async_resume(self) -> None:
        """HA turn_on: resume now in the parked mode."""
        if (mode := self.parked_mode) is None:
            return
        self._epoch += 1
        self._busy += 1
        try:
            error = await self._async_send_resume(mode)
        finally:
            self._busy -= 1
        self._coordinator.async_update_listeners()
        if error is not None:
            raise error

    async def async_set_smart_fan(self, on: bool) -> None:
        """Hand the fan speed to the controller, or back to the user."""
        if on:
            self._fan_at = None
        await self._records.async_set_smart_fan(self._serial, on)
        self._coordinator.async_update_listeners()

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        self._busy += 1
        task = self._entry.async_create_background_task(
            self._hass, coro, f"{DOMAIN} {self._serial} smart control {what}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._evaluate()

    @callback
    def _on_sensor(self, _event: Event[EventStateChangedData]) -> None:
        self._evaluate()

    @callback
    def _evaluate(self) -> None:
        coordinator = self._coordinator
        state = coordinator.data
        now = self._clock.now()
        if state.power is not None:
            if state.power and not self._power:
                self._on_since = now
                self._over_since = None
            self._power = state.power
        if (
            self._busy
            or self._stopping
            or state.power is None
            or not coordinator.last_update_success
            or not coordinator.device_available
        ):
            return
        if (record := self._records.get(self._serial)) is not None:
            # Without auto off, the hub's release resumes the unit.
            if self.options.auto_off:
                self._evaluate_parked(record, state, now)
            return
        self._evaluate_running(state, now)

    def _evaluate_parked(self, record: Park, state: DeviceState, now: float) -> None:
        if state.power:
            if now >= self._settle_until:
                self.async_cancel()
            return
        if now < self._retry_at or now - record.at < self.options.min_off:
            return
        if self._should_resume(record.mode, state, now):
            self._spawn(self._async_resume(record.mode), "resume")

    def _evaluate_running(self, state: DeviceState, now: float) -> None:
        mode = state.mode
        temp = self._temperature(state, now)
        if (
            not state.power
            or mode is None
            or mode not in PARK_MODES
            or mode not in self._coordinator.capabilities.hvac_modes
            or unit_action(state, fresh_telemetry(self._coordinator)) in _UNIT_MANAGED
            or temp is None
        ):
            self._over_since = None
            return
        if self.options.auto_off and self._should_park(mode, state, temp, now):
            self._spawn(self._async_park(mode), "park")
        elif self.smart_fan_active:
            self._evaluate_fan(mode, state, temp, now)

    def _should_park(self, mode: HvacMode, state: DeviceState, temp: float, now: float) -> bool:
        options = self.options
        if not self._overshot(mode, state, temp):
            self._over_since = None
            return False
        if self._over_since is None:
            self._over_since = now
        dwell = min(options.dwell, options.idle_dwell) if self._idle(state) else options.dwell
        return (
            now - self._over_since >= dwell
            and now - self._on_since >= options.min_on
            and now >= self._settle_until
            and now >= self._retry_at
        )

    def _overshot(self, mode: HvacMode, state: DeviceState, temp: float) -> bool:
        off = self.options.off_margin
        heat = state.sp_heat is not None and temp >= state.sp_heat + off
        cool = state.sp_cool is not None and temp <= state.sp_cool - off
        if mode is HvacMode.HEAT:
            return heat
        if mode is HvacMode.COOL:
            return cool
        return heat and cool

    def _idle(self, state: DeviceState) -> bool:
        """Standby, or a 0x06 stop read since power-on; only shortens the dwell."""
        if state.standby:
            return True
        telemetry = fresh_telemetry(self._coordinator)
        return (
            telemetry is not None
            and telemetry.read_at is not None
            and telemetry.read_at >= self._on_since
            and telemetry.operating is False
        )

    def _evaluate_fan(self, mode: HvacMode, state: DeviceState, temp: float, now: float) -> None:
        options = self.options
        ladder = [s for s in self._coordinator.capabilities.fan_speeds if s is not FanSpeed.AUTO]
        need = demand(mode, state, temp)
        if not ladder or need is None:
            return
        if self._fan_at is not None and now - self._fan_at < options.fan_hold:
            return
        current = state.fan_speed
        target = self._fan_index(need, len(ladder))
        if current in ladder:
            index = ladder.index(current)
            if target < index:
                target = min(index, self._fan_index(need + options.fan_hysteresis, len(ladder)))
        speed = ladder[target]
        if speed is current:
            return
        self._fan_at = now
        self._spawn(self._async_set_fan(speed), "fan")

    def _fan_index(self, need: float, count: int) -> int:
        share = min(max(need / self.options.full_speed_at, 0.0), 1.0)
        return math.floor(share * (count - 1) + 0.5)

    def _temperature(self, state: DeviceState, now: float) -> float | None:
        if self.entity_id:
            return state_celsius(self._hass.states.get(self.entity_id))
        if state.updated_at is None or now - state.updated_at > STALE_S:
            return None
        return state.room_temp

    def _should_resume(self, mode: HvacMode, state: DeviceState, now: float) -> bool:
        if (temp := self._temperature(state, now)) is None:
            if not self.entity_id:
                return True
            # A sensor that has not loaded yet after a restart is not a lost sensor.
            if self._temp_lost_at is None:
                self._temp_lost_at = now
            return now - self._temp_lost_at >= STALE_S
        self._temp_lost_at = None
        margin = self.options.restart_margin
        sides: list[bool | None] = []
        if mode in (HvacMode.HEAT, HvacMode.AUTO):
            sides.append(None if state.sp_heat is None else temp <= state.sp_heat - margin)
        if mode in (HvacMode.COOL, HvacMode.AUTO):
            sides.append(None if state.sp_cool is None else temp >= state.sp_cool + margin)
        return any(side is not False for side in sides)

    async def _async_set_fan(self, speed: FanSpeed) -> None:
        try:
            if self.smart_fan_active and self.parked_mode is None:
                await self._coordinator.async_execute(SetFanSpeed(speed))
        except HomeAssistantError as err:
            _LOGGER.warning("Could not set the fan speed of %s: %s", self._serial, err)
        finally:
            self._busy -= 1

    async def _async_park(self, mode: HvacMode) -> None:
        epoch, on_since = self._epoch, self._on_since
        error: HomeAssistantError | None = None
        self._temp_lost_at = None
        try:
            await self._records.async_set(self._serial, mode, self._clock.now())
            if epoch == self._epoch:
                await self._coordinator.async_execute(SetPower(False))
        except HomeAssistantError as err:
            error = err
        finally:
            self._busy -= 1
        if epoch != self._epoch:
            return
        now = self._clock.now()
        self._settle_until = now + SETTLE_S
        if error is None:
            self._records.touch(self._serial, now)
            _LOGGER.debug("Parked %s in %s", self._serial, mode)
        else:
            # The unit may have turned off anyway; keep the record and let a poll decide.
            if isinstance(error, ServiceValidationError):
                self._records.pop(self._serial)
            else:
                self._records.touch(self._serial, now)
            self._retry_at = now + PARK_RETRY_S
            # The rollback to power on is not a fresh start.
            self._on_since = on_since
            _LOGGER.warning("Could not park %s: %s", self._serial, error)
        self._coordinator.async_update_listeners()

    async def _async_resume(self, mode: HvacMode) -> None:
        try:
            error = await self._async_send_resume(mode)
        finally:
            self._busy -= 1
        if error is not None:
            _LOGGER.warning("Could not resume %s: %s", self._serial, error)
        self._coordinator.async_update_listeners()

    async def _async_send_resume(self, mode: HvacMode) -> HomeAssistantError | None:
        """SetMode, not SetPower(True): power-on alone can lose AUTO after a restart."""
        epoch = self._epoch
        try:
            await self._coordinator.async_execute(SetMode(mode))
        except HomeAssistantError as err:
            if epoch == self._epoch:
                if isinstance(err, ServiceValidationError):
                    self._records.pop(self._serial)
                else:
                    self._retry_at = self._clock.now() + RESUME_RETRY_S
            return err
        if epoch == self._epoch:
            self._records.pop(self._serial)
            now = self._clock.now()
            self._settle_until = now + SETTLE_S
            self._on_since = now
            self._over_since = None
            self._fan_at = None
        return None
