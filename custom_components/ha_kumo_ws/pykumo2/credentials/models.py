"""Per-unit local credentials. Secrets stay usable but hide in repr."""

from dataclasses import dataclass
from typing import Literal

from ..local.signing import decode_credentials


class Secret(str):
    """A str whose repr does not show the value."""

    def __repr__(self) -> str:
        return "Secret('**********')"

    def reveal(self) -> str:
        """Return the value as a plain str."""
        return str.__str__(self)


@dataclass(frozen=True, slots=True)
class UnitCredentials:
    """Credentials and address for one indoor unit."""

    serial: str
    password_b64: Secret
    crypto_serial_hex: Secret
    label: str = ""
    mac: str = ""
    unit_type: str = "ductless"
    address: str = ""
    address_pinned: bool = False
    source: Literal["cloud", "import", "local"] = "cloud"
    updated_at: float = 0.0
    verified_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.password_b64, Secret):
            object.__setattr__(self, "password_b64", Secret(self.password_b64))
        if not isinstance(self.crypto_serial_hex, Secret):
            object.__setattr__(self, "crypto_serial_hex", Secret(self.crypto_serial_hex))

    def validate(self) -> None:
        """Raise ValueError if the secrets cannot be used for signing."""
        decode_credentials(self.password_b64, self.crypto_serial_hex)

    def password_bytes(self) -> bytes:
        """Return the decoded password."""
        password, _crypto = decode_credentials(self.password_b64, self.crypto_serial_hex)
        return password

    def crypto_bytes(self) -> bytes:
        """Return the decoded crypto serial."""
        _password, crypto = decode_credentials(self.password_b64, self.crypto_serial_hex)
        return crypto

    @property
    def has_secrets(self) -> bool:
        """True when both secret fields are non-empty."""
        return bool(self.password_b64) and bool(self.crypto_serial_hex)
