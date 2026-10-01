"""Transport protocols shared by the local and cloud implementations."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from .domain.commands import Command
    from .domain.state import DeviceState, StatePatch


class TransportKind(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


Node = Literal["status", "sensors", "profile", "adapter", "mhk2", "cn105"]


class LeaseReason(StrEnum):
    """Why a cloud socket lease is held for a serial."""

    FALLBACK = "fallback"
    CLOUD_ONLY = "cloud_only"
    CREDENTIAL_REFRESH = "credential_refresh"
    PROFILE_BOOTSTRAP = "profile_bootstrap"


class Transport(Protocol):
    """Reads state from and executes commands on units."""

    kind: TransportKind

    def supports(self, command: Command) -> bool: ...

    async def async_fetch(self, serial: str, nodes: frozenset[Node]) -> StatePatch: ...

    async def async_execute(self, serial: str, command: Command, state: DeviceState) -> None: ...

    async def async_close(self) -> None: ...


class PushTransport(Transport, Protocol):
    """Transport that also pushes state changes."""

    def set_listener(self, cb: Callable[[str, StatePatch], None]) -> None: ...

    async def async_acquire(self, serial: str, reason: LeaseReason) -> None: ...

    async def async_release(self, serial: str, reason: LeaseReason) -> None: ...
