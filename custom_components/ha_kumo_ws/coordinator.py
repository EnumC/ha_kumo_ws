"""Per-device coordinator: node scheduling, optimistic commands, ordered writes."""

import asyncio
import dataclasses
import itertools
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CLOUD_BACKSTOP_TICK_S,
    DOMAIN,
    HOLD_TTL_CLOUD,
    HOLD_TTL_LOCAL,
    POST_WRITE_REFRESH_S,
)
from .pykumo2.clock import Clock
from .pykumo2.domain.capabilities import Capabilities
from .pykumo2.domain.commands import (
    Batch,
    Command,
    InjectRoomTemp,
    RebootAdapter,
    SetSetpoints,
    optimistic_values,
)
from .pykumo2.domain.enums import LinkState
from .pykumo2.domain.holds import HoldTable
from .pykumo2.domain.state import DeviceState, StatePatch, apply_patch
from .pykumo2.errors import (
    AuthenticationError,
    CredentialsMissingError,
    KumoError,
    RateLimitedError,
)
from .pykumo2.routing.policy import FALLBACK_STATES, NoRouteError
from .pykumo2.routing.router import DeviceLink
from .pykumo2.transport import Node, TransportKind

if TYPE_CHECKING:
    from .hub import KumoHub

_LOGGER = logging.getLogger(__name__)

LOCAL_LINK_STATES = frozenset({LinkState.LOCAL_OK, LinkState.LOCAL_DEGRADED, LinkState.RECOVERING})
SLOW_NODE_S = 300.0
PROFILE_NODE_S = 86400.0
CLOUD_META_KEYS = frozenset({"name", "model_number", "connected", "error_code", "link_state"})

type CommandBuilder = Callable[[DeviceState], Command]


@dataclass(slots=True)
class KumoDevice:
    """Inventory record for one indoor unit."""

    serial: str
    name: str
    site_id: str = ""
    mac: str = ""
    model: str | None = None
    has_mhk2: bool | None = None


class NodeScheduler:
    """Which nodes a poll reads: status always, slow nodes every 5 min, profile daily."""

    def __init__(self, clock: Clock, *, has_mhk2: bool | None = None) -> None:
        self._clock = clock
        self._last: dict[Node, float] = {}
        self._profile_wanted = True
        self._has_mhk2 = has_mhk2

    def _elapsed(self, node: Node, now: float) -> float:
        last = self._last.get(node)
        return math.inf if last is None else now - last

    def due(self) -> frozenset[Node]:
        now = self._clock.monotonic()
        nodes: set[Node] = {"status"}
        nodes.update(n for n in ("sensors", "adapter") if self._elapsed(n, now) >= SLOW_NODE_S)
        profile_age = self._elapsed("profile", now)
        if profile_age >= PROFILE_NODE_S or (self._profile_wanted and profile_age >= SLOW_NODE_S):
            nodes.add("profile")
        if self._has_mhk2 is not False and self._elapsed("mhk2", now) >= SLOW_NODE_S:
            nodes.add("mhk2")
        return frozenset(nodes)

    def done(self, nodes: frozenset[Node], values: Mapping[str, Any]) -> None:
        """Record a successful local read of nodes."""
        now = self._clock.monotonic()
        for node in nodes:
            self._last[node] = now
        if "profile" in nodes and "capabilities" in values:
            self._profile_wanted = False
        if "mhk2" in nodes and self._has_mhk2 is None:
            self._has_mhk2 = "mhk2_humidity" in values

    def request_profile(self) -> None:
        """Read the profile on the next poll (start, reboot)."""
        self._profile_wanted = True
        self._last.pop("profile", None)


class KumoDeviceCoordinator(DataUpdateCoordinator[DeviceState]):
    """State of one unit; commands run one at a time in call order."""

    # Lock covers build/hold/execute; reads started before a write never revert it
    # (HoldTable generations); failures roll back only owned keys; exclusive jobs run alone.

    data: DeviceState
    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: KumoHub,
        device: KumoDevice,
        link: DeviceLink,
        *,
        poll_interval: timedelta,
        seed: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device.serial}",
            update_interval=poll_interval,
        )
        self.hub = hub
        self.device = device
        self.link = link
        self.serial = device.serial
        self.capabilities = Capabilities.default()
        self.holds = HoldTable()
        self._clock = hub.clock
        self._poll_interval = poll_interval
        self._scheduler = NodeScheduler(hub.clock, has_mhk2=device.has_mhk2)
        self._command_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._tickets = itertools.count(1)
        self._segment = 0
        self._coalesce_key: str | None = None
        self._queued: dict[int, tuple[int, str | None, Command | CommandBuilder]] = {}
        self._command_tasks: set[asyncio.Task[HomeAssistantError | None]] = set()
        self._results: dict[int, asyncio.Future[HomeAssistantError | None]] = {}
        self._started: dict[int, asyncio.Event] = {}
        self._waiting: set[int] = set()
        self._unsub_hold: CALLBACK_TYPE | None = None
        self._closing = False
        self._caps_version = 0
        self._unsub_post_write: CALLBACK_TYPE | None = None
        self._post_write_job = HassJob(self._async_post_write_refresh, cancel_on_shutdown=True)
        self._sw_version: str | None = None
        initial = DeviceState(device.serial, name=device.name, model_number=device.model)
        if seed:
            local = hub.local is not None and hub.local.has_unit(self.serial)
            values = _cloud_meta(seed) if local and link.policy.has_local else seed
            initial = apply_patch(initial, StatePatch(TransportKind.CLOUD, values, 0.0), None, 0.0)
        self.data = dataclasses.replace(initial, link_state=link.state, updated_at=None)
        self._remove_link_listener = link.add_listener(self._on_link_state)
        self._apply_interval(link.state)

    @property
    def device_available(self) -> bool:
        """True on a local link; else False when cloud reports it offline or no route exists."""
        if self.link_is_local:
            return True
        if self.data.connected is False:
            return False
        state = self.link.state
        if state in FALLBACK_STATES or state is LinkState.NO_CREDENTIALS:
            return self.link.policy.has_cloud
        return True

    @property
    def link_is_local(self) -> bool:
        """True while the unit is reachable over the local transport."""
        return self.link.state in LOCAL_LINK_STATES

    @callback
    def _publish(self, state: DeviceState) -> None:
        """Set data and notify entities without moving the poll schedule."""
        self.data = state
        self._sync_device_registry()
        self.async_update_listeners()

    def _merge(self, patch: StatePatch, since: int, caps_version: int | None = None) -> DeviceState:
        values = dict(patch.values)
        caps = values.pop("capabilities", None)
        if patch.source is TransportKind.CLOUD and self.link_is_local:
            values = _cloud_meta(values)
        if isinstance(caps, Capabilities) and (
            caps_version is None or caps_version == self._caps_version
        ):
            self._caps_version += 1
            self.capabilities = caps
            self.link.need_profile = False
        clean = StatePatch(source=patch.source, values=values, at=patch.at)
        state = apply_patch(self.data, clean, self.holds, self._clock.monotonic(), since)
        self._schedule_hold_expiry()
        return state

    @callback
    def async_handle_push(self, patch: StatePatch) -> None:
        """Apply a socket push; it counts as a read that started now."""
        self.last_update_success = True
        self._publish(self._merge(patch, self.holds.begin_read()))

    async def _async_update_data(self) -> DeviceState:
        self.hub.async_check_cloud_auth()
        nodes = self._scheduler.due()
        try:
            async with self._poll_lock:
                since = self.holds.begin_read()
                caps_version = self._caps_version
                patch = await self.link.async_poll(nodes)
        except CredentialsMissingError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_credentials") from err
        except KumoError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": type(err).__name__},
            ) from err
        if patch.source is TransportKind.LOCAL and patch.values.keys() - {"link_state"}:
            self._scheduler.done(nodes, patch.values)
        state = self._merge(patch, since, caps_version)
        self._sync_device_registry(state)
        return state

    @callback
    def _on_link_state(self, state: LinkState) -> None:
        self._apply_interval(state)
        if self.data.link_state is not state:
            self._publish(dataclasses.replace(self.data, link_state=state))

    def _apply_interval(self, state: LinkState) -> None:
        if state is LinkState.CLOUD_ONLY:
            self.update_interval = timedelta(seconds=CLOUD_BACKSTOP_TICK_S)
        else:
            self.update_interval = self._poll_interval

    def _sync_device_registry(self, state: DeviceState | None = None) -> None:
        firmware = (state or self.data).firmware_version
        if firmware is None or firmware == self._sw_version:
            return
        registry = dr.async_get(self.hass)
        entry = registry.async_get_device(identifiers={(DOMAIN, self.serial)})
        if entry is None:
            return
        if entry.sw_version != firmware:
            registry.async_update_device(entry.id, sw_version=firmware)
        self._sw_version = firmware

    async def async_run_exclusive[T](self, job: Callable[[], Awaitable[T]]) -> T | None:
        """Run job with no command or poll in flight on this unit; None once closing."""
        async with self._command_lock, self._poll_lock:
            if self._closing:
                return None
            return await job()

    async def async_execute(
        self, command: Command | CommandBuilder, *, coalesce: str | None = None
    ) -> None:
        """Run a command in call order."""
        # Same-key queued calls coalesce; other calls are barriers; delivery is shielded.
        if self._closing:
            raise _command_error(KumoError())
        if not callable(command) and not isinstance(command, SetSetpoints):
            coalesce = None
        ticket = next(self._tickets)
        if coalesce is None or coalesce != self._coalesce_key:
            self._segment += 1
        self._coalesce_key = coalesce
        self._queued[ticket] = (self._segment, coalesce, command)
        started = asyncio.Event()
        self._started[ticket] = started
        self._results[ticket] = asyncio.get_running_loop().create_future()
        self._waiting.add(ticket)
        task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_run_ticket(ticket, started),
            f"{DOMAIN} {self.serial} command",
        )
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
        try:
            error = await asyncio.shield(task)
            if error is not None:
                raise error
        except asyncio.CancelledError:
            self._waiting.discard(ticket)
            if not started.is_set():
                self._queued.pop(ticket, None)
                task.cancel()
                self._results.pop(ticket, None)
                self._started.pop(ticket, None)
            raise

    async def _async_run_ticket(
        self, ticket: int, started: asyncio.Event
    ) -> HomeAssistantError | None:
        async with self._command_lock:
            item = self._queued.get(ticket)
            if item is None:
                result = self._results.pop(ticket)
                self._started.pop(ticket, None)
                return result.result() if result.done() else None
            if self._closing:
                self._waiting.discard(ticket)
                self._queued.pop(ticket)
                self._results.pop(ticket)
                self._started.pop(ticket, None)
                return _command_error(KumoError())
            segment, key, command = item
            compatible = [
                t
                for t, (seg, k, _) in self._queued.items()
                if key is not None and seg == segment and k == key
            ]
            if compatible:
                commands = [self._queued[t][2] for t in compatible]
                for t in compatible:
                    self._queued.pop(t)

                def build(state: DeviceState) -> Command:
                    merged: Command | None = None
                    for candidate in commands:
                        raw = candidate(state) if callable(candidate) else candidate
                        if isinstance(raw, SetSetpoints) and isinstance(merged, SetSetpoints):
                            raw = SetSetpoints(
                                heat=raw.heat if raw.heat is not None else merged.heat,
                                cool=raw.cool if raw.cool is not None else merged.cool,
                            )
                        merged = raw
                    assert merged is not None
                    return merged

                command = build
            else:
                self._queued.pop(ticket)
            participants = compatible or [ticket]
            for participant in participants:
                self._started[participant].set()
            error = None
            try:
                await self._async_execute_locked(command)
            except HomeAssistantError as err:
                error = err
            except Exception as err:
                error = _command_error(err)
            for participant in participants:
                self._results[participant].set_result(error)
            task = asyncio.current_task()
            assert task is not None
            task.add_done_callback(lambda done: self._command_completed(done, participants))
            self._results.pop(ticket)
            self._started.pop(ticket, None)
            return error

    @callback
    def _command_completed(self, task: asyncio.Task[Any], participants: list[int]) -> None:
        if task.cancelled():
            return
        error = task.exception() or task.result()
        if error is not None and not self._waiting.intersection(participants):
            _LOGGER.warning(
                "Command delivery failed without a waiting caller (%s)", type(error).__name__
            )
        self._waiting.difference_update(participants)

    async def _async_execute_locked(self, command: Command | CommandBuilder) -> None:
        caps = self.capabilities
        state = dataclasses.replace(self.data, command_capabilities=caps)
        raw = command(state) if callable(command) else command
        try:
            cmd = self.link.validate(raw, state, caps)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_command",
                translation_placeholders={"error": str(err)},
            ) from err
        values = optimistic_values(cmd, state)
        before = {key: getattr(state, key) for key in values}
        token = self.holds.add(
            self.serial,
            values,
            self._ttl(self.link.active_transport),
            self._clock.monotonic(),
            in_flight=True,
        )
        if values:
            self._publish(dataclasses.replace(state, **values))
        attempted_local = self.link_is_local
        try:
            kind = await self.link.async_execute(cmd, state, caps=caps)
        except (KumoError, ValueError) as err:
            sent_keys: set[str] = set()
            cause: BaseException | None = err
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                sent_keys.update(getattr(cause, "sent_keys", set()))
                cause = cause.__context__
            self._rollback(token, {k: v for k, v in before.items() if k not in sent_keys})
            self.holds.extend(self.serial, token, HOLD_TTL_LOCAL, self._clock.monotonic())
            self._schedule_hold_expiry()
            if attempted_local and not _injection_only(cmd):
                self._schedule_post_write_refresh()
            elif not self._closing:
                self.config_entry.async_create_background_task(
                    self.hass, self.async_request_refresh(), f"{DOMAIN} {self.serial} rollback"
                )
            raise _command_error(err) from err
        self.holds.extend(self.serial, token, self._ttl(kind), self._clock.monotonic())
        if isinstance(cmd, RebootAdapter):
            self._scheduler.request_profile()
        self._schedule_hold_expiry()
        if (attempted_local or kind is TransportKind.LOCAL) and not _injection_only(cmd):
            self._schedule_post_write_refresh()

    @callback
    def _schedule_hold_expiry(self) -> None:
        if self._unsub_hold is not None:
            self._unsub_hold()
            self._unsub_hold = None
        deadline = self.holds.conflict_deadline(self.serial)
        if not self._closing and deadline is not None and math.isfinite(deadline):
            self._unsub_hold = async_call_later(
                self.hass, max(0, deadline - self._clock.monotonic()), self._expire_holds
            )

    @callback
    def _expire_holds(self, _now: Any) -> None:
        self._unsub_hold = None
        if self._closing:
            return
        values = self.holds.expire(self.serial, self._clock.monotonic())
        if values:
            self._publish(dataclasses.replace(self.data, **values))
            if self.link_is_local:
                self.config_entry.async_create_background_task(
                    self.hass, self.async_request_refresh(), f"{DOMAIN} hold expiry"
                )
        self._schedule_hold_expiry()

    def _rollback(self, token: int, before: Mapping[str, Any]) -> None:
        released = self.holds.rollback_token(self.serial, token, before, self._clock.monotonic())
        restore = {key: before[key] for key in released if key in before}
        if restore:
            self._publish(dataclasses.replace(self.data, **restore))

    @staticmethod
    def _ttl(kind: TransportKind | None) -> float:
        return HOLD_TTL_LOCAL if kind is TransportKind.LOCAL else HOLD_TTL_CLOUD

    @callback
    def _schedule_post_write_refresh(self) -> None:
        """One pending status refresh about 8 s after the latest local write."""
        if self._closing:
            return
        if self._unsub_post_write is not None:
            self._unsub_post_write()
        self._unsub_post_write = async_call_later(
            self.hass, POST_WRITE_REFRESH_S, self._post_write_job
        )

    @callback
    def _async_post_write_refresh(self, _now: Any) -> None:
        self._unsub_post_write = None
        if self._closing:
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} {self.serial} post-write"
        )

    @property
    def post_write_refresh_pending(self) -> bool:
        return self._unsub_post_write is not None

    async def async_shutdown(self) -> None:
        self._closing = True
        await asyncio.gather(*self._command_tasks, return_exceptions=True)
        self._queued.clear()
        if self._unsub_post_write is not None:
            self._unsub_post_write()
            self._unsub_post_write = None
        if self._unsub_hold is not None:
            self._unsub_hold()
            self._unsub_hold = None
        self._remove_link_listener()
        await super().async_shutdown()


def _command_error(err: Exception) -> HomeAssistantError:
    if isinstance(err, NoRouteError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key=err.reason)
    if isinstance(err, ValueError):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_command",
            translation_placeholders={"error": str(err)},
        )
    if isinstance(err, RateLimitedError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="cloud_rate_limited")
    if isinstance(err, AuthenticationError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="cloud_auth_failed")
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_failed",
        translation_placeholders={"error": type(err).__name__},
    )


def _cloud_meta(values: Mapping[str, Any]) -> dict[str, Any]:
    """Cloud values that do not compete with local state."""
    return {k: v for k, v in values.items() if k in CLOUD_META_KEYS}


def _injection_only(command: Command) -> bool:
    if isinstance(command, Batch):
        return bool(command.commands) and all(_injection_only(sub) for sub in command.commands)
    return isinstance(command, InjectRoomTemp)
