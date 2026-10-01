"""Local request signing. Pure; no I/O."""

import base64
import hashlib

from .const import S_PARAM, W_PARAM


def compute_token(password: bytes, crypto_serial: bytes, body: bytes) -> str:
    """Compute the auth token for a local API request body."""
    data_hash = hashlib.sha256(password + body).digest()

    intermediate = bytearray(88)
    intermediate[0:32] = W_PARAM[0:32]
    intermediate[32:64] = data_hash[0:32]
    intermediate[64:66] = bytearray.fromhex("0840")
    intermediate[66] = S_PARAM
    intermediate[79] = crypto_serial[8]
    intermediate[80:84] = crypto_serial[4:8]
    intermediate[84:88] = crypto_serial[0:4]

    return hashlib.sha256(intermediate).hexdigest()


def decode_credentials(password_b64: str, crypto_serial_hex: str) -> tuple[bytes, bytes]:
    """Decode a base64 password and a hex crypto serial."""
    try:
        password = base64.b64decode(password_b64, validate=True)
    except ValueError as exc:
        raise ValueError("invalid_password") from exc
    try:
        crypto_serial = bytes.fromhex(crypto_serial_hex)
    except ValueError as exc:
        raise ValueError("invalid_crypto_serial") from exc
    if len(crypto_serial) < 9:
        raise ValueError("crypto_serial_too_short")
    return password, crypto_serial
