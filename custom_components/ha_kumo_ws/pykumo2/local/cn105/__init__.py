"""CN105 info-reply decoders."""

from collections.abc import Callable
from enum import IntEnum

from .frames import (
    FRAME_OVERHEAD,
    INFO_REQUEST_TYPE,
    INFO_RESPONSE_TYPE,
    MAX_FRAME_LEN,
    PACKET_HEADER,
    PACKET_SUBHEADER,
    PAYLOAD_SIZE,
    build_cn105_frame,
    build_info_request,
    cn105_checksum,
    is_info_reply,
    valid_cn105_reply,
)

_Scalar = float | int | bool | str | None
_Decoder = Callable[[bytes], dict[str, _Scalar]]


class InfoCode(IntEnum):
    """Info codes a unit can be asked for, sent as the first payload byte."""

    TEMPERATURES = 0x03
    # Not every unit answers 0x06; asking one that does not stalls later reads.
    COMPRESSOR = 0x06
    SUB_MODE = 0x09


# COMPRESSOR (0x06) is left out on purpose.
DEFAULT_INFO_CODES = (InfoCode.TEMPERATURES, InfoCode.SUB_MODE)

SUB_MODE_NAMES = {
    0x00: "NORMAL",
    0x01: "WARMUP",
    0x02: "DEFROST",
    0x04: "PREHEAT",
    0x08: "STANDBY",
    0x10: "OFF",
}
STAGE_NAMES = {
    0x00: "IDLE",
    0x01: "LOW",
    0x02: "GENTLE",
    0x03: "MEDIUM",
    0x04: "MODERATE",
    0x05: "HIGH",
    0x06: "DIFFUSE",
}
# Auto sub mode: older units use 0x00..0x03, newer MFZ units 0x40/0x41/0x43.
AUTO_SUB_MODE_NAMES = {
    0x00: "AUTO_OFF",
    0x01: "AUTO_COOL",
    0x02: "AUTO_HEAT",
    0x03: "AUTO_LEADER",
    0x40: "AUTO_INACTIVE",
    0x41: "AUTO_IDLE",
    0x43: "AUTO_ACTIVE",
}


def _decode_temperatures(frame: bytes) -> dict[str, _Scalar]:
    room: float | int | None = None
    outdoor: float | None = None
    runtime: int | None = None
    # Byte 10, (b - 128) / 2; <= 1 means no reading.
    if len(frame) > 10 and frame[10] > 1:
        outdoor = (frame[10] - 128) / 2
    # Byte 11 if set, else the older byte 8 scale (0x00..0x1F is 10..41 C).
    if len(frame) > 11 and frame[11]:
        room = (frame[11] - 128) / 2
    elif len(frame) > 8 and frame[8] <= 0x1F:
        room = 10 + frame[8]
    if len(frame) > 18:
        runtime = (frame[16] << 16) | (frame[17] << 8) | frame[18]
    return {
        "room_temperature": room,
        "outdoor_temperature": outdoor,
        "compressor_runtime_minutes": runtime,
    }


def _decode_compressor(frame: bytes) -> dict[str, _Scalar]:
    operating: bool | None = None
    frequency: int | None = None
    if len(frame) > 9:
        operating = frame[9] == 1
        # Byte 8, but 0 while idle since some units put noise here.
        frequency = frame[8] if operating else 0
    return {"operating": operating, "compressor_frequency": frequency}


def _decode_sub_mode(frame: bytes) -> dict[str, _Scalar]:
    return {
        "sub_mode": SUB_MODE_NAMES.get(frame[8]) if len(frame) > 8 else None,
        "stage": STAGE_NAMES.get(frame[9]) if len(frame) > 9 else None,
        "auto_sub_mode": AUTO_SUB_MODE_NAMES.get(frame[10]) if len(frame) > 10 else None,
    }


_DECODERS: dict[InfoCode, _Decoder] = {
    InfoCode.TEMPERATURES: _decode_temperatures,
    InfoCode.COMPRESSOR: _decode_compressor,
    InfoCode.SUB_MODE: _decode_sub_mode,
}

TELEMETRY_KEYS: dict[InfoCode, tuple[str, ...]] = {
    code: tuple(decoder(b"")) for code, decoder in _DECODERS.items()
}


def decode_info_reply(frame: bytes | bytearray | None, code: int) -> dict[str, _Scalar]:
    """Decode a reply into a ``{field: value}`` dict."""
    try:
        info_code = InfoCode(code)
    except ValueError:
        info_code = None
    decoder = _DECODERS.get(info_code) if info_code is not None else None
    if decoder is None:
        raise ValueError(f"no CN105 decoder for info code 0x{code:02x}")
    if frame is not None and is_info_reply(frame, code):
        return decoder(bytes(frame))
    return decoder(b"")


__all__ = [
    "AUTO_SUB_MODE_NAMES",
    "DEFAULT_INFO_CODES",
    "FRAME_OVERHEAD",
    "INFO_REQUEST_TYPE",
    "INFO_RESPONSE_TYPE",
    "MAX_FRAME_LEN",
    "PACKET_HEADER",
    "PACKET_SUBHEADER",
    "PAYLOAD_SIZE",
    "STAGE_NAMES",
    "SUB_MODE_NAMES",
    "TELEMETRY_KEYS",
    "InfoCode",
    "build_cn105_frame",
    "build_info_request",
    "cn105_checksum",
    "decode_info_reply",
    "is_info_reply",
    "valid_cn105_reply",
]
