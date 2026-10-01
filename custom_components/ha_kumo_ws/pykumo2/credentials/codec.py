"""Import and export unit credentials. No secret values in error strings."""

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..local.signing import decode_credentials
from .models import Secret, UnitCredentials

_BACKUP_FORMAT = "ha_kumo_ws.credentials"
_BACKUP_VERSION = 1


_UNIT_KEYS = frozenset({"serial", "password", "cryptoSerial"})


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """Units parsed from a document, plus errors that name only a serial or index."""

    units: list[UnitCredentials]
    errors: list[str]
    account_username: str | None = None


def decode(text: str) -> DecodeResult:
    """Parse a credentials backup, kumo_cache v2, pykumo kumo.cfg or a zone table."""
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_backup") from exc
    return decode_data(parsed)


def decode_data(parsed: object) -> DecodeResult:
    """Like decode() for an already parsed JSON value."""
    as_dict = _as_dict(parsed)
    if as_dict is not None:
        if as_dict.get("format") == _BACKUP_FORMAT:
            return _decode_backup(as_dict)
        if _is_kumo_cfg(as_dict):
            return _decode_kumo_cfg(as_dict)
        return _decode_zone_table(as_dict)
    if isinstance(parsed, list):
        return _decode_kumo_cache(parsed)
    return DecodeResult(units=[], errors=["unrecognized_format"])


def encode_backup(units: Iterable[UnitCredentials]) -> str:
    """Serialize units as a pretty ha_kumo_ws credentials backup, sorted by serial."""
    payload = {
        "format": _BACKUP_FORMAT,
        "version": _BACKUP_VERSION,
        "units": [
            {
                **_unit_fields(unit),
                "addressPinned": unit.address_pinned,
                "verifiedAt": unit.verified_at,
            }
            for unit in sorted(units, key=lambda unit: unit.serial)
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def encode_kumo_cache(units: Iterable[UnitCredentials]) -> str:
    """Serialize units in the kumo_cache v2 list layout pykumo writes."""
    zone_table: dict[str, object] = {}
    for unit in sorted(units, key=lambda item: item.serial):
        fields = _unit_fields(unit)
        fields["reachable"] = bool(unit.address)
        zone_table[unit.serial] = fields
    payload: list[object] = [{}, {}, {"children": [{"zoneTable": zone_table}]}]
    return json.dumps(payload, indent=2) + "\n"


def _unit_fields(unit: UnitCredentials) -> dict[str, object]:
    return {
        "serial": unit.serial,
        "label": unit.label,
        "password": unit.password_b64.reveal(),
        "cryptoSerial": unit.crypto_serial_hex.reveal(),
        "mac": unit.mac,
        "unitType": unit.unit_type,
        "address": unit.address,
    }


def _decode_backup(data: dict[str, object]) -> DecodeResult:
    if data.get("version") != _BACKUP_VERSION:
        return DecodeResult(units=[], errors=["unsupported_version"])
    raw_units = data.get("units")
    if not isinstance(raw_units, list):
        return DecodeResult(units=[], errors=["missing_units"])
    units: list[UnitCredentials] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_units):
        unit = _parse_unit(raw, index, errors, None)
        if unit is not None:
            units.append(_with_state(unit, raw))
    return DecodeResult(units=units, errors=errors)


def _with_state(unit: UnitCredentials, raw: object) -> UnitCredentials:
    mapping = _as_dict(raw) or {}
    verified = mapping.get("verifiedAt")
    return replace(
        unit,
        address_pinned=mapping.get("addressPinned") is True,
        verified_at=float(verified)
        if isinstance(verified, int | float) and not isinstance(verified, bool)
        else None,
    )


def _decode_kumo_cache(data: list[object]) -> DecodeResult:
    """Port of pykumo ``KumoCloudAccount._extract_cached_units``."""
    if len(data) < 3:
        return DecodeResult(units=[], errors=["invalid_kumo_cache"])
    root = _as_dict(data[2])
    children = root.get("children") if root is not None else None
    if not isinstance(children, list):
        return DecodeResult(units=[], errors=["invalid_kumo_cache"])
    found: dict[str, UnitCredentials] = {}
    errors: list[str] = []
    for child_index, child in enumerate(children):
        child_map = _as_dict(child)
        if child_map is None:
            errors.append(f"index {child_index}: not_an_object")
            continue
        _take_zone_table(child_map.get("zoneTable"), errors, found, f"index {child_index}")
        _take_grandchildren(child_map, errors, found, child_index)
    return DecodeResult(units=list(found.values()), errors=errors)


def _take_grandchildren(
    child_map: dict[str, object],
    errors: list[str],
    found: dict[str, UnitCredentials],
    child_index: int,
) -> None:
    if "children" not in child_map:
        return
    raw_children = child_map["children"]
    if not isinstance(raw_children, list):
        errors.append(f"index {child_index}: invalid_children")
        return
    for grand_index, grandchild in enumerate(raw_children):
        grand_map = _as_dict(grandchild)
        ident = f"index {child_index}.{grand_index}"
        if grand_map is None:
            errors.append(f"{ident}: not_an_object")
            continue
        _take_zone_table(grand_map.get("zoneTable"), errors, found, ident)


def _is_kumo_cfg(data: dict[str, object]) -> bool:
    """pykumo kumo.cfg: {username: {serial: unit}}; zone table units hold plain values."""
    if not data:
        return False
    for table in data.values():
        if not isinstance(table, dict) or not table or _UNIT_KEYS & table.keys():
            return False
        if not all(isinstance(unit, dict) for unit in table.values()):
            return False
    return True


def _decode_kumo_cfg(data: dict[str, object]) -> DecodeResult:
    found: dict[str, UnitCredentials] = {}
    errors: list[str] = []
    for username, table in data.items():
        _take_zone_table(table, errors, found, username, key_is_serial=True)
    username = next(iter(data))
    return DecodeResult(units=list(found.values()), errors=errors, account_username=username)


def _decode_zone_table(data: dict[str, object]) -> DecodeResult:
    found: dict[str, UnitCredentials] = {}
    errors: list[str] = []
    _take_zone_table(data, errors, found, "zoneTable")
    return DecodeResult(units=list(found.values()), errors=errors)


def _take_zone_table(
    table: object,
    errors: list[str],
    found: dict[str, UnitCredentials],
    ident: str,
    *,
    key_is_serial: bool = False,
) -> None:
    mapping = _as_dict(table)
    if mapping is None:
        errors.append(f"{ident}: missing_zone_table")
        return
    for index, (key, raw) in enumerate(mapping.items()):
        unit = _parse_unit(raw, index, errors, key, key_is_serial=key_is_serial)
        if unit is not None:
            found[unit.serial] = unit


def _parse_unit(
    raw: object,
    index: int,
    errors: list[str],
    fallback_serial: str | None,
    *,
    key_is_serial: bool = False,
) -> UnitCredentials | None:
    ident = fallback_serial or f"index {index}"
    mapping = _as_dict(raw)
    if mapping is None:
        errors.append(f"{ident}: not_an_object")
        return None
    serial_raw = mapping.get("serial") or (fallback_serial if key_is_serial else None)
    if isinstance(serial_raw, str) and serial_raw:
        serial = serial_raw
    else:
        errors.append(f"{ident}: missing_serial")
        return None
    password = mapping.get("password")
    crypto = mapping.get("cryptoSerial")
    if not isinstance(password, str) or not password:
        errors.append(f"{serial}: missing_password")
        return None
    if not isinstance(crypto, str) or not crypto:
        errors.append(f"{serial}: missing_crypto_serial")
        return None
    try:
        decode_credentials(password, crypto)
    except ValueError:
        errors.append(f"{serial}: invalid_credentials")
        return None
    unit_type = _as_str(mapping.get("unitType")) or "ductless"
    return UnitCredentials(
        serial=serial,
        password_b64=Secret(password),
        crypto_serial_hex=Secret(crypto),
        label=_as_str(mapping.get("label")),
        mac=_as_str(mapping.get("mac")),
        unit_type=unit_type,
        address=_as_str(mapping.get("address")),
        source="import",
    )


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""
