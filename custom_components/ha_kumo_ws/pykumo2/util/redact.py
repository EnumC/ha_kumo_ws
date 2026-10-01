"""Secret redaction for logs and diagnostics."""

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "**REDACTED**"

SECRET_SUBSTRINGS = (
    "password",
    "token",
    "crypto",
    "secret",
    "cookie",
    "authorization",
    "api-key",
    "apikey",
    "api_key",
)
SECRET_EXACT = frozenset(
    {"m", "access", "refresh", "error", "errors", "message", "detail", "description"}
)

_M_PARAM = re.compile(r"([?&]m=)[^&#]*")


def _is_secret(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in SECRET_EXACT or any(part in lowered for part in SECRET_SUBSTRINGS)


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with secret values replaced."""
    if isinstance(obj, Mapping):
        return {k: REDACTED if _is_secret(k) else redact(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    return obj


def redact_url(url: str) -> str:
    """Mask the m= signature query parameter."""
    return _M_PARAM.sub(rf"\g<1>{REDACTED}", url)
