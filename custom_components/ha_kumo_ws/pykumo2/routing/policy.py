"""Which transport polls and executes, given the mode and the link state."""

from typing import Self

from ..domain.commands import Command
from ..domain.enums import ConnectionMode, LinkState, SetupMethod
from ..errors import CommandNotSupportedError
from ..transport import TransportKind

LOCAL_UNAVAILABLE = "local_unavailable"
NOT_SUPPORTED_CLOUD = "not_supported_cloud"
CLOUD_DISABLED = "cloud_disabled"

LOCAL_STATES = frozenset({LinkState.LOCAL_OK, LinkState.LOCAL_DEGRADED, LinkState.RECOVERING})
FALLBACK_STATES = frozenset(
    {LinkState.CLOUD_FALLBACK, LinkState.AUTH_STALE, LinkState.ADDRESS_LOST}
)


class NoRouteError(CommandNotSupportedError):
    """No transport may execute the command; reason is a translation key."""

    def __init__(self, reason: str, command: Command | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.command = command


class RoutingPolicy:
    """Mode plus available transports. auto degrades to the only available side."""

    def __init__(self, mode: ConnectionMode, *, has_local: bool, has_cloud: bool) -> None:
        if mode is ConnectionMode.LOCAL_ONLY and not has_local:
            raise ValueError("local_only needs a local transport")
        if mode is ConnectionMode.CLOUD_ONLY and not has_cloud:
            raise ValueError("cloud_only needs a cloud transport")
        if mode is ConnectionMode.AUTO:
            if not has_local and not has_cloud:
                raise ValueError("auto needs a transport")
            if not has_cloud:
                mode = ConnectionMode.LOCAL_ONLY
            elif not has_local:
                mode = ConnectionMode.CLOUD_ONLY
        self.mode = mode
        self.has_local = has_local and mode is not ConnectionMode.CLOUD_ONLY
        self.has_cloud = has_cloud and mode is not ConnectionMode.LOCAL_ONLY

    @classmethod
    def for_setup(cls, method: SetupMethod, mode: ConnectionMode, *, has_cloud: bool) -> Self:
        """cloud_ws entries never have local objects and are always cloud_only."""
        if method is SetupMethod.CLOUD_WS:
            return cls(ConnectionMode.CLOUD_ONLY, has_local=False, has_cloud=True)
        return cls(mode, has_local=True, has_cloud=has_cloud)

    @property
    def allow_cloud_retry_on_local_failure(self) -> bool:
        return self.mode is ConnectionMode.AUTO

    def local_ready(self, link_state: LinkState) -> bool:
        """True when local should be tried in this link state."""
        if not self.has_local:
            return False
        if self.mode is ConnectionMode.LOCAL_ONLY:
            return link_state is not LinkState.NO_CREDENTIALS
        return link_state in LOCAL_STATES

    def poll_transport(self, link_state: LinkState) -> TransportKind | None:
        if self.local_ready(link_state):
            return TransportKind.LOCAL
        if self.has_cloud:
            return TransportKind.CLOUD
        return None

    def command_transport(
        self,
        command: Command,
        link_state: LinkState,
        local_supports: bool,
        cloud_supports: bool,
    ) -> TransportKind:
        """Pick the transport for command or raise NoRouteError."""
        if local_supports and self.local_ready(link_state):
            return TransportKind.LOCAL
        if cloud_supports and self.has_cloud:
            return TransportKind.CLOUD
        if self.mode is ConnectionMode.CLOUD_ONLY:
            raise NoRouteError(NOT_SUPPORTED_CLOUD, command)
        if local_supports:
            raise NoRouteError(LOCAL_UNAVAILABLE, command)
        if self.mode is ConnectionMode.LOCAL_ONLY:
            raise NoRouteError(CLOUD_DISABLED, command)
        raise NoRouteError(NOT_SUPPORTED_CLOUD, command)
