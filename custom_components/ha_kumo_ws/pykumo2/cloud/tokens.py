"""Kumo Cloud JWT tokens and a single-lock token manager."""

import asyncio
import base64
import binascii
import dataclasses
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

from ..clock import Clock
from ..errors import AuthenticationError

_LOGGER = logging.getLogger(__name__)

ACCESS_FALLBACK_S = 18 * 60.0
REFRESH_FALLBACK_S = 25 * 86400.0
EXPIRY_SKEW_S = 60.0
LOGIN_FAILURE_COOLDOWN_S = 15 * 60.0


def jwt_claims(token: str) -> dict[str, Any]:
    """Unverified JWT payload claims; {} when malformed."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def _expiry(claims: Mapping[str, Any], now: float, fallback: float) -> float:
    exp = claims.get("exp")
    if isinstance(exp, int | float) and not isinstance(exp, bool):
        return float(exp)
    return now + fallback


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """Access/refresh pair with wall-clock expiries (epoch seconds)."""

    access: str = field(repr=False)
    refresh: str = field(repr=False)
    access_expires_at: float
    refresh_expires_at: float
    user_id: str | None = None

    @classmethod
    def from_tokens(cls, access: str, refresh: str, now: float) -> Self:
        """Build from raw JWTs; expiries from exp, else 18 min / 25 days."""
        claims = jwt_claims(access)
        user_id = claims.get("id")
        return cls(
            access=access,
            refresh=refresh,
            access_expires_at=_expiry(claims, now, ACCESS_FALLBACK_S),
            refresh_expires_at=_expiry(jwt_claims(refresh), now, REFRESH_FALLBACK_S)
            if refresh
            else now,
            user_id=None if user_id is None else str(user_id),
        )

    @classmethod
    def from_response(cls, data: object, now: float) -> Self:
        """Parse a login ({"token": {...}}) or refresh (top-level) response."""
        source = data.get("token") if isinstance(data, Mapping) else None
        if not isinstance(source, Mapping):
            source = data if isinstance(data, Mapping) else {}
        access, refresh = source.get("access"), source.get("refresh")
        if not isinstance(access, str) or not access:
            raise AuthenticationError("token response has no access token")
        return cls.from_tokens(access, refresh if isinstance(refresh, str) else "", now)

    def access_valid(self, now: float) -> bool:
        return now < self.access_expires_at - EXPIRY_SKEW_S

    def refresh_valid(self, now: float) -> bool:
        return bool(self.refresh) and now < self.refresh_expires_at - EXPIRY_SKEW_S


class TokenAuth(Protocol):
    """Unauthenticated login/refresh calls."""

    async def login(self, username: str, password: str) -> TokenInfo: ...

    async def refresh(self, refresh_token: str) -> TokenInfo: ...


class TokenSource(Protocol):
    """Supplies access tokens to REST and socket clients."""

    @property
    def user_id(self) -> str | None: ...

    async def async_access_token(self) -> str: ...

    async def async_invalidate(self, access_token: str) -> None: ...


class TokenManager:
    """Lazily logs in and refreshes; every token change happens under one lock."""

    def __init__(
        self,
        auth: TokenAuth,
        username: str,
        password: str,
        clock: Clock,
        *,
        login_cooldown: float = LOGIN_FAILURE_COOLDOWN_S,
    ) -> None:
        self._auth = auth
        self._username = username
        self._password = password
        self._clock = clock
        self._cooldown = login_cooldown
        self._info: TokenInfo | None = None
        self._lock = asyncio.Lock()
        self._login_error: AuthenticationError | None = None
        self._login_failed_at = 0.0

    def __repr__(self) -> str:
        return f"TokenManager(user_id={self.user_id!r})"

    @property
    def user_id(self) -> str | None:
        return None if self._info is None else self._info.user_id

    async def async_access_token(self) -> str:
        """A valid access token, logging in or refreshing as needed."""
        async with self._lock:
            info = self._info
            now = self._clock.now()
            if info is None or not info.refresh_valid(now):
                return (await self._login()).access
            if not info.access_valid(now):
                return (await self._refresh(info)).access
            return info.access

    @property
    def auth_failed(self) -> bool:
        """True while a rejected login is cached."""
        return self._login_error is not None

    def reset_auth_failure(self) -> None:
        """Allow the next call to log in again."""
        self._login_error = None

    def update_credentials(self, username: str, password: str) -> None:
        """Use new account credentials; drops tokens and any cached login failure."""
        self._username = username
        self._password = password
        self._info = None
        self._login_error = None

    async def async_invalidate(self, access_token: str) -> None:
        """Server rejected access_token; renew it unless that already happened."""
        async with self._lock:
            info = self._info
            if info is None or info.access != access_token:
                return
            if info.refresh_valid(self._clock.now()):
                await self._refresh(info)
            else:
                await self._login()

    async def _login(self) -> TokenInfo:
        cached = self._login_error
        if cached is not None:
            if self._clock.monotonic() - self._login_failed_at < self._cooldown:
                _LOGGER.debug("Cloud login skipped, last attempt was rejected")
                raise AuthenticationError(str(cached))
            self._login_error = None
        try:
            self._info = await self._auth.login(self._username, self._password)
        except AuthenticationError as err:
            if cached is None:
                _LOGGER.warning("Cloud login rejected: %s", err)
            self._login_error = err
            self._login_failed_at = self._clock.monotonic()
            raise
        if cached is not None:
            _LOGGER.info("Cloud login succeeded again")
        return self._info

    async def _refresh(self, info: TokenInfo) -> TokenInfo:
        try:
            new = await self._auth.refresh(info.refresh)
        except AuthenticationError:
            _LOGGER.debug("Token refresh rejected, logging in again")
            return await self._login()
        if not new.refresh:
            new = dataclasses.replace(
                new, refresh=info.refresh, refresh_expires_at=info.refresh_expires_at
            )
        if new.user_id is None:
            new = dataclasses.replace(new, user_id=info.user_id)
        self._info = new
        return new
