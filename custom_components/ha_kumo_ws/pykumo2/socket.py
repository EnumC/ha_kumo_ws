"""Socket.IO helper for streaming Mitsubishi Comfort updates."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Iterable

import socketio

from .client import MitsubishiComfortClient
from .const import DEFAULT_FORCE_REQUESTS, SOCKET_URL

_LOGGER = logging.getLogger(__name__)

SocketCallback = Callable[[str, dict], Awaitable[None] | None]


class SocketUpdateManager:
    """Maintain a Socket.IO connection and fan out device updates."""

    def __init__(
        self,
        client: MitsubishiComfortClient,
        device_serials: Iterable[str],
        callback: SocketCallback,
        refresh_on_connect: bool = True,
        request_types: tuple[str, ...] = DEFAULT_FORCE_REQUESTS,
    ) -> None:
        self._client = client
        self._serials = list(dict.fromkeys(device_serials))
        self._callback = callback
        self._refresh_on_connect = refresh_on_connect
        self._request_types = request_types

        self._sio: socketio.AsyncClient | None = None
        self._wait_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._sio is not None and self._sio.connected

    async def start(self) -> None:
        """Connect and start listening for updates."""
        if self.running:
            return

        await self._client._ensure_authenticated()
        token = self._client._tokens.access if self._client._tokens else None
        if not token:
            raise RuntimeError("Socket start requested without an access token.")

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=5,
            reconnection_delay=2,
            reconnection_delay_max=10,
            logger=False,
            engineio_logger=False,
        )

        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("connect_error", self._on_connect_error)
        self._sio.on("device_update", self._on_device_update)
        self._sio.on("device_status_v2", self._on_device_status)

        await self._sio.connect(
            SOCKET_URL,
            auth={"token": token},
            headers={"Authorization": f"Bearer {token}"},
            transports=["websocket", "polling"],
        )

        # Keep the connection alive in the background
        self._wait_task = asyncio.create_task(self._sio.wait())

    async def stop(self) -> None:
        """Disconnect and stop waiting for events."""
        self._stopping.set()
        if self._sio:
            try:
                await self._sio.disconnect()
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.debug("Socket disconnect failed: %s", exc)
        if self._wait_task:
            await asyncio.wait([self._wait_task], timeout=1)
        self._sio = None
        self._wait_task = None

    async def _on_connect(self) -> None:
        """Subscribe to devices and optionally force fresh data."""
        if not self._sio:
            return

        _LOGGER.debug("Socket connected, subscribing to %s", self._serials)
        for serial in self._serials:
            await self._sio.emit("subscribe", serial)
            await self._sio.emit("device_status_v2", serial)
            if self._refresh_on_connect:
                for request_type in self._request_types:
                    await self._sio.emit("force_adapter_request", (serial, request_type))

        await self._dispatch("connected", {"devices": self._serials})

    async def _on_disconnect(self) -> None:
        await self._dispatch("disconnected", {})

    async def _on_connect_error(self, data) -> None:
        _LOGGER.debug("Socket connect_error: %s", data)
        await self._dispatch("connect_error", {"error": str(data)})

    async def _on_device_update(self, data: dict) -> None:
        _LOGGER.debug("Socket device_update:\n%s", json.dumps(data, indent=2, sort_keys=True))
        await self._dispatch("device_update", data)

    async def _on_device_status(self, data: dict) -> None:
        _LOGGER.debug("Socket device_status_v2:\n%s", json.dumps(data, indent=2, sort_keys=True))
        await self._dispatch("device_status_v2", data)

    async def _dispatch(self, event: str, payload: dict) -> None:
        """Call the provided callback safely."""
        if not self._callback:
            return

        try:
            result = self._callback(event, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("Socket callback failure for %s: %s", event, exc)
