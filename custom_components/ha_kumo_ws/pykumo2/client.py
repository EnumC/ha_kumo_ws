"""Async Mitsubishi Comfort (Kumo Cloud) client."""

from __future__ import annotations

import asyncio
import logging
import json
from typing import Any, Iterable, List, Sequence

import httpx

from .const import BASE_URL, DEFAULT_FORCE_REQUESTS, DEFAULT_HEADERS, SOCKET_URL
from .errors import AuthenticationError, MitsubishiComfortError
from .models import DeviceState, TokenInfo

class MitsubishiComfortClient:
    """Async HTTP client that mirrors the behavior of the mobile app."""

    def __init__(self, username: str, password: str, site_ids: Sequence[str] | None):
        self.username = username
        self.password = password
        self.site_ids: List[str] = list(site_ids or [])

        self._client: httpx.AsyncClient | None = None
        self._tokens: TokenInfo | None = None
        self._auth_lock = asyncio.Lock()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _ensure_http_client(self) -> httpx.AsyncClient:
        """Lazily create the HTTP client in a thread to avoid blocking the loop."""
        if self._client is None:
            self._client = await asyncio.to_thread(
                httpx.AsyncClient,
                base_url=BASE_URL,
                headers=DEFAULT_HEADERS,
                http2=True,
                timeout=30.0,
            )
        return self._client

    async def async_login(self) -> dict[str, Any]:
        """Login to Kumo Cloud and cache tokens."""
        client = await self._ensure_http_client()
        response = await client.post(
            "/v3/login",
            json={
                "username": self.username,
                "password": self.password,
                "appVersion": DEFAULT_HEADERS["x-app-version"],
            },
        )

        if response.status_code >= 400:
            raise AuthenticationError(f"Login failed: {response.text}")

        data = response.json()
        token_data = data.get("token", {})
        self._tokens = TokenInfo.from_response(token_data)
        return data

    async def _refresh_token(self) -> None:
        """Refresh the access token using the refresh token."""
        if not self._tokens or not self._tokens.refresh:
            raise AuthenticationError("No refresh token available.")

        client = await self._ensure_http_client()
        response = await client.post(
            "/v3/refresh",
            json={"refresh": self._tokens.refresh},
            headers={"Authorization": f"Bearer {self._tokens.refresh}"},
        )

        if response.status_code >= 400:
            raise AuthenticationError(f"Token refresh failed: {response.text}")

        data = response.json()
        self._tokens = TokenInfo.from_response(data)

    async def _ensure_authenticated(self) -> None:
        """Ensure valid access token before making a request."""
        async with self._auth_lock:
            if not self._tokens:
                await self.async_login()
                return

            if not self._tokens.is_access_expired():
                return

            if self._tokens.is_refresh_expired():
                await self.async_login()
            else:
                await self._refresh_token()

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        require_auth: bool = True,
    ) -> Any:
        """Issue an HTTP request with automatic token refresh."""
        headers: dict[str, str] = {}
        if require_auth:
            await self._ensure_authenticated()
            if not self._tokens:
                raise AuthenticationError("Missing token after authentication.")
            headers["Authorization"] = f"Bearer {self._tokens.access}"

        client = await self._ensure_http_client()
        # Debug log outbound request
        _logger = logging.getLogger(__name__)
        _logger.debug(
            "HTTP %s %s %s",
            method,
            endpoint,
            json.dumps(json_data, sort_keys=True, indent=2) if json_data else "",
        )
        response = await client.request(
            method,
            endpoint,
            json=json_data,
            headers=headers,
        )

        # Retry once on 401 using a refreshed access token
        if response.status_code == 401 and require_auth:
            await self._refresh_token()
            if not self._tokens:
                raise AuthenticationError("Refresh failed, no token present.")
            headers["Authorization"] = f"Bearer {self._tokens.access}"
            client = await self._ensure_http_client()
            response = await client.request(
                method,
                endpoint,
                json=json_data,
                headers=headers,
            )

        if response.status_code >= 400:
            raise MitsubishiComfortError(
                f"API error {response.status_code}: {response.text}"
            )

        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            body = response.text
        _logger.debug(
            "HTTP %s %s response %s:\n%s",
            method,
            endpoint,
            response.status_code,
            json.dumps(body, indent=2, sort_keys=True) if isinstance(body, dict) else body,
        )
        return body

    async def async_get_sites(self) -> list[dict[str, Any]]:
        """Return all sites associated with the account."""
        result = await self._request("GET", "/v3/sites/")
        return list(result or [])

    async def async_get_zones(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """Return all zones for a site."""
        target_site = site_id or (self.site_ids[0] if self.site_ids else None)
        if not target_site:
            raise MitsubishiComfortError("Site ID is required to fetch zones.")
        result = await self._request("GET", f"/v3/sites/{target_site}/zones")
        return list(result or [])

    async def async_get_devices(self, site_id: str | None = None) -> dict[str, DeviceState]:
        """Return device states keyed by serial, built from the zones endpoint."""
        devices: dict[str, DeviceState] = {}
        for zone in await self.async_get_zones(site_id):
            adapter = zone.get("adapter", {})
            serial = adapter.get("deviceSerial")
            if not serial:
                continue
            device = devices.get(serial) or DeviceState(
                serial=serial,
                name=zone.get("name", serial),
                zone_id=zone.get("id"),
                model_number=adapter.get("modelNumber"),
                serial_number=adapter.get("serialNumber"),
            )
            device.update_from_zone(zone)
            devices[serial] = device

        # Enrich missing model numbers via device endpoint (one per missing device)
        for serial, device in devices.items():
            if device.model_number:
                continue
            try:
                details = await self._request("GET", f"/v3/devices/{serial}")
                model_number = (
                    details.get("modelNumber")
                    or details.get("model")
                    or details.get("modelName")
                )
                serial_number = details.get("serialNumber")
                if model_number:
                    device.model_number = model_number
                if serial_number:
                    device.serial_number = serial_number
                device.raw.update(details)
            except Exception:
                # Best-effort; skip if request fails
                continue
        return devices

    async def async_get_weather(self, site_id: str | None = None) -> dict[str, Any]:
        """Fetch the weather payload for a site."""
        target_site = site_id or (self.site_ids[0] if self.site_ids else None)
        if not target_site:
            raise MitsubishiComfortError("Site ID is required to fetch weather.")
        return await self._request("GET", f"/v3/sites/{target_site}/weather")

    async def async_send_command(self, device_serial: str, commands: dict[str, Any]) -> dict[str, Any]:
        """Send a command payload to a device."""
        payload = {"deviceSerial": device_serial, "commands": commands}
        return await self._request("POST", "/v3/devices/send-command", json_data=payload)

    async def async_force_refresh_payload(
        self,
        device_serials: Iterable[str],
        request_types: tuple[str, ...] = DEFAULT_FORCE_REQUESTS,
    ) -> dict[str, Any]:
        """Construct a force refresh command payload (used by the SocketIO flow)."""
        return {
            "device_serials": list(device_serials),
            "requests": request_types,
            "socket_url": SOCKET_URL,
        }
