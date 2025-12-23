"""Lightweight models used by the Mitsubishi Comfort client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


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
        self.zone_id = zone.get("id", self.zone_id)
        adapter = zone.get("adapter", {})
        self.name = zone.get("name", self.name)
        self.apply_update(adapter)

    def apply_update(self, payload: dict[str, Any]) -> None:
        """Merge any payload into the device state."""
        if "name" in payload:
            self.name = payload["name"]
        if "roomTemp" in payload:
            self.room_temp = payload.get("roomTemp")
        if "spCool" in payload:
            self.sp_cool = payload.get("spCool")
        if "spHeat" in payload:
            self.sp_heat = payload.get("spHeat")
        if "humidity" in payload:
            self.humidity = payload.get("humidity")
        if "operationMode" in payload or "mode" in payload:
            self.operation_mode = payload.get("operationMode") or payload.get("mode")
        if "power" in payload:
            self.power = bool(payload.get("power"))
        if "fanSpeed" in payload:
            fan = payload.get("fanSpeed")
            if fan in self.VALID_FAN_SPEEDS:
                self.fan_speed = fan
        if "airDirection" in payload:
            direction = payload.get("airDirection")
            if direction in self.VALID_AIR_DIRECTIONS:
                self.air_direction = direction
        if "scheduleOwner" in payload:
            self.schedule_owner = payload.get("scheduleOwner")
        if "lastStatusChangeAt" in payload:
            self.last_status_change = payload.get("lastStatusChangeAt")
        if "rssi" in payload:
            self.rssi = payload.get("rssi")
        if "twoFiguresCode" in payload:
            self.two_figures_code = payload.get("twoFiguresCode")
        if "serialNumber" in payload:
            self.serial_number = payload.get("serialNumber")
        if "modelNumber" in payload or "model" in payload or "modelName" in payload:
            self.model_number = payload.get("modelNumber") or payload.get("model") or payload.get("modelName")
        if "connected" in payload:
            self.connected = bool(payload.get("connected"))
        if "status" in payload:
            # device_status_v2 uses status to indicate connectivity
            self.connected = payload.get("status") == "connected"
        if "lastTimeConnected" in payload:
            self.raw["lastTimeConnected"] = payload.get("lastTimeConnected")
        if "lastTimeDisconnected" in payload:
            self.raw["lastTimeDisconnected"] = payload.get("lastTimeDisconnected")
        if "lastDisconnectedReason" in payload:
            self.raw["lastDisconnectedReason"] = payload.get("lastDisconnectedReason")
        if "displayConfig" in payload and isinstance(payload.get("displayConfig"), dict):
            self.display_config = payload.get("displayConfig", {})

        # Keep a merged raw copy for debugging
        self.raw.update(payload)
