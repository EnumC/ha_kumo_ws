"""Build and validate raw CN105/ITP serial frames."""

PACKET_HEADER = 0xFC
PACKET_SUBHEADER = bytes((0x01, 0x30))
FRAME_OVERHEAD = 6
INFO_REQUEST_TYPE = 0x42
INFO_RESPONSE_TYPE = 0x62
# Info request payloads are always padded to 16 bytes.
PAYLOAD_SIZE = 16
# Firmware uses a signed length byte, so 1..127 is usable.
MAX_FRAME_LEN = 127


def cn105_checksum(data: bytes) -> int:
    """Return the CN105 frame checksum for ``data`` (all preceding bytes)."""
    return (0xFC - sum(data)) & 0xFF


def build_cn105_frame(type_byte: int, payload: bytes, pad_to: int = PAYLOAD_SIZE) -> bytes:
    """Build a whole frame: header, payload, then the checksum."""
    if not 0 <= type_byte <= 0xFF:
        raise ValueError("type_byte must be 0..255")
    if not 0 <= pad_to <= PAYLOAD_SIZE:
        raise ValueError(f"pad_to must be 0..{PAYLOAD_SIZE}")
    padded = bytes(payload).ljust(pad_to, b"\x00")
    if len(padded) > PAYLOAD_SIZE:
        raise ValueError(f"payload must be <= {PAYLOAD_SIZE} bytes")
    body = bytes((PACKET_HEADER, type_byte)) + PACKET_SUBHEADER
    body += bytes((len(padded),)) + padded
    return body + bytes((cn105_checksum(body),))


def build_info_request(code: int) -> bytes:
    """Build an info-request frame (type ``0x42``) for ``code``."""
    if not 0 <= code <= 0xFF:
        raise ValueError("code must be 0..255")
    return build_cn105_frame(INFO_REQUEST_TYPE, bytes((code,)))


def valid_cn105_reply(frame: bytes | bytearray | None) -> bool:
    """True if ``frame`` begins with one well-formed frame whose checksum matches."""
    if not frame or len(frame) < FRAME_OVERHEAD:
        return False
    buf = bytes(frame)
    if buf[0] != PACKET_HEADER or buf[2:4] != PACKET_SUBHEADER:
        return False
    end = buf[4] + FRAME_OVERHEAD
    if len(buf) < end:
        return False
    return cn105_checksum(buf[: end - 1]) == buf[end - 1]


def is_info_reply(frame: bytes | bytearray | None, code: int) -> bool:
    """True if ``frame`` is a valid ``0x62`` reply to a request for ``code``."""
    if frame is None or not valid_cn105_reply(frame):
        return False
    return frame[1] == INFO_RESPONSE_TYPE and frame[5] == code
