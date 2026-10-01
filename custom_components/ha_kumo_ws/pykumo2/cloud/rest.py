"""Kumo Cloud v3 REST client."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from ..clock import Clock
from ..errors import AuthenticationError, CloudError, RateLimitedError
from ..util.redact import redact
from .budget import CallCategory, CloudCallLedger
from .const import APP_VERSION, BASE_URL, DEFAULT_HEADERS
from .tokens import TokenInfo, TokenSource

_LOGGER = logging.getLogger(__name__)

_AUTH_REJECTED = frozenset({400, 401, 403})


def default_client_factory() -> httpx.AsyncClient:
    """HTTP/2 client like the app. Loads CA certs, so call it off the event loop."""
    return httpx.AsyncClient(timeout=30.0)


class CloudRestClient:
    """REST calls; authed ones get tokens from a bound TokenSource."""

    def __init__(
        self,
        client: httpx.AsyncClient | Callable[[], httpx.AsyncClient],
        ledger: CloudCallLedger,
        clock: Clock,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self._factory: Callable[[], httpx.AsyncClient] | None = None
        self._client: httpx.AsyncClient | None = None
        self._owned = False
        if isinstance(client, httpx.AsyncClient):
            self._client = client
        else:
            self._factory = client
            self._owned = True
        self._ledger = ledger
        self._clock = clock
        self._base_url = base_url
        self._tokens: TokenSource | None = None

    def bind_tokens(self, tokens: TokenSource) -> None:
        """Set the token source for authed calls (it usually wraps this client)."""
        self._tokens = tokens

    async def async_close(self) -> None:
        if not self._owned:
            return
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def login(self, username: str, password: str) -> TokenInfo:
        body = {"username": username, "password": password, "appVersion": APP_VERSION}
        response = await self._send("POST", "/v3/login", "login", body=body, log_body=False)
        if response.status_code in _AUTH_REJECTED:
            raise AuthenticationError(f"login rejected: HTTP {response.status_code}")
        return TokenInfo.from_response(self._parse(response, "/v3/login", log=False), self._now)

    async def refresh(self, refresh_token: str) -> TokenInfo:
        response = await self._send(
            "POST",
            "/v3/refresh",
            "refresh",
            body={"refresh": refresh_token},
            headers={"Authorization": f"Bearer {refresh_token}"},
            log_body=False,
        )
        if response.status_code in _AUTH_REJECTED:
            raise AuthenticationError(f"refresh rejected: HTTP {response.status_code}")
        return TokenInfo.from_response(self._parse(response, "/v3/refresh", log=False), self._now)

    async def get_sites(self) -> list[dict[str, Any]]:
        return _as_list(await self._authed("GET", "/v3/sites/", "get"))

    async def get_zones(self, site_id: str) -> list[dict[str, Any]]:
        return _as_list(await self._authed("GET", f"/v3/sites/{site_id}/zones", "get"))

    async def get_device(self, serial: str) -> dict[str, Any]:
        return _as_dict(await self._authed("GET", f"/v3/devices/{serial}", "get"))

    async def get_device_status(self, serial: str) -> dict[str, Any]:
        """Adapter status; contains cryptoSerial, never log it unredacted."""
        return _as_dict(await self._authed("GET", f"/v3/devices/{serial}/status", "get"))

    async def send_command(self, body: Mapping[str, Any]) -> Any:
        return await self._authed(
            "POST", "/v3/devices/send-command", "command", body=body, essential=True
        )

    async def relay_command(self, serial: str, body: Mapping[str, Any]) -> Any:
        return await self._authed(
            "POST",
            f"/v3/devices/{serial}/relay-command",
            "relay",
            body=body,
            headers={"x-allow-cache": "true"},
            essential=True,
        )

    @property
    def _now(self) -> float:
        return self._clock.now()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            assert self._factory is not None
            self._client = self._factory()
        return self._client

    async def _authed(
        self,
        method: str,
        path: str,
        category: CallCategory,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        essential: bool = False,
    ) -> Any:
        tokens = self._tokens
        if tokens is None:
            raise AuthenticationError("no token source bound")
        if not self._ledger.allow(essential):
            raise RateLimitedError(
                "cloud rate-limit headroom reserved for commands",
                retry_after=self._ledger.seconds_until_reset(),
            )
        response: httpx.Response | None = None
        for attempt in range(2):
            token = await tokens.async_access_token()
            auth = {**(headers or {}), "Authorization": f"Bearer {token}"}
            response = await self._send(method, path, category, body=body, headers=auth)
            if response.status_code != 401 or attempt:
                break
            _LOGGER.debug("HTTP %s %s got 401, renewing token", method, path)
            await tokens.async_invalidate(token)
        assert response is not None
        return self._parse(response, path)

    async def _send(
        self,
        method: str,
        path: str,
        category: CallCategory,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        log_body: bool = True,
    ) -> httpx.Response:
        self._ledger.record(category)
        if log_body and body is not None:
            _LOGGER.debug("HTTP %s %s %s", method, path, redact(body))
        else:
            _LOGGER.debug("HTTP %s %s", method, path)
        try:
            response = await self._http().request(
                method,
                self._base_url + path,
                json=body,
                headers={**DEFAULT_HEADERS, **(headers or {})},
            )
        except httpx.HTTPError as err:
            raise CloudError(f"{method} {path} failed: {type(err).__name__}") from err
        self._ledger.update_from_headers(response.headers)
        return response

    def _parse(self, response: httpx.Response, path: str, *, log: bool = True) -> Any:
        status = response.status_code
        data = _json(response)
        if log or status >= 400:
            _LOGGER.debug("HTTP %s -> %s", path, status)
        if status == 429:
            retry_after = _retry_after(response) or self._ledger.seconds_until_reset()
            raise RateLimitedError(f"{path}: HTTP 429", retry_after=retry_after)
        if status == 401:
            raise AuthenticationError(f"{path}: HTTP 401")
        if status >= 400:
            raise CloudError(f"{path}: HTTP {status}")
        return data


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


def _as_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise CloudError("expected a JSON list")
    return [item for item in data if isinstance(item, dict)]


def _as_dict(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CloudError("expected a JSON object")
    return data
