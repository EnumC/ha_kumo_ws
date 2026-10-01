"""Kumo Cloud transport: REST commands, socket push, REST zones as a backstop."""

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..clock import Clock
from ..domain.commands import Command
from ..domain.state import DeviceState, StatePatch
from ..errors import CloudError, CommandNotSupportedError, KumoError
from ..transport import LeaseReason, Node, PushTransport, TransportKind
from .codec import CloudCodec
from .rest import CloudRestClient
from .socket import CloudSocketSession

_LOGGER = logging.getLogger(__name__)

ZONES_CACHE_S = 10.0

_STATUS_REASONS = frozenset({LeaseReason.FALLBACK, LeaseReason.CLOUD_ONLY})


class CloudTransport:
    """PushTransport over Kumo Cloud. Patches with key "capabilities" carry a profile."""

    kind = TransportKind.CLOUD

    def __init__(
        self,
        rest: CloudRestClient,
        socket: CloudSocketSession,
        codec: CloudCodec,
        clock: Clock,
    ) -> None:
        self._rest = rest
        self._socket = socket
        self._codec = codec
        self._clock = clock
        self._sites: dict[str, str] = {}
        self._overlays: dict[str, dict[str, Any]] = {}
        self._profiles: dict[str, Mapping[str, Any]] = {}
        self._zones: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._listener: Callable[[str, StatePatch], None] | None = None
        socket.set_listener(self._on_socket_event)

    def register(self, serial: str, site_id: str) -> None:
        """Record the site a serial belongs to (needed by async_fetch)."""
        self._sites[serial] = site_id

    def supports(self, command: Command) -> bool:
        return self._codec.supports(command)

    def set_listener(self, cb: Callable[[str, StatePatch], None]) -> None:
        self._listener = cb

    async def async_execute(self, serial: str, command: Command, state: DeviceState) -> None:
        if not self._codec.supports(command):
            raise CommandNotSupportedError(f"{type(command).__name__} is local only")
        sent_keys: set[str] = set()
        try:
            for request in self._codec.encode(command, state):
                if request.kind == "send":
                    await self._rest.send_command(request.body)
                    sent_keys.update(self._codec.decode_device(request.body["commands"]))
                else:
                    await self._rest.relay_command(serial, request.body)
                    sent_keys.add("room_temp_offset")
        except (KumoError, ValueError) as err:
            err.__dict__["sent_keys"] = sent_keys | getattr(err, "sent_keys", set())
            raise

    async def async_fetch(self, serial: str, nodes: frozenset[Node]) -> StatePatch:
        """Status from the site's zones; other nodes arrive by socket push only."""
        if "status" not in nodes:
            return self._patch({})
        site_id = self._sites.get(serial)
        if site_id is None:
            raise CloudError(f"no site registered for {serial}")
        for zone in await self._site_zones(site_id):
            decoded = self._codec.decode_zone(zone)
            if decoded is not None and decoded[0] == serial:
                return self._patch(decoded[1])
        raise CloudError(f"{serial} not found in site zones")

    async def async_acquire(
        self, serial: str, reason: LeaseReason, *, want_profile: bool = False
    ) -> None:
        """Lease the socket; FALLBACK/CLOUD_ONLY also force a fresh iuStatus."""
        force: list[str] = []
        if reason in _STATUS_REASONS:
            force.append("iuStatus")
        if want_profile or reason is LeaseReason.PROFILE_BOOTSTRAP:
            force.append("profile")
        if reason is LeaseReason.CREDENTIAL_REFRESH:
            force.append("adapterStatus")
        await self._socket.acquire(serial, reason, force=force)

    async def async_request_profile(self, serial: str) -> None:
        """Request a profile independently of the routing lease."""
        try:
            await self._socket.acquire(serial, LeaseReason.PROFILE_BOOTSTRAP, force=("profile",))
        finally:
            await self._socket.release(serial, LeaseReason.PROFILE_BOOTSTRAP)

    async def async_release(self, serial: str, reason: LeaseReason) -> None:
        await self._socket.release(serial, reason)

    async def async_close(self) -> None:
        """Close the socket session and the REST client."""
        await self._socket.async_close()
        await self._rest.async_close()

    async def _site_zones(self, site_id: str) -> list[dict[str, Any]]:
        now = self._clock.monotonic()
        cached = self._zones.get(site_id)
        if cached is not None and now - cached[0] < ZONES_CACHE_S:
            return cached[1]
        zones = await self._rest.get_zones(site_id)
        self._zones[site_id] = (now, zones)
        return zones

    def _patch(self, values: Mapping[str, Any]) -> StatePatch:
        return StatePatch(source=TransportKind.CLOUD, values=values, at=self._clock.now())

    def _on_socket_event(self, event: str, payload: Mapping[str, Any]) -> None:
        serial = payload.get("deviceSerial")
        if not isinstance(serial, str):
            return
        values: dict[str, Any] | None = None
        if event == "profile_update":
            self._profiles[serial] = dict(payload)
            caps = self._codec.decode_profile(payload, self._overlays.get(serial))
            values = None if caps is None else {"capabilities": caps}
        elif event == "adapter_update":
            values, overlay = self._codec.decode_adapter(payload)
            overlay = {**self._overlays.get(serial, {}), **overlay}
            if self._overlays.get(serial) != overlay:
                self._overlays[serial] = overlay
                if serial in self._profiles:
                    caps = self._codec.decode_profile(self._profiles[serial], overlay)
                    if caps is not None:
                        values["capabilities"] = caps
        else:
            decoded = self._codec.decode_event(event, payload)
            values = None if decoded is None else decoded[1]
        if values and self._listener is not None:
            self._listener(serial, self._patch(values))


if TYPE_CHECKING:

    def _conforms(transport: CloudTransport) -> PushTransport:
        return transport
