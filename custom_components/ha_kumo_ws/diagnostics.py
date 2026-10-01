"""Diagnostics; every secret and identifier is redacted."""

import dataclasses
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .hub import KumoConfigEntry
from .pykumo2.util.redact import redact

TO_REDACT = {
    "password",
    "password_b64",
    "cryptoSerial",
    "crypto_serial_hex",
    "access",
    "refresh",
    "token",
    "mac",
    "address",
    "serial",
    "serial_number",
    "routerSsid",
    "ssid",
    "username",
    "unique_id",
    "uuid",
    "ip_overrides",
    "title",
}


def _plain(value: Any) -> Any:
    """Dataclasses and enums to JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((_plain(v) for v in value), key=str)
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KumoConfigEntry
) -> dict[str, Any]:
    """Entry, per-device state, routing and cloud usage."""
    hub = entry.runtime_data
    devices = []
    for index, (serial, coordinator) in enumerate(sorted(hub.coordinators.items())):
        link = coordinator.link
        creds = hub.credentials.get(serial) if hub.credentials is not None else None
        devices.append(
            {
                "device": f"device_{index}",
                "model": coordinator.device.model,
                "site": "set" if coordinator.device.site_id else "unset",
                "state": _plain(coordinator.data),
                "capabilities": _plain(coordinator.capabilities),
                "link_state": link.state.value,
                "active_transport": _plain(link.active_transport),
                "circuit": link.health.state.value,
                "last_update_success": coordinator.last_update_success,
                "has_local_credentials": creds is not None and creds.has_secrets,
                "credentials_verified": creds is not None and creds.verified_at is not None,
            }
        )
    data = {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "setup_method": hub.method.value,
        "policy": {
            "mode": hub.policy.mode.value,
            "has_local": hub.policy.has_local,
            "has_cloud": hub.policy.has_cloud,
        },
        "devices": devices,
        "cloud": {
            "calls_last_hour": hub.ledger.counts_last_hour(),
            "total_calls": hub.ledger.total,
            "rate_limit": hub.ledger.limit,
            "rate_limit_remaining": hub.ledger.remaining,
            "socket_connected": hub.socket.connected if hub.socket is not None else None,
            "leased_devices": len(hub.leases.leased) if hub.leases is not None else 0,
        },
    }
    return async_redact_data(redact(data), TO_REDACT)
