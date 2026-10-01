"""Tracks which serials hold a cloud socket lease and releases them after a cooldown."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..clock import Clock
from ..errors import KumoError
from ..transport import LeaseReason
from .ports import CloudPort

_LOGGER = logging.getLogger(__name__)

BACKOFF_INITIAL_S = 30.0
BACKOFF_MAX_S = 900.0


class CloudLeaseManager:
    """Per-serial leases: FALLBACK (released after cooldown) or CLOUD_ONLY (permanent)."""

    def __init__(
        self,
        cloud: CloudPort,
        clock: Clock,
        *,
        cooldown: float = 600.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cloud = cloud
        self._clock = clock
        self._cooldown = cooldown
        self._sleep = sleep
        self._reasons: dict[str, set[LeaseReason]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._release_at: dict[str, float] = {}
        self._retry_at: dict[str, float] = {}
        self._backoff: dict[str, float] = {}

    @property
    def leased(self) -> frozenset[str]:
        return frozenset(self._reasons)

    def is_leased(self, serial: str) -> bool:
        return serial in self._reasons

    def release_pending(self, serial: str) -> bool:
        return serial in self._timers

    def release_at(self, serial: str) -> float | None:
        """Monotonic time the pending release fires, if any."""
        return self._release_at.get(serial) if serial in self._timers else None

    def retry_at(self, serial: str) -> float | None:
        """Monotonic time the next acquire is allowed after a failure, if backing off."""
        return self._retry_at.get(serial)

    async def async_enter_fallback(self, serial: str, want_profile: bool = False) -> None:
        """Hold a FALLBACK lease; cancels a pending release instead of re-acquiring."""
        async with self._lock(serial):
            self._cancel_timer(serial)
            if serial in self._reasons:
                return
            await self._acquire(serial, LeaseReason.FALLBACK, want_profile)

    async def async_leave_fallback(self, serial: str) -> None:
        """Release the FALLBACK lease after the cooldown unless re-entered first."""
        held = self._reasons.get(serial, set())
        if held != {LeaseReason.FALLBACK} or serial in self._timers:
            return
        self._release_at[serial] = self._clock.monotonic() + self._cooldown
        self._timers[serial] = asyncio.create_task(self._release_later(serial))

    async def async_hold_permanent(self, serial: str, want_profile: bool = False) -> None:
        """Hold a CLOUD_ONLY lease (replaces a FALLBACK lease)."""
        async with self._lock(serial):
            self._cancel_timer(serial)
            if LeaseReason.CLOUD_ONLY in self._reasons.get(serial, ()):
                return
            if not await self._acquire(serial, LeaseReason.CLOUD_ONLY, want_profile):
                return
            for reason in self._reasons[serial] - {LeaseReason.CLOUD_ONLY}:
                self._reasons[serial].discard(reason)
                await self._safe_release(serial, reason)

    async def async_drop(self, serial: str) -> None:
        """Release now, e.g. when a device is removed."""
        async with self._lock(serial):
            self._cancel_timer(serial)
            await self._release(serial)

    async def async_close(self) -> None:
        """Cancel timers and release every lease."""
        for serial in list(self._timers):
            self._cancel_timer(serial)
        for serial in list(self._reasons):
            await self.async_drop(serial)

    def _lock(self, serial: str) -> asyncio.Lock:
        return self._locks.setdefault(serial, asyncio.Lock())

    async def _acquire(self, serial: str, reason: LeaseReason, want_profile: bool) -> bool:
        """Acquire unless backing off; True once held. Raises the cloud error on failure."""
        retry_at = self._retry_at.get(serial)
        if retry_at is not None and self._clock.monotonic() < retry_at:
            _LOGGER.debug("Cloud lease for %s backing off", serial)
            return False
        try:
            await self._cloud.async_acquire(serial, reason, want_profile=want_profile)
        except KumoError as err:
            self._failed(serial, err)
            raise
        except BaseException:
            # Cancelled mid-acquire: the transport may hold a partial lease.
            await self._safe_release(serial, reason)
            raise
        if self._backoff.pop(serial, None) is not None:
            _LOGGER.info("Cloud lease for %s acquired again", serial)
        self._retry_at.pop(serial, None)
        self._reasons.setdefault(serial, set()).add(reason)
        return True

    def _failed(self, serial: str, err: KumoError) -> None:
        previous = self._backoff.get(serial)
        delay = BACKOFF_INITIAL_S if previous is None else min(previous * 2, BACKOFF_MAX_S)
        self._backoff[serial] = delay
        self._retry_at[serial] = self._clock.monotonic() + delay
        level = logging.WARNING if previous is None else logging.DEBUG
        _LOGGER.log(level, "Cloud lease for %s failed, retry in %.0f s: %s", serial, delay, err)

    def _cancel_timer(self, serial: str) -> None:
        self._release_at.pop(serial, None)
        task = self._timers.pop(serial, None)
        if task is not None:
            task.cancel()

    async def _release_later(self, serial: str) -> None:
        await self._sleep(self._cooldown)
        async with self._lock(serial):
            if self._timers.get(serial) is not asyncio.current_task():
                return
            self._timers.pop(serial, None)
            self._release_at.pop(serial, None)
            await self._release(serial)

    async def _release(self, serial: str) -> None:
        for reason in sorted(self._reasons.pop(serial, ())):
            await self._safe_release(serial, reason)

    async def _safe_release(self, serial: str, reason: LeaseReason) -> None:
        try:
            await self._cloud.async_release(serial, reason)
        except KumoError as err:
            _LOGGER.warning("Releasing cloud lease for %s failed: %s", serial, err)
