"""Exception hierarchy for pykumo2."""


class KumoError(Exception):
    """Base error for all pykumo2 failures."""


class CloudError(KumoError):
    """Kumo Cloud REST or socket failure."""


class AuthenticationError(CloudError):
    """Cloud login or token refresh failed."""


class RateLimitedError(CloudError):
    """Cloud refused the call because of rate limiting."""

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LocalError(KumoError):
    """Local unit API failure."""


class LocalTimeoutError(LocalError):
    """The unit did not answer in time."""


class LocalConnectionError(LocalError):
    """The unit refused or dropped the connection."""


class LocalAuthError(LocalError):
    """The unit rejected the request signature."""


class LocalBusyError(LocalError):
    """The unit reported a transient serializer or memory error."""


class LocalProtocolError(LocalError):
    """The unit returned an unexpected response."""


class CommandNotSupportedError(KumoError):
    """The command cannot be executed on the chosen transport."""


class CredentialsMissingError(KumoError):
    """No local credentials are stored for the unit."""
