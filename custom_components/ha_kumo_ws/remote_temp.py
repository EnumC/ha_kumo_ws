"""RemoteTempFeeder: feed a Home Assistant temperature sensor to one unit."""

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HassJob, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import DOMAIN
from .coordinator import KumoDeviceCoordinator
from .pykumo2.domain.commands import InjectRoomTemp, SetTempSource
from .pykumo2.domain.enums import TempSource
from .repairs import ISSUE_REMOTE_TEMP_LOST, async_clear_unit_issue, async_raise_unit_issue

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_S = 5.0
LOST_AFTER_S = 300.0
MIN_INTERVAL_S = 5
_UNITS = {
    UnitOfTemperature.CELSIUS: UnitOfTemperature.CELSIUS,
    "C": UnitOfTemperature.CELSIUS,
    None: UnitOfTemperature.CELSIUS,
    UnitOfTemperature.FAHRENHEIT: UnitOfTemperature.FAHRENHEIT,
    "F": UnitOfTemperature.FAHRENHEIT,
    UnitOfTemperature.KELVIN: UnitOfTemperature.KELVIN,
}


def state_celsius(state: State | None) -> float | None:
    """Numeric temperature of state in Celsius, or None when unusable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = _UNITS.get(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
    if unit is None or not math.isfinite(value):
        return None
    return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)


class RemoteTempFeeder:
    """Injects the mapped sensor on change (debounced) and on a heartbeat."""

    # With manage_source the previous tempSource is restored if the sensor is lost or on unload.
    # Injection pauses while the unit reports a source other than api (user override).

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: KumoDeviceCoordinator,
        *,
        entity_id: str,
        interval: float,
        manage_source: bool,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self.entity_id = entity_id
        self._interval = max(float(interval), MIN_INTERVAL_S)
        self._manage_source = manage_source
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_debounce: Callable[[], None] | None = None
        self._unsub_lost: Callable[[], None] | None = None
        self._debounce_job = HassJob(self._on_debounce, cancel_on_shutdown=True)
        self._lost_job = HassJob(self._on_lost, cancel_on_shutdown=True)
        self._previous: TempSource | None = None
        self._managed = False
        self._paused = False
        self._lost = False
        self._stopping = False

    @property
    def managed(self) -> bool:
        """True while the unit regulates on this feeder (tempSource=api set by us)."""
        return self._managed

    @callback
    def async_start(self) -> None:
        self._unsubs.append(
            async_track_state_change_event(self._hass, [self.entity_id], self._on_state)
        )
        self._unsubs.append(
            async_track_time_interval(
                self._hass,
                self._on_heartbeat,
                timedelta(seconds=self._interval),
                cancel_on_shutdown=True,
            )
        )
        if self._value() is None:
            self._start_lost_timer()
        else:
            self._spawn(self._async_inject(), "inject")

    async def async_stop(self) -> None:
        """Cancel timers and pending work, then restore the previous source."""
        self._stopping = True
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_debounce()
        self._cancel_lost_timer()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._async_restore()
        async_clear_unit_issue(self._hass, ISSUE_REMOTE_TEMP_LOST, self._coordinator.serial)

    def _value(self) -> float | None:
        return state_celsius(self._hass.states.get(self.entity_id))

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        task = self._entry.async_create_background_task(
            self._hass, coro, f"{DOMAIN} {self._coordinator.serial} remote temp {what}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        if state_celsius(event.data["new_state"]) is None:
            self._start_lost_timer()
            return
        self._cancel_lost_timer()
        if self._lost:
            self._lost = False
            async_clear_unit_issue(self._hass, ISSUE_REMOTE_TEMP_LOST, self._coordinator.serial)
        # Changes within the window coalesce; the latest value is sent when it closes.
        if self._unsub_debounce is None:
            self._unsub_debounce = async_call_later(self._hass, DEBOUNCE_S, self._debounce_job)

    @callback
    def _on_debounce(self, _now: datetime) -> None:
        self._unsub_debounce = None
        self._spawn(self._async_inject(), "inject")

    @callback
    def _on_heartbeat(self, _now: datetime) -> None:
        if self._lost:
            # Retry a restore that failed while the link was not local.
            if self._managed:
                self._spawn(self._async_restore(), "restore")
            return
        self._spawn(self._async_inject(), "inject")

    @callback
    def _on_lost(self, _now: datetime) -> None:
        self._unsub_lost = None
        self._lost = True
        _LOGGER.warning(
            "Remote temperature sensor for %s unavailable for %d s",
            self._coordinator.serial,
            LOST_AFTER_S,
        )
        if self._manage_source:
            async_raise_unit_issue(self._hass, ISSUE_REMOTE_TEMP_LOST, self._coordinator)
            self._spawn(self._async_restore(), "restore")

    @callback
    def _start_lost_timer(self) -> None:
        if self._unsub_lost is None and not self._lost:
            self._unsub_lost = async_call_later(self._hass, LOST_AFTER_S, self._lost_job)

    @callback
    def _cancel_lost_timer(self) -> None:
        if self._unsub_lost is not None:
            self._unsub_lost()
            self._unsub_lost = None

    @callback
    def _cancel_debounce(self) -> None:
        if self._unsub_debounce is not None:
            self._unsub_debounce()
            self._unsub_debounce = None

    async def _async_inject(self) -> None:
        coordinator = self._coordinator
        async with self._lock:
            celsius = self._value()
            if celsius is None or self._lost or self._stopping or not coordinator.link_is_local:
                return
            current = coordinator.data.temp_source
            try:
                if self._manage_source and not self._managed and not self._paused:
                    await coordinator.async_execute(SetTempSource(TempSource.API))
                    self._managed = True
                    if current not in (None, TempSource.API, TempSource.UNSET):
                        self._previous = current
                elif current is not TempSource.API:
                    self._release()
                    return
                elif self._paused:
                    self._paused = False
                    self._managed = True
                await coordinator.async_execute(InjectRoomTemp(celsius))
            except HomeAssistantError as err:
                _LOGGER.debug("Remote temperature for %s not sent: %s", coordinator.serial, err)

    async def _async_restore(self) -> None:
        async with self._lock:
            if not self._managed:
                return
            if self._coordinator.data.temp_source is not TempSource.API:
                self._release()
                return
            target = self._previous or TempSource.RETURNAIR
            try:
                await self._coordinator.async_execute(SetTempSource(target))
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "Could not restore temperature source for %s: %s",
                    self._coordinator.serial,
                    err,
                )
                return
            self._managed = False
            self._previous = None

    def _release(self) -> None:
        """Another source was chosen: stop managing it until api is reported again."""
        self._paused = self._manage_source
        self._managed = False
        self._previous = None
