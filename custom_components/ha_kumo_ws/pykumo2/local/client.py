"""Async signed HTTP client for one indoor unit."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

from ..clock import Clock, SystemClock
from ..credentials.models import UnitCredentials
from ..errors import (
    LocalAuthError,
    LocalBusyError,
    LocalConnectionError,
    LocalError,
    LocalProtocolError,
    LocalTimeoutError,
)
from .const import (
    CONNECT_TIMEOUT,
    DEVICE_AUTHENTICATION_ERROR,
    NO_MEMORY,
    READ_TIMEOUT,
    SERIALIZER_ERROR,
    SET_NO_SUCH_OPTION,
    SET_OUT_OF_BOUNDS,
)
from .signing import compute_token

_LOGGER = logging.getLogger(__name__)

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
_REBOOT_INTERVAL = 30 * 60
_SYSTEM_CLOCK = SystemClock()


class _MalformedResponse(LocalProtocolError):
    """The body was not a JSON object. The connection is not reused."""


def _dumps(node: Any) -> bytes:
    return json.dumps(node, separators=(",", ":")).encode()


def _query_body(path: Sequence[str], attribute: str | None = None) -> bytes:
    leaf: Any = {} if attribute is None else {attribute: {}}
    for key in reversed(path):
        leaf = {key: leaf}
    return _dumps({"c": leaf})


def _merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            base[key] = _merge(current, value)
        else:
            base[key] = value
    return base


def _node_path(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return "-"
    node: object = payload.get("c") if isinstance(payload, dict) else None
    parts: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        key = next(iter(node))
        parts.append(str(key))
        node = node[key]
    return ".".join(parts) if parts else "-"


def _map_transport(exc: Exception) -> LocalError | None:
    if isinstance(exc, TimeoutError):
        return LocalTimeoutError("timeout")
    if isinstance(exc, ClientError | OSError):
        return LocalConnectionError("connect_failed")
    return None


def _raise_for_payload(payload: dict[str, Any]) -> None:
    """Classify a decoded object. A normal reply has an ``r`` key."""
    try:
        blob = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = ""
    if payload.get("_api_error") == DEVICE_AUTHENTICATION_ERROR:
        raise LocalAuthError(DEVICE_AUTHENTICATION_ERROR)
    if payload.get("_api_error") == SERIALIZER_ERROR or NO_MEMORY in blob:
        raise LocalBusyError(NO_MEMORY if NO_MEMORY in blob else SERIALIZER_ERROR)
    if SET_NO_SUCH_OPTION in blob or SET_OUT_OF_BOUNDS in blob:
        token = SET_NO_SUCH_OPTION if SET_NO_SUCH_OPTION in blob else SET_OUT_OF_BOUNDS
        raise LocalProtocolError(token)
    if "r" not in payload:
        raise LocalProtocolError("missing_r")


class LocalUnitClient:
    """Signed ``PUT /api`` client for a single adapter."""

    def __init__(
        self,
        address: str,
        creds: UnitCredentials,
        *,
        session_factory: Callable[[], ClientSession] | None = None,
        timeouts: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT),
        clock: Clock = _SYSTEM_CLOCK,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._address = address
        self._creds = creds
        self._session_factory = session_factory
        self._timeouts = timeouts
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._cycle_depth = 0
        self._session: ClientSession | None = None
        self._session_address: str | None = None
        self._last_reboot_at: float | None = None

    @property
    def address(self) -> str:
        """Host, or ``host:port``, the next request will use."""
        return self._address

    def set_address(self, address: str) -> None:
        """Point later requests at ``address``. The open session is dropped."""
        self._address = address

    def set_credentials(self, creds: UnitCredentials) -> None:
        """Replace the signing credentials."""
        self._creds = creds

    @asynccontextmanager
    async def cycle(self) -> AsyncIterator[None]:
        """Share one connection across the requests in this block."""
        async with self._lock:
            self._cycle_depth += 1
        try:
            yield
        finally:
            async with self._lock:
                self._cycle_depth = max(0, self._cycle_depth - 1)
                if self._cycle_depth == 0:
                    await self._close_session()

    async def request(self, body: bytes) -> dict[str, Any]:
        """PUT ``body`` and return the JSON object."""
        if not self._address:
            raise LocalConnectionError("address_not_set")
        path = _node_path(body)
        async with self._lock:
            last: LocalError | None = None
            for attempt in range(2):
                try:
                    payload = await self._exchange(body)
                except (LocalTimeoutError, LocalConnectionError) as exc:
                    last = exc
                    await self._close_session()
                    self._log(path, exc, final=attempt == 1)
                    if attempt == 0:
                        continue
                    raise
                except _MalformedResponse as exc:
                    await self._close_session()
                    self._log(path, exc, final=True)
                    raise
                except LocalError as exc:
                    await self._close_if_idle()
                    self._log(path, exc, final=True)
                    raise
                except BaseException:
                    await self._close_session()
                    raise
                else:
                    await self._close_if_idle()
                    return payload
            raise last if last is not None else LocalConnectionError("connect_failed")

    async def send(self, body: bytes) -> dict[str, Any]:
        """PUT a pre-encoded body and return the JSON object."""
        return await self.request(body)

    async def write(self, path: Sequence[str], values: Mapping[str, Any]) -> None:
        """PUT ``values`` at ``path`` as one ``{"c": ...}`` body."""
        await self.send(_command_body(path, values))

    async def reboot(self) -> None:
        """Ask the adapter to reboot."""
        await self.write(("adapter", "status"), {"runState": "reboot"})

    async def retrieve(
        self,
        path: Sequence[str],
        needed: Sequence[str],
        retries: int = 3,
    ) -> dict[str, Any]:
        """Read ``path``, falling back to one request per name in ``needed``."""
        return await self._retrieve(tuple(path), tuple(needed), retries, reboot_ok=True)

    async def aclose(self) -> None:
        """Close the shared session, if one is open."""
        async with self._lock:
            self._cycle_depth = 0
            await self._close_session()

    async def _retrieve(
        self,
        path: tuple[str, ...],
        needed: tuple[str, ...],
        retries: int,
        *,
        reboot_ok: bool,
    ) -> dict[str, Any]:
        path_s = ".".join(path) if path else "-"
        response, last_error, should_reboot = await self._read_node(path, None, retries, path_s)
        if not should_reboot and response is None:
            response, last_error, should_reboot = await self._read_attributes(
                path, needed, retries, path_s, last_error
            )
        if should_reboot and reboot_ok and self._reboot_due():
            self._last_reboot_at = self._clock.monotonic()
            _LOGGER.warning("rebooting adapter path=%s", path_s)
            try:
                await self.reboot()
            except LocalError as exc:
                last_error = exc
                self._log(path_s, exc, final=True)
            await self._sleep(5.0)
            return await self._retrieve(path, needed, retries, reboot_ok=False)
        if response is None:
            raise last_error if last_error is not None else LocalProtocolError("empty_response")
        return response

    async def _read_node(
        self,
        path: tuple[str, ...],
        attribute: str | None,
        retries: int,
        path_s: str,
    ) -> tuple[dict[str, Any] | None, LocalError | None, bool]:
        """Try one query. Busy fails immediately; auth sleeps and retries."""
        query = _query_body(path, attribute)
        last_error: LocalError | None = None
        tries = 0
        while tries < retries:
            try:
                return await self.request(query), None, False
            except LocalBusyError as exc:
                return None, exc, True
            except LocalAuthError as exc:
                last_error = exc
                self._log(path_s, exc, final=False)
                await self._sleep(1.0)
                tries += 1
            except LocalError as exc:
                return None, exc, False
        return None, last_error, False

    async def _read_attributes(
        self,
        path: tuple[str, ...],
        needed: tuple[str, ...],
        retries: int,
        path_s: str,
        last_error: LocalError | None,
    ) -> tuple[dict[str, Any] | None, LocalError | None, bool]:
        built: dict[str, Any] = {"r": {}}
        should_reboot = False
        for attribute in needed:
            sub, last_error, should_reboot = await self._read_node(
                path, attribute, retries, f"{path_s}.{attribute}"
            )
            if should_reboot:
                break
            if sub is not None and attribute in str(sub):
                _merge(built, sub)
            else:
                error = type(last_error).__name__ if last_error is not None else "empty"
                _LOGGER.warning(
                    "attribute missing path=%s attribute=%s error=%s",
                    path_s,
                    attribute,
                    error,
                )
        response = built if built.get("r") else None
        return response, last_error, should_reboot

    def _reboot_due(self) -> bool:
        last = self._last_reboot_at
        if last is None:
            return True
        return self._clock.monotonic() - last > _REBOOT_INTERVAL

    async def _exchange(self, body: bytes) -> dict[str, Any]:
        address = self._address
        creds = self._creds
        token = compute_token(creds.password_bytes(), creds.crypto_bytes(), body)
        session = await self._ensure_session(address)
        url = f"http://{address}/api?m={token}"
        timeout = ClientTimeout(
            total=sum(self._timeouts),
            connect=self._timeouts[0],
            sock_connect=self._timeouts[0],
            sock_read=self._timeouts[1],
        )
        try:
            async with session.put(url, data=body, headers=_HEADERS, timeout=timeout) as resp:
                raw = await resp.read()
                status = resp.status
        except Exception as exc:
            mapped = _map_transport(exc)
            if mapped is None:
                raise
            raise mapped from None
        payload = _decode_body(raw)
        if not 200 <= status < 300:
            raise LocalProtocolError("http_error")
        return payload

    async def _ensure_session(self, address: str) -> ClientSession:
        if self._session is not None and self._session_address != address:
            await self._close_session()
        if self._session is None or self._session.closed:
            self._session = self._open_session()
            self._session_address = address
        return self._session

    def _open_session(self) -> ClientSession:
        if self._session_factory is not None:
            return self._session_factory()
        return ClientSession(
            connector=TCPConnector(limit=1),
            connector_owner=True,
            timeout=ClientTimeout(
                total=sum(self._timeouts),
                connect=self._timeouts[0],
                sock_connect=self._timeouts[0],
                sock_read=self._timeouts[1],
            ),
            trust_env=False,
        )

    async def _close_if_idle(self) -> None:
        if self._cycle_depth == 0:
            await self._close_session()

    async def _close_session(self) -> None:
        session = self._session
        self._session = None
        self._session_address = None
        if session is None or session.closed:
            return
        try:
            await session.close()
        except Exception as exc:
            self._log("-", exc, final=False)

    def _log(self, path: str, exc: BaseException, *, final: bool) -> None:
        log = _LOGGER.warning if final else _LOGGER.debug
        log("local request failed path=%s error=%s", path, type(exc).__name__)


def _command_body(path: Sequence[str], values: Mapping[str, Any]) -> bytes:
    leaf: Any = dict(values)
    for key in reversed(path):
        leaf = {key: leaf}
    return _dumps({"c": leaf})


def _decode_body(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MalformedResponse("malformed_json") from exc
    if not isinstance(parsed, dict):
        raise _MalformedResponse("malformed_json")
    _raise_for_payload(parsed)
    return parsed
