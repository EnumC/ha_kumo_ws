"""Domain enums shared by the local and cloud codecs."""

import logging
from enum import StrEnum
from typing import Any, Self

_LOGGER = logging.getLogger(__name__)
_SEEN_UNKNOWN: set[tuple[str, str]] = set()


class _LenientEnum(StrEnum):
    """StrEnum with a case-insensitive parse that returns None for unknown values."""

    @classmethod
    def parse(cls, value: object) -> Self | None:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            return None
        folded = value.casefold()
        for member in cls:
            if member.value.casefold() == folded:
                return member
        key = (cls.__name__, value)
        if key not in _SEEN_UNKNOWN:
            _SEEN_UNKNOWN.add(key)
            _LOGGER.debug("Unknown %s value: %r", cls.__name__, value)
        return None


class HvacMode(_LenientEnum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    DRY = "dry"
    VENT = "vent"


class FanSpeed(_LenientEnum):
    SUPER_QUIET = "superQuiet"
    QUIET = "quiet"
    LOW = "low"
    POWERFUL = "powerful"
    SUPER_POWERFUL = "superPowerful"
    AUTO = "auto"


class VaneDirection(_LenientEnum):
    AUTO = "auto"
    HORIZONTAL = "horizontal"
    MIDHORIZONTAL = "midhorizontal"
    MIDPOINT = "midpoint"
    MIDVERTICAL = "midvertical"
    VERTICAL = "vertical"
    SWING = "swing"


class TempSource(_LenientEnum):
    SENSOR0 = "sensor0"
    SENSOR1 = "sensor1"
    SENSOR2 = "sensor2"
    SENSOR3 = "sensor3"
    RETURNAIR = "returnair"
    REMOTE = "remote"
    API = "api"
    UNSET = "unset"

    @property
    def is_settable(self) -> bool:
        """unset is reported by the adapter but rejected on write."""
        return self is not TempSource.UNSET


SETTABLE_TEMP_SOURCES = frozenset(s for s in TempSource if s.is_settable)


class ConnectionMode(_LenientEnum):
    AUTO = "auto"
    LOCAL_ONLY = "local_only"
    CLOUD_ONLY = "cloud_only"


class SetupMethod(_LenientEnum):
    LOCAL_BACKUP = "local_backup"
    CLOUD_WS = "cloud_ws"
    CLOUD_FETCH = "cloud_fetch"


class LinkState(_LenientEnum):
    NO_CREDENTIALS = "no_credentials"
    LOCAL_OK = "local_ok"
    LOCAL_DEGRADED = "local_degraded"
    CLOUD_FALLBACK = "cloud_fallback"
    AUTH_STALE = "auth_stale"
    ADDRESS_LOST = "address_lost"
    RECOVERING = "recovering"
    CLOUD_ONLY = "cloud_only"


def decode_operation_mode(raw: object) -> dict[str, Any]:
    """Map a wire mode (incl. autoHeat/autoCool) to power/mode/auto_active values."""
    if isinstance(raw, str):
        folded = raw.casefold()
        if folded == "autoheat":
            return {"power": True, "mode": HvacMode.AUTO, "auto_active": HvacMode.HEAT}
        if folded == "autocool":
            return {"power": True, "mode": HvacMode.AUTO, "auto_active": HvacMode.COOL}
    mode = HvacMode.parse(raw)
    if mode is None:
        return {}
    if mode is HvacMode.OFF:
        return {"power": False}
    return {"power": True, "mode": mode, "auto_active": None}
