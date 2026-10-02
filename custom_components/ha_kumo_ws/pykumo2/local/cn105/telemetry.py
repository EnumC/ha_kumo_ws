"""Read CN105 info codes into one telemetry snapshot."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from ...clock import Clock, SystemClock
from ...domain.state import Cn105Telemetry
from . import DEFAULT_INFO_CODES, TELEMETRY_KEYS, InfoCode, decode_info_reply

_LOGGER = logging.getLogger(__name__)
_SYSTEM_CLOCK = SystemClock()


class InfoBus(Protocol):
    """The slice of the CN105 bus this reader uses."""

    async def read_info(
        self,
        code: int,
        timeout: float = ...,  # noqa: ASYNC109
    ) -> bytes | None: ...


def compressor_running(operating: bool | None, mode: str | None) -> bool | None:
    """The 0x06 flag; False when off or idle without it, else None."""
    if operating is not None:
        return operating
    if mode in ("off", "idle"):
        return False
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class Cn105TelemetryReader:
    """Asks the bus for info codes and folds them into ``Cn105Telemetry``."""

    def __init__(
        self,
        bus: InfoBus,
        *,
        codes: Sequence[int] = DEFAULT_INFO_CODES,
        clock: Clock = _SYSTEM_CLOCK,
    ) -> None:
        self._bus = bus
        self._codes = tuple(codes)
        self._clock = clock

    async def read(
        self,
        *,
        mode: str | None = None,
        codes: Sequence[int] | None = None,
    ) -> Cn105Telemetry:
        """Read each code. Failed codes leave their own fields None."""
        try:
            return await self._read(mode, self._codes if codes is None else tuple(codes))
        except Exception as exc:
            _LOGGER.warning("cn105 read failed error=%s", type(exc).__name__)
            return Cn105Telemetry()

    async def _read(self, mode: str | None, codes: tuple[int, ...]) -> Cn105Telemetry:
        fields: dict[str, Any] = {}
        answered = False
        for code in codes:
            info = _known_code(code)
            if info is None:
                _LOGGER.warning("cn105 unknown code=%s", code)
                continue
            for key in TELEMETRY_KEYS[info]:
                fields[key] = None
            try:
                reply = await self._bus.read_info(int(info))
            except Exception as exc:
                _LOGGER.warning("cn105 read failed code=%s error=%s", int(info), type(exc).__name__)
                continue
            if reply is None:
                continue
            fields.update(decode_info_reply(reply, int(info)))
            answered = True
        operating = compressor_running(_as_bool(fields.get("operating")), mode)
        return Cn105Telemetry(
            room_temperature=_as_float(fields.get("room_temperature")),
            outdoor_temperature=_as_float(fields.get("outdoor_temperature")),
            compressor_runtime_minutes=_as_int(fields.get("compressor_runtime_minutes")),
            operating=operating,
            compressor_frequency=_as_int(fields.get("compressor_frequency")),
            sub_mode=_as_str(fields.get("sub_mode")),
            stage=_as_str(fields.get("stage")),
            auto_sub_mode=_as_str(fields.get("auto_sub_mode")),
            read_at=self._clock.now() if answered else None,
        )


def _known_code(code: object) -> InfoCode | None:
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    try:
        info = InfoCode(code)
    except ValueError:
        return None
    if info not in TELEMETRY_KEYS:
        return None
    return info
