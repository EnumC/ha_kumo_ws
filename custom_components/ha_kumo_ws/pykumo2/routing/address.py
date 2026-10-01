"""Finds a unit's IP again: pinned override > DHCP hint > rate-limited subnet rescan."""

import asyncio
import logging
from collections.abc import Callable

from ..clock import Clock
from ..errors import KumoError, LocalError
from .ports import CredentialPort, LocalPort, Scanner

_LOGGER = logging.getLogger(__name__)


class AddressResolver:
    """One per entry; lost units share a single in-flight rescan."""

    def __init__(
        self,
        creds: CredentialPort,
        local: LocalPort,
        scanner: Scanner,
        clock: Clock,
        *,
        cidrs_provider: Callable[[], list[str]],
        rescan_interval: float = 900.0,
    ) -> None:
        self._creds = creds
        self._local = local
        self._scanner = scanner
        self._clock = clock
        self._cidrs = cidrs_provider
        self._interval = rescan_interval
        self._hints: dict[str, str] = {}
        self._lost: set[str] = set()
        self._last_scan: float | None = None
        self._last_matches: dict[str, str] = {}
        self._inflight: asyncio.Task[dict[str, str]] | None = None

    @property
    def lost(self) -> frozenset[str]:
        return frozenset(self._lost)

    def note_dhcp(self, serial: str, ip: str) -> None:
        """Remember an address seen via DHCP; probed before any rescan."""
        self._hints[serial] = ip

    def mark_lost(self, serial: str) -> None:
        self._lost.add(serial)

    def mark_found(self, serial: str) -> None:
        self._lost.discard(serial)

    async def async_resolve(self, serial: str) -> str | None:
        """Return a working address for serial, updating creds and local on change."""
        unit = self._creds.get(serial)
        if unit is not None and unit.address_pinned:
            self._lost.discard(serial)
            return unit.address
        hint = self._hints.pop(serial, None)
        if hint is not None and await self._try_hint(serial, hint, unit.address if unit else ""):
            return hint
        return (await self._rescan()).get(serial)

    async def async_close(self) -> None:
        if self._inflight is not None:
            self._inflight.cancel()
            self._inflight = None

    async def _try_hint(self, serial: str, hint: str, previous: str) -> bool:
        self._local.update_address(serial, hint)
        try:
            ok = await self._local.async_probe(serial)
        except LocalError:
            ok = False
        if ok:
            await self._creds.async_set_address(serial, hint)
            self._lost.discard(serial)
            return True
        if previous:
            self._local.update_address(serial, previous)
        return False

    async def _rescan(self) -> dict[str, str]:
        if self._inflight is None:
            now = self._clock.monotonic()
            if self._last_scan is not None and now - self._last_scan < self._interval:
                return self._last_matches
            self._last_scan = now
            task = asyncio.create_task(self._scan())
            task.add_done_callback(self._scan_done)
            self._inflight = task
        return await asyncio.shield(self._inflight)

    def _scan_done(self, task: asyncio.Task[dict[str, str]]) -> None:
        if self._inflight is task:
            self._inflight = None

    async def _scan(self) -> dict[str, str]:
        cidrs = self._cidrs()
        if not cidrs:
            return {}
        try:
            ips = await self._scanner.scan(cidrs)
            serials = sorted(s for s in self._lost if not self._pinned(s))
            matches = await self._scanner.match(ips, serials) if ips and serials else {}
        except (OSError, KumoError) as err:
            _LOGGER.warning("Subnet rescan failed: %s", err)
            return {}
        for serial, ip in matches.items():
            await self._creds.async_set_address(serial, ip)
            self._local.update_address(serial, ip)
            self._lost.discard(serial)
        _LOGGER.debug("Rescan matched %s", sorted(matches))
        self._last_matches = matches
        return matches

    def _pinned(self, serial: str) -> bool:
        unit = self._creds.get(serial)
        return unit is not None and unit.address_pinned
