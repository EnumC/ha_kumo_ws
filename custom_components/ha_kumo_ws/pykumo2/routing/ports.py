"""Narrow ports the router depends on; the HA layer wires the concrete classes."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from ..domain.commands import Command
from ..domain.state import DeviceState, StatePatch
from ..transport import LeaseReason, Node, Transport

if TYPE_CHECKING:
    from ..cloud.transport import CloudTransport
    from ..credentials.service import CredentialService
    from ..local.transport import LocalTransport


class LocalPort(Transport, Protocol):
    """Local transport plus the probe and address hooks the router needs."""

    async def async_probe(self, serial: str) -> bool: ...

    def update_address(self, serial: str, address: str) -> None: ...

    def has_unit(self, serial: str) -> bool: ...


class CloudPort(Protocol):
    """Cloud transport with socket leases."""

    def supports(self, command: Command) -> bool: ...

    async def async_fetch(self, serial: str, nodes: frozenset[Node]) -> StatePatch: ...

    async def async_execute(self, serial: str, command: Command, state: DeviceState) -> None: ...

    async def async_acquire(
        self, serial: str, reason: LeaseReason, *, want_profile: bool = False
    ) -> None: ...

    async def async_release(self, serial: str, reason: LeaseReason) -> None: ...

    async def async_request_profile(self, serial: str) -> None: ...


class UnitAddress(Protocol):
    """Address fields of stored unit credentials."""

    @property
    def address(self) -> str: ...

    @property
    def address_pinned(self) -> bool: ...


class RefreshOutcome(Protocol):
    """Result of a credential refresh."""

    @property
    def refreshed(self) -> Sequence[str]: ...

    @property
    def error(self) -> str | None: ...


class CredentialPort(Protocol):
    """Credential store; rate limiting of refreshes is its job."""

    def get(self, serial: str) -> UnitAddress | None: ...

    async def async_request_refresh(
        self, serials: Sequence[str], *, reason: str, force: bool = False
    ) -> RefreshOutcome: ...

    async def async_set_address(
        self, serial: str, address: str, *, pinned: bool = False
    ) -> None: ...


class Scanner(Protocol):
    """Subnet discovery: fingerprint scan, then signed matching of serials to IPs."""

    async def scan(self, cidrs: Sequence[str]) -> list[str]: ...

    async def match(self, ips: Sequence[str], serials: Sequence[str]) -> dict[str, str]: ...


if TYPE_CHECKING:

    def _cloud_conforms(transport: CloudTransport) -> CloudPort:
        return transport

    def _creds_conform(service: CredentialService) -> CredentialPort:
        return service

    def _local_conforms(transport: LocalTransport) -> LocalPort:
        return transport
