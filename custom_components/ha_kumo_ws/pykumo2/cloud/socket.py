"""Lazy, lease-counted Kumo Cloud Socket.IO session."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import socketio

from ..clock import Clock
from ..credentials.models import Secret
from ..errors import CloudError, KumoError
from ..transport import LeaseReason
from ..util.redact import redact
from .budget import CloudCallLedger
from .const import SOCKET_URL
from .tokens import TokenSource

_LOGGER = logging.getLogger(__name__)

type SocketListener = Callable[[str, Mapping[str, Any]], None]
type _Observer = Callable[[str, str, Mapping[str, Any]], None]

FORWARDED_EVENTS = (
    "device_update",
    "device_status_v2",
    "profile_update",
    "adapter_update",
    "acoil_update",
)
# force_adapter_request type -> event that answers it; others are fire-and-forget.
_ANSWERED_BY = {
    "iuStatus": "device_update",
    "profile": "profile_update",
    "adapterStatus": "adapter_update",
}
_STATUS_REASONS = frozenset({LeaseReason.FALLBACK, LeaseReason.CLOUD_ONLY})
_AUTH_HINTS = ("auth", "token", "jwt", "expired", "unauthor", "401")
# python-socketio disconnect reasons. After a server disconnect it does not reconnect.
_SERVER_DISCONNECT = "server disconnect"
_CLIENT_DISCONNECT = "client disconnect"
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0


class CloudSocketSession:
    """Connects only while leases exist and subscribes only the leased serials."""

    def __init__(
        self,
        tokens: TokenSource,
        ledger: CloudCallLedger,
        clock: Clock,
        *,
        client_factory: Callable[..., Any] = socketio.AsyncClient,
        idle_disconnect: float = 600.0,
        refresh_on_connect: bool = False,
        socket_url: str = SOCKET_URL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._ledger = ledger
        self._clock = clock
        self._factory = client_factory
        self._idle_s = idle_disconnect
        self._refresh_on_connect = refresh_on_connect
        self._url = socket_url
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._leases: set[tuple[str, LeaseReason]] = set()
        self._pending_force: dict[str, set[str]] = {}
        self._subscribed: set[str] = set()
        self._client: Any = None
        self._connected = False
        self._idle_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_delay = RECONNECT_MIN_S
        self._builtin_reconnect = False
        self._connected_at = 0.0
        self._listener: SocketListener | None = None
        self._observers: list[_Observer] = []
        self._last_token: str | None = None
        self._token_error: KumoError | None = None

    @property
    def leases(self) -> frozenset[tuple[str, LeaseReason]]:
        return frozenset(self._leases)

    @property
    def leased(self) -> frozenset[str]:
        """Serials holding at least one lease."""
        return frozenset(serial for serial, _ in self._leases)

    @property
    def subscribed(self) -> frozenset[str]:
        """Serials subscribed on the live connection."""
        return frozenset(self._subscribed)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def reconnecting(self) -> bool:
        """True while built-in or owned reconnection is in progress."""
        task = self._reconnect_task
        return self._builtin_reconnect or (task is not None and not task.done())

    def set_listener(self, cb: SocketListener | None) -> None:
        self._listener = cb

    async def acquire(self, serial: str, reason: LeaseReason, force: Iterable[str] = ()) -> None:
        """Lease serial; connects if needed, else subscribes incrementally."""
        self._cancel_idle()
        force = tuple(force)
        async with self._lock:
            added = (serial, reason) not in self._leases
            self._leases.add((serial, reason))
            self._pending_force.setdefault(serial, set()).update(force)
            try:
                if self._client is None or not (self._connected or self.reconnecting):
                    await self._reconnect()
                elif self._connected:
                    if serial not in self._subscribed:
                        await self._subscribe(serial)
                    else:
                        await self._force(serial, force)
            except BaseException:
                if added:
                    await self._forget(serial, reason)
                if self._leases and self._client is None:
                    self._schedule_reconnect()
                raise

    async def release(self, serial: str, reason: LeaseReason) -> None:
        """Drop a lease; unsubscribes on the serial's last lease, idles out on the last."""
        async with self._lock:
            await self._forget(serial, reason)
            if not self._leases and self._client is not None:
                self._cancel_idle()
                self._idle_task = asyncio.create_task(self._idle_disconnect())

    async def async_close(self) -> None:
        """Drop all leases and disconnect; acquire() reconnects later."""
        await _cancel_and_wait(self._cancel_idle())
        task, self._reconnect_task = self._reconnect_task, None
        await _cancel_and_wait(task)
        async with self._lock:
            self._leases.clear()
            self._pending_force.clear()
            await self._drop_client()

    async def request_adapter_status(
        self,
        serials: Iterable[str],
        timeout: float = 60.0,  # noqa: ASYNC109 (partial result)
    ) -> dict[str, Secret]:
        """Collect adapter passwords from adapter_update; partial results on timeout."""
        wanted = set(serials)
        found: dict[str, Secret] = {}
        done = asyncio.Event()

        def observe(event: str, serial: str, payload: Mapping[str, Any]) -> None:
            password = payload.get("password")
            if event != "adapter_update" or serial not in wanted or serial in found:
                return
            if isinstance(password, str) and password:
                found[serial] = Secret(password)
                if found.keys() >= wanted:
                    done.set()

        self._observers.append(observe)
        reason = LeaseReason.CREDENTIAL_REFRESH
        try:
            for serial in sorted(wanted):
                await self.acquire(serial, reason, force=("adapterStatus",))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    await done.wait()
        finally:
            self._observers.remove(observe)
            for serial in sorted(wanted):
                await self.release(serial, reason)
        missing = wanted - found.keys()
        if missing:
            _LOGGER.debug("No adapter password for %s", sorted(missing))
        return found

    async def _forget(self, serial: str, reason: LeaseReason) -> None:
        self._leases.discard((serial, reason))
        if serial in self.leased:
            return
        self._pending_force.pop(serial, None)
        if serial in self._subscribed:
            self._subscribed.discard(serial)
            await self._emit("unsubscribe", serial)

    async def _reconnect(self) -> None:
        await self._drop_client()
        await self._connect()

    def _schedule_reconnect(self) -> None:
        if not self._leases or self.reconnecting:
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Owned reconnect with backoff; python-socketio gives up after a server disconnect."""
        while True:
            delay = self._reconnect_delay
            self._reconnect_delay = min(delay * 2, RECONNECT_MAX_S)
            await self._sleep(delay)
            async with self._lock:
                if not self._leases or self._connected:
                    return
                try:
                    await self._reconnect()
                except KumoError as err:
                    _LOGGER.debug("Cloud socket reconnect failed: %s", type(err).__name__)
                    continue
                return

    async def _connect(self) -> None:
        client = self._factory(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=2,
            reconnection_delay_max=60,
            logger=False,
            engineio_logger=False,
        )
        self._register(client)
        self._client = client
        self._token_error = None
        try:
            await client.connect(
                self._url,
                headers=self._headers,
                auth=self._auth,
                transports=["websocket", "polling"],
                wait_timeout=10,
            )
        except BaseException as err:
            await self._drop_client()
            if not isinstance(err, Exception) or isinstance(err, KumoError):
                raise
            if self._token_error is not None:
                raise self._token_error from err
            raise CloudError(f"socket connect failed: {type(err).__name__}") from err

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        self._builtin_reconnect = False
        self._subscribed.clear()
        if client is None:
            return
        _LOGGER.debug("Cloud socket closing")
        try:
            await client.shutdown()
        except Exception as err:
            _LOGGER.debug("Socket shutdown failed: %s", type(err).__name__)

    async def _token(self) -> str:
        """Fresh token per (re)connect attempt; ValueError keeps built-in reconnect going."""
        try:
            token = await self._tokens.async_access_token()
        except KumoError as err:
            self._token_error = err
            _LOGGER.debug("Socket token unavailable: %s", type(err).__name__)
            raise ValueError("no access token") from err
        self._last_token = token
        return token

    async def _headers(self) -> dict[str, str]:
        token = await self._token()
        self._ledger.record("socket_connect")
        return {"Authorization": f"Bearer {token}"}

    async def _auth(self) -> dict[str, str]:
        return {"token": await self._token()}

    def _cancel_idle(self) -> asyncio.Task[None] | None:
        task, self._idle_task = self._idle_task, None
        if task is not None and not task.done():
            task.cancel()
            return task
        return None

    async def _idle_disconnect(self) -> None:
        await self._sleep(self._idle_s)
        self._idle_task = None
        async with self._lock:
            if not self._leases:
                await self._drop_client()

    def _register(self, client: Any) -> None:
        async def on_connect() -> None:
            if client is not self._client:
                await client.disconnect()
                return
            await self._on_connect()

        async def on_disconnect(reason: Any = None, *_: Any) -> None:
            if client is self._client:
                self._on_disconnect(reason)

        async def on_connect_error(data: Any = None, *_: Any) -> None:
            if client is self._client:
                await self._on_connect_error(data)

        client.on("connect", on_connect)
        client.on("disconnect", on_disconnect)
        client.on("connect_error", on_connect_error)
        for event in FORWARDED_EVENTS:
            client.on(event, self._event_handler(client, event))

    def _event_handler(self, client: Any, event: str) -> Callable[..., Awaitable[None]]:
        async def handler(payload: Any = None, *_: Any) -> None:
            if client is self._client:
                self._on_event(event, payload)

        return handler

    async def _on_connect(self) -> None:
        _LOGGER.debug("Cloud socket connected")
        self._connected = True
        self._builtin_reconnect = False
        self._connected_at = self._clock.monotonic()
        self._subscribed.clear()
        if self._refresh_on_connect:
            for serial, reason in self._leases:
                if reason in _STATUS_REASONS:
                    self._pending_force.setdefault(serial, set()).add("iuStatus")
        user_id = self._tokens.user_id
        if user_id:
            await self._emit("subscribe", ("", user_id))
        for serial in sorted(self.leased):
            # Releases and acquires may interleave with these emits.
            if serial in self.leased and serial not in self._subscribed:
                await self._subscribe(serial)

    def _on_disconnect(self, reason: Any) -> None:
        _LOGGER.debug("Cloud socket disconnected")
        if self._connected and self._clock.monotonic() - self._connected_at >= RECONNECT_MAX_S:
            self._reconnect_delay = RECONNECT_MIN_S
        self._connected = False
        self._subscribed.clear()
        if reason == _SERVER_DISCONNECT:
            self._schedule_reconnect()
        elif reason != _CLIENT_DISCONNECT:
            self._builtin_reconnect = True

    async def _on_connect_error(self, data: Any) -> None:
        text = str(data).lower()
        auth_failed = any(hint in text for hint in _AUTH_HINTS)
        _LOGGER.debug("Socket connect_error: %s", "authentication" if auth_failed else "connection")
        token = self._last_token
        if token is None or not auth_failed:
            return
        try:
            await self._tokens.async_invalidate(token)
        except KumoError as err:
            _LOGGER.debug("Token renewal after connect_error failed: %s", type(err).__name__)

    def _on_event(self, event: str, payload: Any) -> None:
        serial = payload.get("deviceSerial") if isinstance(payload, Mapping) else None
        if not isinstance(serial, str) or serial not in self.leased:
            _LOGGER.debug("Ignoring %s for unleased serial %r", event, serial)
            return
        _LOGGER.debug("Socket %s: %s", event, redact(payload))
        pending = self._pending_force.get(serial)
        if pending:
            pending.difference_update(t for t, ev in _ANSWERED_BY.items() if ev == event)
        for observer in list(self._observers):
            observer(event, serial, payload)
        if self._listener is not None:
            try:
                self._listener(event, payload)
            except Exception:
                _LOGGER.exception("Socket listener failed for %s", event)

    async def _subscribe(self, serial: str) -> None:
        self._subscribed.add(serial)
        await self._emit("subscribe", serial)
        await self._emit("device_status_v2", serial)
        await self._force(serial, tuple(self._pending_force.get(serial, ())))

    async def _force(self, serial: str, types: Iterable[str]) -> None:
        pending = self._pending_force.get(serial, set())
        for request_type in sorted(types):
            await self._emit("force_adapter_request", (serial, request_type))
            if request_type not in _ANSWERED_BY:
                pending.discard(request_type)

    async def _emit(self, event: str, data: Any) -> None:
        client = self._client
        if client is None:
            return
        self._ledger.record("socket_emit")
        _LOGGER.debug("Socket emit %s %s", event, data)
        try:
            await client.emit(event, data)
        except Exception as err:
            _LOGGER.debug("Socket emit %s failed: %s", event, type(err).__name__)


async def _cancel_and_wait(task: asyncio.Task[None] | None) -> None:
    """Cancel task and wait for it; the caller's own cancellation still propagates."""
    if task is None:
        return
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled() and (exc := task.exception()) is not None:
        _LOGGER.debug("Socket task failed: %s", type(exc).__name__)
