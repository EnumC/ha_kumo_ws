"""Per-unit CN105 telemetry reads while the link is LOCAL_OK."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .coordinator import KumoDeviceCoordinator
from .pykumo2.domain.enums import LinkState
from .pykumo2.domain.state import Cn105Telemetry, DeviceState, StatePatch
from .pykumo2.local.cn105 import InfoCode
from .pykumo2.local.cn105.bus import REPLY_TIMEOUT_SECONDS
from .pykumo2.local.cn105.telemetry import Cn105TelemetryReader, InfoBus
from .pykumo2.transport import TransportKind

_LOGGER = logging.getLogger(__name__)

FRESH_INTERVALS = 3
STATE_CHANGE_DELAY = 5.0
_OFF_SKIPPED = frozenset({InfoCode.COMPRESSOR, InfoCode.SUB_MODE})
_WARNED_COMPRESSOR: set[str] = set()


class BusSource(Protocol):
    """The LocalTransport slice this task uses."""

    def cn105_bus(self, serial: str) -> InfoBus: ...


def fresh_telemetry(coordinator: KumoDeviceCoordinator) -> Cn105Telemetry | None:
    """CN105 telemetry if it was read within FRESH_INTERVALS intervals, else None."""
    telemetry = coordinator.data.cn105
    if telemetry is None or telemetry.read_at is None:
        return None
    max_age = FRESH_INTERVALS * coordinator.hub.cn105_interval
    return telemetry if coordinator.hub.clock.now() - telemetry.read_at <= max_age else None


def _reader_mode(state: DeviceState) -> str | None:
    if state.power is False:
        return "off"
    if state.standby:
        return "idle"
    return None if state.mode is None else state.mode.value


def _trigger_key(state: DeviceState) -> tuple[Any, ...]:
    return (
        state.power,
        state.mode,
        state.auto_active,
        state.standby,
        state.defrost,
        state.sp_heat,
        state.sp_cool,
    )


class _ExclusiveBus:
    """InfoBus that reads each code inside the coordinator's exclusive section."""

    def __init__(self, coordinator: KumoDeviceCoordinator, local: BusSource) -> None:
        self._coordinator = coordinator
        self._local = local

    async def read_info(
        self,
        code: int,
        timeout: float = REPLY_TIMEOUT_SECONDS,  # noqa: ASYNC109
    ) -> bytes | None:
        coordinator = self._coordinator

        async def job() -> bytes | None:
            if coordinator.link.state is not LinkState.LOCAL_OK:
                return None
            return await self._local.cn105_bus(coordinator.serial).read_info(code, timeout)

        try:
            return await coordinator.async_run_exclusive(job)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug(
                "CN105 read for %s code=0x%02x failed: %s",
                coordinator.serial,
                code,
                type(err).__name__,
            )
            return None


class Cn105Poller:
    """Reads CN105 info codes every interval and patches them into DeviceState."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: KumoDeviceCoordinator,
        local: BusSource,
        *,
        codes: tuple[int, ...],
        interval: float,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._interval = interval
        self._codes = codes
        self._reader = Cn105TelemetryReader(
            _ExclusiveBus(coordinator, local), codes=codes, clock=coordinator.hub.clock
        )
        self._task: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_tick: CALLBACK_TYPE | None = None
        self._tick_job = HassJob(self._on_tick, cancel_on_shutdown=True)
        self._key = _trigger_key(coordinator.data)
        self._kicked_at: float | None = None
        if InfoCode.COMPRESSOR in codes and entry.entry_id not in _WARNED_COMPRESSOR:
            _WARNED_COMPRESSOR.add(entry.entry_id)
            _LOGGER.warning(
                "CN105 info code 0x06 is enabled; some units stop answering CN105 reads "
                "until the adapter reboots"
            )

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self._coordinator.async_add_listener(self._on_state))
        self._on_tick(None)

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_tick()
        if (task := self._task) is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    @property
    def reading(self) -> bool:
        return self._task is not None and not self._task.done()

    def _cancel_tick(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    def _schedule(self, delay: float) -> None:
        self._cancel_tick()
        self._unsub_tick = async_call_later(self._hass, delay, self._tick_job)

    @callback
    def _on_state(self) -> None:
        key = _trigger_key(self._coordinator.data)
        if key == self._key:
            return
        self._key = key
        now = self._coordinator.hub.clock.monotonic()
        if self._kicked_at is not None and now - self._kicked_at < self._interval:
            return
        self._kicked_at = now
        self._schedule(STATE_CHANGE_DELAY)

    @callback
    def _on_tick(self, _now: datetime | None) -> None:
        if self.reading:
            self._schedule(STATE_CHANGE_DELAY)
            return
        self._schedule(self._interval)
        if self._coordinator.link.state is not LinkState.LOCAL_OK:
            return
        self._task = self._entry.async_create_background_task(
            self._hass, self._async_read(), f"{DOMAIN} {self._coordinator.serial} cn105"
        )

    async def _async_read(self) -> None:
        coordinator = self._coordinator
        mode = _reader_mode(coordinator.data)
        codes = self._codes
        if mode == "off":
            codes = tuple(code for code in codes if code not in _OFF_SKIPPED)
            if not codes:
                values = {"cn105": Cn105Telemetry(operating=False)}
                now = coordinator.hub.clock.now()
                coordinator.async_handle_push(StatePatch(TransportKind.LOCAL, values, now))
                return
        telemetry = await self._reader.read(mode=mode, codes=codes)
        if telemetry.read_at is None:
            _LOGGER.debug("CN105 read for %s got no answer", coordinator.serial)
            return
        patch = StatePatch(TransportKind.LOCAL, {"cn105": telemetry}, telemetry.read_at)
        coordinator.async_handle_push(patch)
