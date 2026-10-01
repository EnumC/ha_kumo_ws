"""DeviceLink: per-device state machine choosing local or cloud for polls and commands."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from ..clock import Clock
from ..domain.capabilities import Capabilities
from ..domain.commands import Command, CommandValidator
from ..domain.enums import ConnectionMode, LinkState
from ..domain.state import DeviceState, StatePatch
from ..errors import (
    CredentialsMissingError,
    KumoError,
    LocalAuthError,
    LocalConnectionError,
    LocalError,
    LocalTimeoutError,
)
from ..transport import Node, TransportKind
from .address import AddressResolver
from .health import HealthTracker
from .policy import FALLBACK_STATES, RoutingPolicy
from .ports import CloudPort, CredentialPort, LocalPort
from .session_manager import CloudLeaseManager

_LOGGER = logging.getLogger(__name__)

BACKSTOP_INTERVAL = 900.0
PROFILE_RETRY_S = 60.0
PROFILE_RETRY_MAX_S = 3600.0
AUTH_REFRESH_REASON = "auth_stale"

type LinkListener = Callable[[LinkState], None]


class DeviceLink:
    """Routes one unit. auto: local primary, cloud socket lease while the circuit is open."""

    def __init__(
        self,
        serial: str,
        *,
        policy: RoutingPolicy,
        health: HealthTracker,
        local: LocalPort | None,
        cloud: CloudPort | None,
        creds: CredentialPort | None,
        addresses: AddressResolver | None,
        leases: CloudLeaseManager | None,
        clock: Clock,
        validator: CommandValidator,
        backstop_interval: float = BACKSTOP_INTERVAL,
    ) -> None:
        if policy.has_local and local is None:
            raise ValueError("policy uses local but no local transport given")
        if policy.has_cloud and (cloud is None or leases is None):
            raise ValueError("policy uses cloud but no cloud transport or lease manager given")
        self.serial = serial
        self.policy = policy
        self.health = health
        self._local = local if policy.has_local else None
        self._cloud = cloud if policy.has_cloud else None
        self._leases = leases if policy.has_cloud else None
        self._creds = creds
        self._addresses = addresses
        self._clock = clock
        self._validator = validator
        self._backstop = backstop_interval
        self._state = LinkState.LOCAL_OK
        self._listeners: list[LinkListener] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._resolve_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._backstop_failing = False
        self._probe_lock = asyncio.Lock()
        self._cloud_ref = clock.monotonic()
        self._closed = False
        self._profile_retry_at = 0.0
        self._profile_delay = PROFILE_RETRY_S
        self.need_profile = False
        self.refresh_error: str | None = None

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def active_transport(self) -> TransportKind | None:
        return self.policy.poll_transport(self._state)

    @property
    def cloud_backstop_due(self) -> bool:
        """True when no push or cloud fetch arrived for backstop_interval."""
        return self._clock.monotonic() - self._cloud_ref >= self._backstop

    @property
    def _auto(self) -> bool:
        return self.policy.mode is ConnectionMode.AUTO

    def add_listener(self, cb: LinkListener) -> Callable[[], None]:
        """Call cb with the new LinkState on every change."""
        self._listeners.append(cb)

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

    async def async_start(self) -> None:
        if self.policy.mode is ConnectionMode.CLOUD_ONLY:
            self._set_state(LinkState.CLOUD_ONLY)
            self._cloud_ref = self._clock.monotonic()
            await self._ensure_permanent()
            return
        if self._local is None or not self._local.has_unit(self.serial):
            self._set_state(LinkState.NO_CREDENTIALS)
            if self._auto:
                self._cloud_ref = self._clock.monotonic()
                await self._ensure_lease()
            return
        self.health.reset()
        self._set_state(LinkState.LOCAL_OK)

    async def async_poll(self, nodes: frozenset[Node]) -> StatePatch:
        """Read state from the active transport. Fallback polls are usually empty."""
        if self._closed:
            return self._empty()
        await self._retry_profile()
        if self._state is LinkState.CLOUD_ONLY:
            await self._ensure_permanent()
            return await self._cloud_poll(nodes)
        if self._state is LinkState.NO_CREDENTIALS:
            if self._local is not None and self._local.has_unit(self.serial):
                await self._credentials_arrived()
            elif not self._auto:
                raise CredentialsMissingError(self.serial)
        if self.policy.local_ready(self._state):
            return await self._local_poll(nodes)
        await self._ensure_lease()
        if self._local is not None and self.health.probe_due:
            await self._probe()
            if self.policy.local_ready(self._state):
                return await self._local_poll(nodes)
        return await self._cloud_poll(nodes)

    def on_push(self, patch: StatePatch) -> StatePatch:
        """Record a socket push (resets the backstop) and hand it on."""
        self._cloud_ref = self._clock.monotonic()
        if "capabilities" in patch.values:
            self.need_profile = False
        return patch

    def validate(
        self, command: Command, state: DeviceState, caps: Capabilities | None = None
    ) -> Command:
        """Normalized command or ValueError."""
        return self._validator.validate(command, caps or Capabilities.default(), state)

    async def async_execute(
        self, command: Command, state: DeviceState, *, caps: Capabilities | None = None
    ) -> TransportKind:
        """Validate and run command; returns the transport that executed it."""
        cmd = self.validate(command, state, caps)
        local_ok = self._local is not None and self._local.supports(cmd)
        cloud_ok = self._cloud is not None and self._cloud.supports(cmd)
        kind = self.policy.command_transport(cmd, self._state, local_ok, cloud_ok)
        if kind is TransportKind.CLOUD:
            assert self._cloud is not None
            await self._cloud.async_execute(self.serial, cmd, state)
            return kind
        assert self._local is not None
        try:
            await self._local.async_execute(self.serial, cmd, state)
        except LocalError as err:
            await self._on_local_failure(err)
            if not (self.policy.allow_cloud_retry_on_local_failure and cloud_ok):
                raise
            assert self._cloud is not None
            _LOGGER.debug("Local command failed for %s, retrying on cloud: %s", self.serial, err)
            await self._cloud.async_execute(self.serial, cmd, state)
            return TransportKind.CLOUD
        await self._on_local_success()
        return kind

    async def async_on_credentials_changed(self) -> None:
        """New or refreshed credentials: probe now."""
        if self._state is LinkState.NO_CREDENTIALS:
            if self._local is not None and self._local.has_unit(self.serial):
                await self._credentials_arrived()
            return
        if self._auto and self._state in FALLBACK_STATES:
            await self._probe()

    async def async_on_address_changed(self) -> None:
        """Address updated (DHCP, option, rescan): probe now."""
        if self._auto and self._state in FALLBACK_STATES:
            await self._probe()

    async def async_drain(self) -> None:
        """Wait for background refresh/resolve tasks."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def async_close(self) -> None:
        """Cancel background work and release this serial's lease."""
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        await self.async_drain()
        self._listeners.clear()
        if self._leases is not None:
            await self._leases.async_drop(self.serial)

    async def _local_poll(self, nodes: frozenset[Node]) -> StatePatch:
        assert self._local is not None
        try:
            patch = await self._local.async_fetch(self.serial, nodes)
        except LocalError as err:
            await self._on_local_failure(err)
            if not self._auto:
                raise
            return self._empty()
        await self._on_local_success()
        return self._tag(patch)

    async def _cloud_poll(self, nodes: frozenset[Node]) -> StatePatch:
        """Backstop fetch; a failure keeps the last state and waits another interval."""
        if self._cloud is None or not self.cloud_backstop_due:
            return self._empty()
        self._cloud_ref = self._clock.monotonic()
        try:
            patch = await self._cloud.async_fetch(self.serial, nodes)
        except KumoError as err:
            level = logging.DEBUG if self._backstop_failing else logging.WARNING
            _LOGGER.log(level, "Cloud backstop fetch for %s failed: %s", self.serial, err)
            self._backstop_failing = True
            return self._empty()
        if self._backstop_failing:
            _LOGGER.info("Cloud backstop fetch for %s recovered", self.serial)
            self._backstop_failing = False
        return self._tag(patch)

    def _tag(self, patch: StatePatch) -> StatePatch:
        values = {**patch.values, "link_state": self._state}
        return StatePatch(source=patch.source, values=values, at=patch.at)

    def _empty(self) -> StatePatch:
        source = self.active_transport or TransportKind.LOCAL
        return StatePatch(source=source, values={"link_state": self._state}, at=self._clock.now())

    async def _on_local_success(self) -> None:
        was_open = self.health.is_open
        if was_open:
            self.health.reset()
        else:
            self.health.record_success()
        if self._addresses is not None and self._state is not LinkState.RECOVERING:
            self._addresses.mark_found(self.serial)
        leases = self._leases
        leaving = was_open or self._state in FALLBACK_STATES or self._state is LinkState.RECOVERING
        if leaving and leases is not None and leases.is_leased(self.serial):
            # Also reached when a command outlives a concurrent trip: cooldown release.
            if not leases.release_pending(self.serial):
                await leases.async_leave_fallback(self.serial)
            self._set_state(LinkState.RECOVERING)
            return
        self._set_state(LinkState.LOCAL_OK)

    async def _on_local_failure(self, err: LocalError) -> None:
        self.health.record_failure(err)
        if self._state is LinkState.RECOVERING:
            self.health.trip()
        if not self.health.is_open:
            self._set_state(LinkState.LOCAL_DEGRADED)
            return
        if self._auto and self._state not in FALLBACK_STATES:
            self._cloud_ref = self._clock.monotonic()
            await self._ensure_lease()
        self._enter(self._classify(err))

    def _classify(self, err: BaseException | None) -> LinkState:
        if (
            isinstance(err, LocalAuthError)
            and self.health.consecutive_auth_failures >= self.health.open_after
        ):
            return LinkState.AUTH_STALE
        if isinstance(err, LocalTimeoutError | LocalConnectionError):
            return LinkState.ADDRESS_LOST
        if self._auto:
            if self._state in FALLBACK_STATES:
                return self._state
            return LinkState.CLOUD_FALLBACK
        if self._state in (LinkState.AUTH_STALE, LinkState.ADDRESS_LOST):
            return self._state
        return LinkState.LOCAL_DEGRADED

    def _enter(self, target: LinkState) -> None:
        self._set_state(target)
        # Re-requested on every failed probe; the credential service rate-limits.
        refreshing = self._refresh_task is not None and not self._refresh_task.done()
        if target is LinkState.AUTH_STALE and self._auto and not refreshing:
            self._refresh_task = self._spawn(self._refresh())
        if target is LinkState.ADDRESS_LOST and self._addresses is not None:
            self._addresses.mark_lost(self.serial)
            if self._resolve_task is None or self._resolve_task.done():
                self._resolve_task = self._spawn(self._resolve())

    async def _credentials_arrived(self) -> None:
        if not self._auto:
            self.health.reset()
            self._set_state(LinkState.LOCAL_OK)
            return
        self.health.trip()
        self._set_state(LinkState.CLOUD_FALLBACK)
        await self._probe()

    async def _probe(self) -> None:
        """Half-open probe; recovery switches back to local and starts the lease cooldown."""
        assert self._local is not None
        async with self._probe_lock:
            if not self.health.is_open:
                return
            err: LocalError | None = None
            try:
                ok = await self._local.async_probe(self.serial)
            except LocalError as exc:
                ok, err = False, exc
            if not ok:
                self.health.record_failure(err)
                if err is not None:
                    self._enter(self._classify(err))
                return
            self.health.record_success()
            if self.health.is_open:
                return
            self._set_state(LinkState.RECOVERING)
            if self._addresses is not None:
                self._addresses.mark_found(self.serial)
            if self._leases is not None:
                await self._leases.async_leave_fallback(self.serial)

    async def _refresh(self) -> None:
        if self._creds is None:
            return
        result = await self._creds.async_request_refresh([self.serial], reason=AUTH_REFRESH_REASON)
        if result.error is not None or self.serial in result.refreshed:
            self.refresh_error = result.error
        if self.serial in result.refreshed and self._auto:
            await self._probe()

    async def _resolve(self) -> None:
        if self._addresses is None:
            return
        unit = self._creds.get(self.serial) if self._creds is not None else None
        previous = unit.address if unit is not None else None
        address = await self._addresses.async_resolve(self.serial)
        if address and address != previous and self._auto:
            await self._probe()

    async def _ensure_lease(self) -> None:
        if self._leases is None:
            return
        if self._leases.is_leased(self.serial) and not self._leases.release_pending(self.serial):
            return
        try:
            await self._leases.async_enter_fallback(self.serial, self.need_profile)
            self._profile_requested()
        except KumoError as err:
            _LOGGER.debug("Cloud lease for %s failed: %s", self.serial, err)

    async def _ensure_permanent(self) -> None:
        if self._leases is None or self._leases.is_leased(self.serial):
            return
        try:
            await self._leases.async_hold_permanent(self.serial, self.need_profile)
            self._profile_requested()
        except KumoError as err:
            _LOGGER.debug("Cloud lease for %s failed: %s", self.serial, err)

    def _profile_requested(self) -> None:
        if self.need_profile and self._leases is not None and self._leases.is_leased(self.serial):
            self._profile_retry_at = self._clock.monotonic() + self._profile_delay

    async def _retry_profile(self) -> None:
        if (
            not self.need_profile
            or self._cloud is None
            or self._leases is None
            or not self._leases.is_leased(self.serial)
            or self._clock.monotonic() < self._profile_retry_at
        ):
            return
        self._profile_delay = min(self._profile_delay * 2, PROFILE_RETRY_MAX_S)
        self._profile_retry_at = self._clock.monotonic() + self._profile_delay
        try:
            await self._cloud.async_request_profile(self.serial)
        except KumoError as err:
            _LOGGER.debug("Cloud profile for %s failed: %s", self.serial, type(err).__name__)

    def _set_state(self, state: LinkState) -> None:
        if state is self._state:
            return
        _LOGGER.debug("%s link %s -> %s", self.serial, self._state, state)
        self._state = state
        for cb in list(self._listeners):
            cb(state)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _LOGGER.warning("Background task for %s failed: %s", self.serial, exc)
