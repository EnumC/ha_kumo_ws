"""Credential persistence Protocol, JSON schema and an in-memory store."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from .models import Secret, UnitCredentials

SCHEMA_VERSION = 1


@dataclass(slots=True)
class StoredCredentials:
    """Persisted units plus per-serial non-secret metadata."""

    units: dict[str, UnitCredentials] = field(default_factory=dict)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)


class CredentialRepository(Protocol):
    """Storage backend for credentials."""

    async def async_load(self) -> StoredCredentials: ...

    async def async_save(self, data: StoredCredentials) -> None: ...


def to_json_dict(data: StoredCredentials) -> dict[str, Any]:
    """Serialize to the version 1 schema. Secrets are revealed."""
    units: dict[str, Any] = {}
    for serial, unit in data.units.items():
        units[serial] = {
            "password": unit.password_b64.reveal(),
            "cryptoSerial": unit.crypto_serial_hex.reveal(),
            "label": unit.label,
            "mac": unit.mac,
            "unit_type": unit.unit_type,
            "address": unit.address,
            "address_pinned": unit.address_pinned,
            "source": unit.source,
            "updated_at": unit.updated_at,
            "verified_at": unit.verified_at,
        }
    return {
        "version": SCHEMA_VERSION,
        "units": units,
        "meta": {serial: dict(values) for serial, values in data.meta.items()},
    }


def from_json_dict(raw: dict[str, Any]) -> StoredCredentials:
    """Parse the version 1 schema. Raises ValueError on a bad version."""
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported_version")
    units: dict[str, UnitCredentials] = {}
    for serial, item in (raw.get("units") or {}).items():
        source = item.get("source", "cloud")
        if source not in ("cloud", "import", "local"):
            source = "cloud"
        verified = item.get("verified_at")
        units[serial] = UnitCredentials(
            serial=serial,
            password_b64=Secret(item.get("password", "")),
            crypto_serial_hex=Secret(item.get("cryptoSerial", "")),
            label=item.get("label", ""),
            mac=item.get("mac", ""),
            unit_type=item.get("unit_type") or "ductless",
            address=item.get("address", ""),
            address_pinned=bool(item.get("address_pinned", False)),
            source=cast(Literal["cloud", "import", "local"], source),
            updated_at=float(item.get("updated_at", 0.0)),
            verified_at=None if verified is None else float(verified),
        )
    meta = {serial: dict(values) for serial, values in (raw.get("meta") or {}).items()}
    return StoredCredentials(units=units, meta=meta)


class InMemoryCredentialRepository:
    """Repository kept in memory, for tests and local-only use."""

    def __init__(self, initial: StoredCredentials | None = None) -> None:
        self._data = to_json_dict(initial or StoredCredentials())
        self.saves = 0

    async def async_load(self) -> StoredCredentials:
        return from_json_dict(self._data)

    async def async_save(self, data: StoredCredentials) -> None:
        self._data = to_json_dict(data)
        self.saves += 1
