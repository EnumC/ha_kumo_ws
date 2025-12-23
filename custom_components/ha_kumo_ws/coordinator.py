"""Coordinator managing Mitsubishi Comfort data and socket updates."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Dict, Set, Tuple
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .pykumo2 import (
    DeviceState,
    MitsubishiComfortClient,
    MitsubishiComfortError,
    SocketUpdateManager,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class MitsubishiComfortCoordinator(DataUpdateCoordinator[Dict[str, DeviceState]]):
    """Fetch data via REST and keep it fresh via socket events."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MitsubishiComfortClient,
        site_ids: list[str],
        refresh_on_connect: bool,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Mitsubishi Comfort",
            update_interval=timedelta(minutes=10),
        )
        self.client = client
        self.site_ids = site_ids or []
        self.refresh_on_connect = refresh_on_connect
        self._socket: SocketUpdateManager | None = None
        self._lock = asyncio.Lock()
        # Holds: serial -> (expires_at_monotonic, protected_keys)
        self._holds: Dict[str, Tuple[float, Set[str]]] = {}

    async def _async_update_data(self) -> Dict[str, DeviceState]:
        """Fetch latest device data via REST."""
        try:
            devices: Dict[str, DeviceState] = {}
            target_sites = self.site_ids or [None]
            for site_id in target_sites:
                devices.update(await self.client.async_get_devices(site_id))
            # DataUpdateCoordinator expects a new object to trigger updates
            return dict(devices)
        except MitsubishiComfortError as exc:
            raise UpdateFailed(str(exc)) from exc

    async def async_start_socket(self) -> None:
        """Start the socket listener for live updates."""
        async with self._lock:
            if self._socket and self._socket.running:
                return
            if not self.data:
                return
            serials = list(self.data.keys())
            self._socket = SocketUpdateManager(
                client=self.client,
                device_serials=serials,
                callback=self._handle_socket_event,
                refresh_on_connect=self.refresh_on_connect,
            )
            await self._socket.start()

    async def async_stop(self) -> None:
        """Stop socket listener."""
        async with self._lock:
            if self._socket:
                await self._socket.stop()
            self._socket = None

    async def _handle_socket_event(self, event: str, payload: dict) -> None:
        """Handle incoming socket event and update coordinator data."""
        if event not in ("device_update", "device_status_v2"):
            return

        serial = payload.get("deviceSerial")
        if not serial or serial not in self.data:
            return

        # Respect command holds: ignore protected keys while a recent command is pending
        hold = self._holds.get(serial)
        if hold:
            expires_at, protected = hold
            if time.monotonic() >= expires_at:
                self._holds.pop(serial, None)
            else:
                payload = dict(payload)
                for key in list(payload.keys()):
                    if key in protected:
                        payload.pop(key)
                        self.logger.debug(
                            "Ignoring stale %s for %s due to command hold; payload=%s",
                            key,
                            serial,
                            payload,
                        )

        device = self.data[serial]
        before_state = {
            "mode": device.operation_mode,
            "power": device.power,
            "fan": device.fan_speed,
            "vane": device.air_direction,
            "roomTemp": device.room_temp,
            "spCool": device.sp_cool,
            "spHeat": device.sp_heat,
        }
        device.apply_update(payload)

        after_state = {
            "mode": device.operation_mode,
            "power": device.power,
            "fan": device.fan_speed,
            "vane": device.air_direction,
            "roomTemp": device.room_temp,
            "spCool": device.sp_cool,
            "spHeat": device.sp_heat,
        }
        changes: Dict[str, tuple] = {
            key: (before_state.get(key), after_state.get(key))
            for key in after_state
            if before_state.get(key) != after_state.get(key)
        }
        if changes:
            self.logger.debug(
                "Device %s state updated via socket: %s",
                serial,
                json.dumps(changes, indent=2, sort_keys=True),
            )

        # Push updated state to entities
        self.async_set_updated_data(dict(self.data))

    def register_command_hold(self, serial: str, protected_keys: Set[str], duration: float = 10.0) -> None:
        """Protect specific keys from being overwritten by stale updates for a short window."""
        self._holds[serial] = (time.monotonic() + duration, set(protected_keys))


async def async_unload_coordinator(hass: HomeAssistant, entry_id: str) -> None:
    """Helper to stop socket when unloading an entry."""
    stored = hass.data.get(DOMAIN, {}).get(entry_id)
    if stored:
        coordinator: MitsubishiComfortCoordinator | None = stored.get("coordinator")
        if coordinator:
            await coordinator.async_stop()
