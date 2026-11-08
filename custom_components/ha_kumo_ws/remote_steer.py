"""SetpointSteerer: steer a unit's setpoint so a Home Assistant sensor settles at the target."""

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
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
    CONF_RS_BIAS_TAU,
    CONF_RS_MAX_TRIM,
    CONF_RS_MIN_WRITE,
    CONF_RS_STEADY_BAND,
    CONF_RS_TRIM,
    CONF_RS_TRIM_TIME,
    CONF_RS_WARMUP,
    DOMAIN,
    RS_LIMITS,
)
from .coordinator import KumoDeviceCoordinator
from .pykumo2.domain.capabilities import Capabilities
from .pykumo2.domain.commands import Batch, Command, SetSetpoints, round_half
from .pykumo2.domain.enums import HvacMode
from .pykumo2.domain.state import DeviceState
from .remote_temp import (
    async_longest_report_gap,
    auto_stale_after,
    default_stale_after,
    fresh_celsius,
)
from .repairs import ISSUE_REMOTE_TEMP_LOST, async_clear_unit_issue, async_raise_unit_issue

_LOGGER = logging.getLogger(__name__)

HEAT = "heat"
COOL = "cool"
SIDES = (HEAT, COOL)
STEER_MODES = frozenset({HvacMode.HEAT, HvacMode.COOL, HvacMode.AUTO})
EVAL_S = 30
DERIVE_INTERVAL_S = 21600
SAMPLE_AFTER_WRITE_S = 300.0
UNIT_STALE_S = 900.0
MAX_DELTA_C = 8.0
BIAS_LIMIT_C = 6.0
SLOW_FACTOR = 4.0
MAX_DT_S = 120.0
TRIM_DEADBAND_C = 0.2
WRITE_THRESHOLD_C = 0.35
MAX_STEP_C = 1.0
MATCH_C = 0.3
RETRY_S = 60.0
ECHO_S = 90.0
ECHO_DEPTH = 3
CONFIRM_S = 60.0
EXTERNAL_WINDOW_S = 600.0
EXTERNAL_LIMIT = 3
PAUSE_S = 1800.0
RESTORE_RETRY_S = 60.0
STORAGE_VERSION = 1
_UNIT_MANAGED = frozenset({HVACAction.DEFROSTING, HVACAction.PREHEATING})


def _default(key: str) -> float:
    return float(RS_LIMITS[key][0])


@dataclass(frozen=True, slots=True)
class SteerOptions:
    """Per-unit steering tunables; temperatures in C, durations in seconds."""

    trim: bool = True
    bias_tau: float = _default(CONF_RS_BIAS_TAU)
    trim_time: float = _default(CONF_RS_TRIM_TIME)
    max_trim: float = _default(CONF_RS_MAX_TRIM)
    min_write: float = _default(CONF_RS_MIN_WRITE)
    warmup: float = _default(CONF_RS_WARMUP)
    steady_band: float = _default(CONF_RS_STEADY_BAND)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Self:
        """Stored unit options; missing keys use the defaults."""

        def value(key: str) -> float:
            return float(options.get(key, RS_LIMITS[key][0]))

        return cls(
            trim=bool(options.get(CONF_RS_TRIM, True)),
            bias_tau=value(CONF_RS_BIAS_TAU),
            trim_time=value(CONF_RS_TRIM_TIME),
            max_trim=value(CONF_RS_MAX_TRIM),
            min_write=value(CONF_RS_MIN_WRITE),
            warmup=value(CONF_RS_WARMUP),
            steady_band=value(CONF_RS_STEADY_BAND),
        )


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def steered_sides(mode: HvacMode | None) -> tuple[str, ...]:
    """Setpoint sides steered in mode."""
    if mode is HvacMode.HEAT:
        return (HEAT,)
    if mode is HvacMode.COOL:
        return (COOL,)
    if mode is HvacMode.AUTO:
        return SIDES
    return ()


def freeze_reason(
    state: DeviceState,
    action: HVACAction | None,
    remote: float | None,
    now: float,
    *,
    active_since: float,
    written_at: float | None,
    warmup: float,
) -> str | None:
    """None when the state may train the bias, else why it may not."""
    if not state.power:
        return "off"
    if state.mode not in STEER_MODES:
        return "mode"
    if action in _UNIT_MANAGED or state.hot_adjust:
        return "unit_managed"
    if state.standby:
        return "standby"
    if now - active_since < warmup:
        return "warmup"
    if written_at is not None and now - written_at < SAMPLE_AFTER_WRITE_S:
        return "write"
    if remote is None:
        return "stale_sensor"
    if state.updated_at is None or now - state.updated_at > UNIT_STALE_S:
        return "stale_unit"
    if state.room_temp is None or abs(state.room_temp - remote) > MAX_DELTA_C:
        return "delta"
    return None


def valid_sample(
    state: DeviceState,
    action: HVACAction | None,
    remote: float | None,
    now: float,
    *,
    active_since: float,
    written_at: float | None,
    warmup: float,
) -> bool:
    """The state may train the bias."""
    return (
        freeze_reason(
            state,
            action,
            remote,
            now,
            active_since=active_since,
            written_at=written_at,
            warmup=warmup,
        )
        is None
    )


def ema(
    value: float | None, seen: float, sample: float, dt: float, tau: float
) -> tuple[float, float]:
    """Plain average until tau seconds of samples are seen, then a time-weighted EMA."""
    if value is None or seen <= 0:
        return sample, dt
    seen += dt
    if seen <= tau:
        return value + (sample - value) * dt / seen, seen
    return value + (sample - value) * (1 - math.exp(-dt / tau)), seen


def shift_bounds(
    mode: HvacMode | None, heat: float | None, cool: float | None, caps: Capabilities
) -> tuple[float, float]:
    """Shift range that keeps every steered setpoint within the unit's range."""
    lows: list[float] = []
    highs: list[float] = []
    for side in steered_sides(mode):
        target = heat if side == HEAT else cool
        if target is None:
            continue
        rng = caps.heat_range(mode) if side == HEAT else caps.cool_range(mode)
        lows.append(rng.low - target)
        highs.append(rng.high - target)
    if not lows:
        return 0.0, 0.0
    low, high = max(lows), min(highs)
    return (low, high) if low <= high else (0.0, 0.0)


def quantize(value: float, current: float | None) -> float | None:
    """value rounded to 0.5 C when it is at least 0.35 C from current, else None."""
    if current is not None and abs(value - current) < WRITE_THRESHOLD_C:
        return None
    return round_half(value)


def is_ours(
    reported: float, written: float | None, history: list[tuple[float, float]], now: float
) -> bool:
    """reported is the last written value, or one of the recent ones within the echo window."""
    if written is not None and abs(reported - written) < MATCH_C:
        return True
    return any(
        abs(reported - value) < MATCH_C and now - at <= ECHO_S
        for value, at in history[-ECHO_DEPTH:]
    )


def _side_value(state: DeviceState, side: str) -> float | None:
    return state.sp_heat if side == HEAT else state.sp_cool


def _sides() -> dict[str, float | None]:
    return dict.fromkeys(SIDES)


def _zeros() -> dict[str, float]:
    return dict.fromkeys(SIDES, 0.0)


@dataclass(slots=True)
class SteerRecord:
    """Persisted steering state of one unit."""

    targets: dict[str, float | None] = field(default_factory=_sides)
    written: dict[str, float | None] = field(default_factory=_sides)
    bias: dict[str, float | None] = field(default_factory=_sides)
    trim: dict[str, float] = field(default_factory=_zeros)
    n: dict[str, float] = field(default_factory=_zeros)
    auto_side: str | None = None

    @property
    def pending(self) -> bool:
        """The unit holds a shifted setpoint that is owed a restore."""
        return any(value is not None for value in self.written.values())


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        return None
    return float(raw)


def _per_side(raw: Any) -> dict[str, float | None]:
    raw = raw if isinstance(raw, dict) else {}
    return {side: _number(raw.get(side)) for side in SIDES}


def _record(raw: dict[str, Any]) -> SteerRecord:
    side = raw.get("auto_side")
    return SteerRecord(
        targets=_per_side(raw.get("targets")),
        written=_per_side(raw.get("written")),
        bias=_per_side(raw.get("bias")),
        trim={k: v or 0.0 for k, v in _per_side(raw.get("trim")).items()},
        n={k: v or 0.0 for k, v in _per_side(raw.get("n")).items()},
        auto_side=side if side in SIDES else None,
    )


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.steer", private=True)


class SteerRecords:
    """Virtual targets, written setpoints and learned terms by serial."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _store(hass, entry_id)
        self._data: dict[str, SteerRecord] = {}

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self._data = {
            serial: _record(record) for serial, record in raw.items() if isinstance(record, dict)
        }

    def _raw(self) -> dict[str, Any]:
        return {
            serial: {
                "targets": record.targets,
                "written": record.written,
                "bias": record.bias,
                "trim": record.trim,
                "n": record.n,
                "auto_side": record.auto_side,
            }
            for serial, record in sorted(self._data.items())
        }

    def get(self, serial: str) -> SteerRecord | None:
        return self._data.get(serial)

    def record(self, serial: str) -> SteerRecord:
        """The record of serial, created empty when missing."""
        return self._data.setdefault(serial, SteerRecord())

    def pending(self, serial: str) -> bool:
        record = self._data.get(serial)
        return record is not None and record.pending

    def serials(self) -> set[str]:
        return set(self._data)

    async def async_save(self) -> None:
        await self._store.async_save(self._raw())

    def delay_save(self) -> None:
        self._store.async_delay_save(self._raw, 60)

    async def async_clear_written(self, serial: str) -> None:
        if (record := self._data.get(serial)) is not None and record.pending:
            record.written = _sides()
            await self.async_save()

    async def async_pop(self, serial: str) -> None:
        if self._data.pop(serial, None) is not None:
            await self.async_save()


async def async_remove_records(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the steering Store for entry_id."""
    await _store(hass, entry_id).async_remove()


def _restore_values(
    record: SteerRecord,
    state: DeviceState,
    history: Mapping[str, list[tuple[float, float]]],
    now: float,
) -> dict[str, float]:
    """Targets for sides that still hold our written value."""
    values: dict[str, float] = {}
    for side in SIDES:
        written, target = record.written[side], record.targets[side]
        reported = _side_value(state, side)
        if written is None or target is None or reported is None:
            continue
        ours = is_ours(reported, written, history.get(side, []), now)
        if ours and abs(reported - target) >= MATCH_C:
            values[side] = target
    return values


async def async_restore_targets(coordinator: KumoDeviceCoordinator, records: SteerRecords) -> None:
    """The unit is left shifted without a steerer: write the targets back, retrying until done."""
    serial = coordinator.serial
    warned = False
    while (record := records.get(serial)) is not None and record.pending:
        state = coordinator.data
        if state.sp_heat is None and state.sp_cool is None:
            await asyncio.sleep(RESTORE_RETRY_S)
            continue
        values = _restore_values(record, state, {}, coordinator.hub.clock.now())
        if not values:
            break
        try:
            await coordinator.async_execute(
                SetSetpoints(heat=values.get(HEAT), cool=values.get(COOL)),
                coalesce="set_temperature",
            )
        except ServiceValidationError as err:
            _LOGGER.warning("Could not restore the setpoint of %s: %s", serial, err)
            break
        except HomeAssistantError as err:
            if not warned:
                warned = True
                _LOGGER.warning("Could not restore the setpoint of %s: %s", serial, err)
        else:
            break
        await asyncio.sleep(RESTORE_RETRY_S)
    await records.async_clear_written(serial)


class SetpointSteerer:
    """Writes target + bias + trim to one cloud unit so the mapped sensor reaches the target."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: KumoDeviceCoordinator,
        records: SteerRecords,
        *,
        entity_id: str,
        options: SteerOptions,
        stale_after: float | None,
        lost_after: float,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._records = records
        self._serial = coordinator.serial
        self._clock = coordinator.hub.clock
        self.entity_id = entity_id
        self.options = options
        self._auto_stale = stale_after is None
        self._stale_after = default_stale_after(stale_after)
        self._lost_after = float(lost_after)
        self._record = records.record(self._serial)
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._busy = 0
        self._epoch = 0
        self._stopping = False
        self._stopped = False
        now = self._clock.now()
        state = coordinator.data
        self._power = state.power
        self._mode = state.mode
        self._active_since = now
        self._unchecked = {side for side in SIDES if self._record.written[side] is not None}
        self._history: dict[str, deque[tuple[float, float]]] = {
            side: deque(maxlen=ECHO_DEPTH) for side in SIDES
        }
        self._candidates: dict[str, tuple[float, float]] = {}
        self._external: deque[float] = deque()
        self._paused_until = 0.0
        self._retry_at = 0.0
        self._written_at: float | None = None
        self._sample_at: float | None = None
        self._missing_since: float | None = None
        self._lost = False
        self._force = True
        self._saturated = False
        self._freeze: str | None = None

    @property
    def targets(self) -> tuple[float | None, float | None]:
        """Virtual heat and cool targets."""
        return self._record.targets[HEAT], self._record.targets[COOL]

    @property
    def remote_temp(self) -> float | None:
        """Fresh mapped sensor value in C, else None."""
        return fresh_celsius(self._hass.states.get(self.entity_id), self._stale_after)

    @property
    def stale_after(self) -> float:
        """Seconds without a report before the sensor counts as stale."""
        return self._stale_after

    @property
    def saturated(self) -> bool:
        """The last shift was clipped by the unit's setpoint range."""
        return self._saturated

    @property
    def shift(self) -> float | None:
        """Applied setpoint shift in C on the steered side, 0 when unshifted."""
        side = self._side(self._coordinator.data)
        if side is None:
            return None
        written, target = self._record.written[side], self._record.targets[side]
        return 0.0 if written is None or target is None else written - target

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self._coordinator.async_add_listener(self._evaluate))
        self._unsubs.append(
            async_track_time_interval(
                self._hass, self._on_tick, timedelta(seconds=EVAL_S), cancel_on_shutdown=True
            )
        )
        self._unsubs.append(
            async_track_state_change_event(self._hass, [self.entity_id], self._on_sensor)
        )
        if self._auto_stale:
            self._unsubs.append(
                async_track_time_interval(
                    self._hass,
                    self._on_derive,
                    timedelta(seconds=DERIVE_INTERVAL_S),
                    cancel_on_shutdown=True,
                )
            )
            self._spawn(self._async_derive_stale_after(), "derive")
        self._evaluate()

    async def async_stop(self) -> None:
        """Cancel timers and pending work, then write the targets back."""
        if self._stopped:
            return
        self._stopping = self._stopped = True
        self._epoch += 1
        while self._unsubs:
            self._unsubs.pop()()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._async_restore()
        await self._records.async_save()
        async_clear_unit_issue(self._hass, ISSUE_REMOTE_TEMP_LOST, self._serial)

    async def async_set_targets(
        self,
        heat: float | None = None,
        cool: float | None = None,
        *,
        command: Command | None = None,
        mode: HvacMode | None = None,
    ) -> None:
        """New virtual targets for mode (default the unit's); command goes first in one batch."""
        caps = self._coordinator.capabilities
        record = self._record
        if heat is not None:
            record.targets[HEAT] = caps.heat_range().clamp(round_half(heat))
        if cool is not None:
            record.targets[COOL] = caps.cool_range().clamp(round_half(cool))
        await self._records.async_save()
        self._epoch += 1
        self._force = False
        sent: dict[str, float | None] = {}

        previous = dict(self._record.written)

        def build(current: DeviceState) -> Command:
            setpoints = self._setpoints(current, mode or current.mode, False, sent)
            self._mark_written(sent)
            return setpoints if command is None else Batch((command, setpoints))

        self._busy += 1
        try:
            await self._coordinator.async_execute(
                build, coalesce="set_temperature" if command is None else None
            )
        except HomeAssistantError:
            self._record.written.update(previous)
            self._force = True
            raise
        finally:
            self._busy -= 1
        await self._async_wrote(sent)
        self._coordinator.async_update_listeners()

    def diagnostics(self) -> dict[str, Any]:
        """Steering state for diagnostics."""
        now = self._clock.now()
        record = self._record
        return {
            "side": self._side(self._coordinator.data),
            "targets": dict(record.targets),
            "written": dict(record.written),
            "bias": dict(record.bias),
            "trim": dict(record.trim),
            "n": dict(record.n),
            "saturated": self._saturated,
            "freeze_reason": self._freeze,
            "lost": self._lost,
            "last_write_age": None if self._written_at is None else now - self._written_at,
            "external_changes": len(self._external),
            "paused_until": self._paused_until if self._paused_until > now else None,
        }

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        self._busy += 1
        task = self._entry.async_create_background_task(
            self._hass, self._run(coro), f"{DOMAIN} {self._serial} steer {what}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        finally:
            self._busy -= 1

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._evaluate()

    @callback
    def _on_sensor(self, _event: Event[EventStateChangedData]) -> None:
        self._evaluate()

    @callback
    def _on_derive(self, _now: datetime) -> None:
        self._spawn(self._async_derive_stale_after(), "derive")

    async def _async_derive_stale_after(self) -> None:
        gap = await async_longest_report_gap(self._hass, self.entity_id)
        self._stale_after = auto_stale_after(gap)

    def _side(self, state: DeviceState) -> str | None:
        return self._side_for(state.mode)

    def _side_for(self, mode: HvacMode | None) -> str | None:
        if mode is HvacMode.HEAT:
            return HEAT
        if mode is HvacMode.COOL:
            return COOL
        if mode is HvacMode.AUTO:
            return self._record.auto_side
        return None

    def _raw_shift(self, side: str | None) -> float:
        if side is None:
            return 0.0
        trim = self._record.trim[side] if self.options.trim else 0.0
        return (self._record.bias[side] or 0.0) + trim

    def _shift(self, mode: HvacMode | None) -> tuple[float, float, float]:
        """Clamped shift, its range and the unclamped shift for mode."""
        raw = self._raw_shift(self._side_for(mode))
        heat, cool = self.targets
        low, high = shift_bounds(mode, heat, cool, self._coordinator.capabilities)
        return _clamp(raw, low, high), low, high

    def _setpoints(
        self,
        state: DeviceState,
        mode: HvacMode | None,
        limited: bool,
        sent: dict[str, float | None],
    ) -> SetSetpoints:
        """Shifted setpoints for mode, or the targets when mode is not steered."""
        caps = self._coordinator.capabilities
        values: dict[str, float] = {}
        sides = steered_sides(mode)
        if sides and not self._lost:
            shift, _low, _high = self._shift(mode)
            for side in sides:
                target = self._record.targets[side]
                if target is None:
                    continue
                rng = caps.heat_range(mode) if side == HEAT else caps.cool_range(mode)
                value = rng.clamp(round_half(target + shift))
                current = _side_value(state, side)
                if limited and current is not None:
                    value = current + _clamp(value - current, -MAX_STEP_C, MAX_STEP_C)
                values[side] = value
                sent[side] = value
        if not values:
            targets = {s: t for s in SIDES if (t := self._record.targets[s]) is not None}
            changed = {
                side: target
                for side, target in targets.items()
                if self._record.written[side] is not None
                or (reported := _side_value(state, side)) is None
                or abs(reported - target) >= MATCH_C
            }
            values = changed or targets
            sent.update(dict.fromkeys(values))
        return SetSetpoints(heat=values.get(HEAT), cool=values.get(COOL))

    def _mark_written(self, sent: dict[str, float | None]) -> None:
        """Record values as ours before delivery; a shielded send outlives a cancelled caller."""
        now = self._clock.now()
        for side, value in sent.items():
            self._record.written[side] = value
            self._candidates.pop(side, None)
            if value is None:
                self._history[side].clear()
            elif not self._history[side] or self._history[side][-1][0] != value:
                self._history[side].append((value, now))
        self._written_at = now
        self._sample_at = None

    async def _async_wrote(self, sent: dict[str, float | None]) -> None:
        self._mark_written(sent)
        await self._records.async_save()

    @callback
    def _evaluate(self) -> None:
        if self._stopping:
            return
        coordinator = self._coordinator
        state = coordinator.data
        now = self._clock.now()
        self._track(state, now)
        if (
            self._busy
            or not coordinator.last_update_success
            or not coordinator.device_available
            or (state.sp_heat is None and state.sp_cool is None)
        ):
            return
        self._resolve(state)
        remote = self.remote_temp
        if self._check_lost(remote, now):
            if self._record.pending and now >= self._retry_at:
                self._spawn(self._async_restore(), "restore")
            return
        self._check_external(state, now)
        mode = state.mode
        if not state.power or mode is None:
            self._freeze = "off"
            return
        if mode not in STEER_MODES:
            self._freeze = "mode"
            if self._record.pending and now >= self._retry_at:
                self._spawn(self._async_restore(), "restore")
            return
        if remote is None:
            return
        self._learn(state, remote, now)
        self._decide(state, now)

    def _track(self, state: DeviceState, now: float) -> None:
        if state.power is not None:
            if state.power and not self._power:
                self._restart(now)
            self._power = state.power
        if state.mode is not None:
            if state.mode is not self._mode and self._mode is not None:
                self._restart(now)
            self._mode = state.mode
        if state.mode is HvacMode.AUTO and state.auto_active in (HvacMode.HEAT, HvacMode.COOL):
            side = HEAT if state.auto_active is HvacMode.HEAT else COOL
            if self._record.auto_side != side:
                self._record.auto_side = side
                self._records.delay_save()
                self._restart(now)
        for side in SIDES:
            if self._lost or not state.power or side not in steered_sides(state.mode):
                self._follow(state, side)

    def _restart(self, now: float) -> None:
        """Power-on or mode change: re-shift with the stored bias and restart warm-up."""
        self._active_since = now
        self._sample_at = None
        self._force = True

    def _follow(self, state: DeviceState, side: str) -> None:
        """An unshifted side that is not steered tracks the unit's value."""
        if self._busy or self._record.written[side] is not None:
            return
        if (reported := _side_value(state, side)) is not None:
            self._record.targets[side] = reported

    def _resolve(self, state: DeviceState) -> None:
        """Seed missing targets; a stored write the unit no longer holds is adopted as target."""
        record = self._record
        changed = False
        for side in SIDES:
            reported = _side_value(state, side)
            if reported is None:
                continue
            if side in self._unchecked:
                self._unchecked.discard(side)
                written = record.written[side]
                if written is not None and abs(reported - written) >= MATCH_C:
                    record.targets[side] = reported
                    record.written[side] = None
                    changed = True
            if record.targets[side] is None:
                record.targets[side] = reported
                changed = True
        if changed:
            self._records.delay_save()

    def _check_lost(self, remote: float | None, now: float) -> bool:
        if remote is not None:
            self._missing_since = None
            if self._lost:
                self._lost = False
                self._force = True
                async_clear_unit_issue(self._hass, ISSUE_REMOTE_TEMP_LOST, self._serial)
            return False
        if self._missing_since is None:
            self._missing_since = now
        self._freeze = "stale_sensor"
        if not self._lost and now - self._missing_since >= self._lost_after:
            self._lost = True
            self._freeze = "lost"
            _LOGGER.warning(
                "Remote temperature sensor for %s unavailable for %d s",
                self._serial,
                self._lost_after,
            )
            async_raise_unit_issue(self._hass, ISSUE_REMOTE_TEMP_LOST, self._coordinator)
        return self._lost

    def _check_external(self, state: DeviceState, now: float) -> None:
        record = self._record
        steered = steered_sides(state.mode) if state.power else ()
        for side in SIDES:
            written = record.written[side]
            expected = (
                written if written is not None or side not in steered else record.targets[side]
            )
            reported = _side_value(state, side)
            if expected is None or reported is None:
                continue
            if is_ours(reported, expected, list(self._history[side]), now):
                self._candidates.pop(side, None)
                continue
            candidate = self._candidates.get(side)
            if candidate is None or abs(candidate[0] - reported) >= MATCH_C:
                self._candidates[side] = (reported, now)
                continue
            if now - candidate[1] < CONFIRM_S or (
                self._written_at is not None and now - self._written_at < ECHO_S
            ):
                continue
            del self._candidates[side]
            record.targets[side] = reported
            record.written[side] = None if written is None else reported
            self._history[side].clear()
            self._force = True
            self._note_external(now)
            self._records.delay_save()
            _LOGGER.debug("External %s setpoint %s on %s", side, reported, self._serial)

    def _note_external(self, now: float) -> None:
        self._external.append(now)
        while self._external and now - self._external[0] > EXTERNAL_WINDOW_S:
            self._external.popleft()
        if len(self._external) > EXTERNAL_LIMIT and now >= self._paused_until:
            self._paused_until = now + PAUSE_S
            _LOGGER.warning(
                "Setpoint of %s changed outside Home Assistant %d times in %d min; "
                "steering paused for %d min",
                self._serial,
                len(self._external),
                EXTERNAL_WINDOW_S // 60,
                PAUSE_S // 60,
            )

    def _learn(self, state: DeviceState, remote: float | None, now: float) -> None:
        action = unit_action(state, fresh_telemetry(self._coordinator))
        reason = freeze_reason(
            state,
            action,
            remote,
            now,
            active_since=self._active_since,
            written_at=self._written_at,
            warmup=self.options.warmup,
        )
        side = self._side(state)
        if reason is None and side is None:
            reason = "auto_side"
        self._freeze = reason
        if reason is not None:
            self._sample_at = None
            return
        assert remote is not None and state.room_temp is not None and side is not None
        if self._sample_at is None:
            self._sample_at = now
            return
        dt = min(now - self._sample_at, MAX_DT_S)
        self._sample_at = now
        target = self._record.targets[side]
        if dt <= 0 or target is None:
            return
        record, options = self._record, self.options
        error = target - remote
        weight = dt if abs(error) <= options.steady_band else dt / SLOW_FACTOR
        sample = _clamp(state.room_temp - remote, -BIAS_LIMIT_C, BIAS_LIMIT_C)
        bias, record.n[side] = ema(
            record.bias[side], record.n[side], sample, weight, options.bias_tau
        )
        record.bias[side] = _clamp(bias, -BIAS_LIMIT_C, BIAS_LIMIT_C)
        if options.trim and abs(error) > TRIM_DEADBAND_C:
            raw = self._raw_shift(side)
            _shift, low, high = self._shift(state.mode)
            if not (error > 0 and raw >= high) and not (error < 0 and raw <= low):
                record.trim[side] = _clamp(
                    record.trim[side] + error * dt / options.trim_time,
                    -options.max_trim,
                    options.max_trim,
                )
        self._records.delay_save()

    def _decide(self, state: DeviceState, now: float) -> None:
        mode = state.mode
        shift, low, high = self._shift(mode)
        raw = self._raw_shift(self._side(state))
        self._saturated = not low <= raw <= high
        if now < self._paused_until or now < self._retry_at or self._candidates:
            return
        forced = self._force
        if not forced and (
            self._written_at is not None and now - self._written_at < self.options.min_write
        ):
            return
        needed = any(
            quantize(target + shift, _side_value(state, side)) is not None
            for side in steered_sides(mode)
            if (target := self._record.targets[side]) is not None
        )
        self._force = False
        if needed:
            self._spawn(self._async_write(not forced), "write")

    async def _async_write(self, limited: bool) -> None:
        epoch = self._epoch
        sent: dict[str, float | None] = {}
        previous = dict(self._record.written)

        def build(state: DeviceState) -> Command:
            setpoints = self._setpoints(state, state.mode, limited, sent)
            self._mark_written(sent)
            return setpoints

        try:
            await self._coordinator.async_execute(build, coalesce="set_temperature")
        except HomeAssistantError as err:
            self._record.written.update(previous)
            if epoch == self._epoch:
                self._retry_at = self._clock.now() + RETRY_S
                self._force = self._force or not limited
            _LOGGER.debug("Setpoint of %s not steered: %s", self._serial, err)
            return
        await self._async_wrote(sent)

    async def _async_restore(self) -> None:
        """Write the targets back to every side that holds our shifted value."""
        history = {side: list(self._history[side]) for side in SIDES}
        values = _restore_values(self._record, self._coordinator.data, history, self._clock.now())
        if values:
            try:
                await self._coordinator.async_execute(
                    SetSetpoints(heat=values.get(HEAT), cool=values.get(COOL)),
                    coalesce="set_temperature",
                )
            except ServiceValidationError as err:
                _LOGGER.warning("Could not restore the setpoint of %s: %s", self._serial, err)
            except HomeAssistantError as err:
                _LOGGER.warning("Could not restore the setpoint of %s: %s", self._serial, err)
                self._retry_at = self._clock.now() + RESTORE_RETRY_S
                return
        await self._async_wrote(dict.fromkeys(SIDES))
