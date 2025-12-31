"""Lightweight models used by the Mitsubishi Comfort client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .payloads import DeviceUpdatePayload, ZoneResponse


@dataclass
class TokenInfo:
    """JWT token information with expiration tracking."""

    access: str
    refresh: str
    access_expires_at: datetime
    refresh_expires_at: datetime

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "TokenInfo":
        """Create TokenInfo from an auth response."""
        now = datetime.now(timezone.utc)
        return cls(
            access=data.get("access", ""),
            refresh=data.get("refresh", ""),
            access_expires_at=now + timedelta(minutes=18),
            refresh_expires_at=now + timedelta(days=25),
        )

    def is_access_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.access_expires_at

    def is_refresh_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.refresh_expires_at


@dataclass
class DeviceState:
    """Holds the latest known state for a single device."""

    VALID_FAN_SPEEDS = {
        "superQuiet",
        "quiet",
        "low",
        "powerful",
        "superPowerful",
        "auto",
    }
    VALID_AIR_DIRECTIONS = {
        "auto",
        "horizontal",
        "midhorizontal",
        "midpoint",
        "midvertical",
        "vertical",
        "swing",
    }

    serial: str
    name: str
    zone_id: str | None = None
    connected: bool = True
    room_temp: float | None = None
    sp_cool: float | None = None
    sp_heat: float | None = None
    humidity: float | None = None
    room_temp_offset: float | None = None
    operation_mode: str | None = None
    power: bool = False
    fan_speed: str | None = None
    air_direction: str | None = None
    schedule_owner: str | None = None
    last_status_change: str | None = None
    rssi: int | None = None
    two_figures_code: str | None = None
    serial_number: str | None = None
    model_number: str | None = None
    display_config: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.serial_number is None:
            self.serial_number = self.serial
        if self.model_number is None and self.raw:
            self.model_number = self.raw.get("modelNumber")

    def target_temperature(self) -> float | None:
        """Return the active target setpoint in Celsius."""
        if self.operation_mode == "cool":
            return self.sp_cool
        if self.operation_mode == "heat":
            return self.sp_heat
        if self.sp_cool is not None:
            return self.sp_cool
        return self.sp_heat

    def update_from_zone(self, zone: dict[str, Any]) -> None:
        """Update state from a zone payload that includes adapter data."""
        zone_payload = ZoneResponse.model_validate(zone)
        if zone_payload.id:
            self.zone_id = zone_payload.id
        zone_payload.apply_to_device(self)

    def apply_update(self, payload: dict[str, Any]) -> None:
        """Merge any payload into the device state."""
        device_payload = DeviceUpdatePayload.model_validate(payload)
        device_payload.apply_to_device(self)
