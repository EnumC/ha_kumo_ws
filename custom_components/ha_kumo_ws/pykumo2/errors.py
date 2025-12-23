"""Custom exceptions for the Mitsubishi Comfort client."""


class MitsubishiComfortError(Exception):
    """Base error raised for Mitsubishi Comfort failures."""


class AuthenticationError(MitsubishiComfortError):
    """Raised when authentication or refresh fails."""
