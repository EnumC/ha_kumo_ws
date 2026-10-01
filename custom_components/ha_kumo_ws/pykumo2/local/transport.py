"""Local Transport: one signed client per indoor unit."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import Clock, SystemClock
from ..credentials.models import UnitCredentials
from ..domain.capabilities import Capabilities
from ..domain.commands import Command
from ..domain.state import DeviceState, StatePatch
from ..errors import CredentialsMissingError, LocalError
from ..transport import Node, TransportKind
from ..util.coerce import dig
from .client import LocalUnitClient
from .cn105.bus import AsyncCn105Bus
from .codec import POSSIBLE_SENSORS, LocalCodec

_LOGGER = logging.getLogger(__name__)
_SYSTEM_CLOCK = SystemClock()

_READS = {
    "status": (
        ("indoorUnit", "status"),
        (
            "mode",
            "standby",
            "spHeat",
            "spCool",
            "roomTemp",
            "fanSpeed",
            "vaneDir",
            "filterDirty",
            "defrost",
            "tempSource",
            "activeThermistor",
        ),
    ),
    "profile": (
        ("indoorUnit", "profile"),
        (
            "numberOfFanSpeeds",
            "hasFanSpeedAuto",
            "hasVaneSwing",
            "hasModeDry",
            "hasModeHeat",
            "hasModeVent",
            "hasModeAuto",
            "hasVaneDir",
            "maximumSetPoints",
            "minimumSetPoints",
        ),
    ),
    "adapter": (
        ("adapter", "status"),
        ("autoModePrevention", "userHasModeDry", "userHasModeHeat", "localNetwork", "runState"),
    ),
}
_SENSOR_ATTRIBUTES = ("uuid", "humidity", "temperature", "battery", "rssi", "txPower")

_FETCH_ORDER: tuple[Node, ...] = ("status", "sensors", "adapter", "profile", "mhk2")


class LocalWriteError(LocalError):
    """Local write failed after sending a prefix of the encoded bodies."""

    def __init__(self, sent_count: int, sent_keys: set[str]) -> None:
        super().__init__("local write failed")
        self.sent_count = sent_count
        self.sent_keys = sent_keys


class LocalTransport:
    """Reads and commands units over the local adapter API."""

    kind = TransportKind.LOCAL

    def __init__(
        self,
        codec: LocalCodec,
        *,
        client_factory: Callable[[str, UnitCredentials], LocalUnitClient],
        clock: Clock = _SYSTEM_CLOCK,
    ) -> None:
        self._codec = codec
        self._client_factory = client_factory
        self._clock = clock
        self._clients: dict[str, LocalUnitClient] = {}
        self._overlays: dict[str, Mapping[str, Any]] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._capabilities: dict[str, Capabilities] = {}
        self._buses: dict[str, AsyncCn105Bus] = {}

    def supports(self, command: Command) -> bool:
        """True when the codec can encode ``command``."""
        return self._codec.supports(command)

    def add_unit(self, serial: str, creds: UnitCredentials) -> None:
        """Open a client for ``serial`` at ``creds.address``."""
        self._clients[serial] = self._client_factory(creds.address, creds)
        self._buses.pop(serial, None)

    def remove_unit(self, serial: str) -> None:
        """Drop ``serial`` and any cached profile. Does not close a live cycle."""
        self._clients.pop(serial, None)
        self._buses.pop(serial, None)
        self._overlays.pop(serial, None)
        self._capabilities.pop(serial, None)
        self._profiles.pop(serial, None)

    def update_credentials(self, serial: str, creds: UnitCredentials) -> None:
        """Replace the signing credentials for ``serial``."""
        self._client(serial).set_credentials(creds)

    def update_address(self, serial: str, address: str) -> None:
        """Point ``serial`` at a new host."""
        self._client(serial).set_address(address)

    def has_unit(self, serial: str) -> bool:
        """True when ``serial`` was added and not removed."""
        return serial in self._clients

    def cn105_bus(self, serial: str) -> AsyncCn105Bus:
        """CN105 bus on the unit's client, so it shares the client's lock and connection."""
        client = self._client(serial)
        bus = self._buses.get(serial)
        if bus is None:
            bus = self._buses[serial] = AsyncCn105Bus(client, serial, clock=self._clock)
        return bus

    async def async_fetch(self, serial: str, nodes: frozenset[Node]) -> StatePatch:
        """Read ``nodes`` on one connection. A failed status read is raised."""
        client = self._client(serial)
        values: dict[str, Any] = {}
        async with client.cycle():
            for node in _FETCH_ORDER:
                if node not in nodes:
                    continue
                if node == "status":
                    values.update(await self._fetch_status(client))
                elif node == "sensors":
                    sensors = await self._fetch_sensors(client, serial)
                    if sensors is not None:
                        values.update(sensors)
                elif node == "adapter":
                    adapter = await self._fetch_adapter(client, serial)
                    if adapter is not None:
                        values.update(adapter)
                elif node == "profile":
                    profile = await self._fetch_profile(client, serial)
                    if profile is not None:
                        values.update(profile)
                else:
                    extra = await self._fetch_optional(client, serial, node)
                    if extra is not None:
                        values.update(extra)
        return StatePatch(source=TransportKind.LOCAL, values=values, at=self._clock.now())

    async def async_execute(self, serial: str, command: Command, state: DeviceState) -> None:
        """Send bodies in order; local errors carry sent_count and sent_keys."""
        client = self._client(serial)
        caps = (
            state.command_capabilities or self._capabilities.get(serial) or Capabilities.default()
        )
        bodies = self._codec.encode(command, state, caps)
        sent_count = 0
        sent_keys: set[str] = set()
        try:
            async with client.cycle():
                for body in bodies:
                    await client.send(body)
                    sent_count += 1
                    raw = json.loads(body)["c"]
                    for node in raw.values():
                        for key in node.get("status", {}):
                            sent_keys.update(
                                {
                                    "mode": {"mode", "power"},
                                    "spHeat": {"sp_heat"},
                                    "spCool": {"sp_cool"},
                                    "fanSpeed": {"fan_speed"},
                                    "vaneDir": {"vane"},
                                    "tempSource": {"temp_source"},
                                    "roomTemp": {"room_temp"},
                                    "roomTempOffset": {"room_temp_offset"},
                                }.get(key, set())
                            )
        except LocalError as err:
            err.__dict__.update(sent_count=sent_count, sent_keys=sent_keys)
            raise

    async def async_probe(self, serial: str) -> bool:
        """True when a single status read succeeds. False on a local error."""
        client = self._client(serial)
        try:
            await client.request(self._codec.query("status"))
        except LocalError:
            return False
        return True

    async def async_close(self) -> None:
        """Close every unit client."""
        clients = list(self._clients.values())
        self._clients.clear()
        self._buses.clear()
        for client in clients:
            await client.aclose()

    def _client(self, serial: str) -> LocalUnitClient:
        try:
            return self._clients[serial]
        except KeyError as exc:
            raise CredentialsMissingError(serial) from exc

    async def _fetch_status(self, client: LocalUnitClient) -> dict[str, Any]:
        response = await client.retrieve(*_READS["status"])
        return self._codec.decode_status(response)

    async def _fetch_sensors(self, client: LocalUnitClient, serial: str) -> dict[str, Any] | None:
        slots: dict[str, Any] = {}
        try:
            for index in range(POSSIBLE_SENSORS):
                response = await client.retrieve(("sensors", str(index)), _SENSOR_ATTRIBUTES)
                raw = dig(response, "r", "sensors", str(index))
                if not isinstance(raw, Mapping) or not raw.get("uuid"):
                    break
                slots[str(index)] = raw
        except LocalError as exc:
            self._skip(serial, "sensors", exc)
            if not slots:
                return None
        try:
            return self._codec.decode_sensors({"r": {"sensors": slots}})
        except LocalError as exc:
            self._skip(serial, "sensors", exc)
            return None

    async def _fetch_adapter(self, client: LocalUnitClient, serial: str) -> dict[str, Any] | None:
        try:
            response = await client.retrieve(*_READS["adapter"])
            values, overlay = self._codec.decode_adapter(response)
        except LocalError as exc:
            self._skip(serial, "adapter", exc)
            return None
        changed = self._overlays.get(serial) != overlay
        self._overlays[serial] = overlay
        if changed and serial in self._profiles:
            caps = self._codec.decode_profile(self._profiles[serial], overlay)
            self._capabilities[serial] = caps
            values["capabilities"] = caps
        return values

    async def _fetch_profile(self, client: LocalUnitClient, serial: str) -> dict[str, Any] | None:
        try:
            response = await client.retrieve(*_READS["profile"])
            caps = self._codec.decode_profile(response, self._overlays.get(serial))
        except LocalError as exc:
            self._skip(serial, "profile", exc)
            return None
        self._profiles[serial] = response
        self._capabilities[serial] = caps
        return {"capabilities": caps}

    async def _fetch_optional(
        self, client: LocalUnitClient, serial: str, node: Node
    ) -> dict[str, Any] | None:
        try:
            response = await client.request(self._codec.query(node))
            if node == "mhk2":
                return self._codec.decode_mhk2(response)
        except LocalError as exc:
            self._skip(serial, node, exc)
            return None
        return None

    def _skip(self, serial: str, node: str, exc: BaseException) -> None:
        _LOGGER.warning(
            "local fetch skipped serial=%s node=%s error=%s",
            serial,
            node,
            type(exc).__name__,
        )
