"""Constants for the Mitsubishi Comfort integration."""

from typing import Any, Final

from homeassistant.const import Platform

DOMAIN: Final = "ha_kumo_ws"
MANUFACTURER: Final = "Mitsubishi Electric"
UNIQUE_ID_PREFIX: Final = "mitsubishi_comfort"

PLATFORMS: Final[list[Platform]] = [Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER]

CONF_SETUP_METHOD: Final = "setup_method"
CONF_SITE_IDS: Final = "site_ids"

CONF_CONNECTION_MODE: Final = "connection_mode"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_SOCKET_IDLE_DISCONNECT: Final = "socket_idle_disconnect"
CONF_REFRESH_ON_CONNECT: Final = "refresh_on_connect"
CONF_CIDRS: Final = "cidrs"
CONF_IP_OVERRIDES: Final = "ip_overrides"
CONF_CN105_ENABLED: Final = "cn105_enabled"
CONF_CN105_CODES: Final = "cn105_codes"
CONF_CN105_INTERVAL: Final = "cn105_interval"
CONF_REMOTE_TEMP: Final = "remote_temp"
CONF_LOCAL_ROOM_TEMP_OFFSET: Final = "local_room_temp_offset"
CONF_TARGET_TEMP_STEP: Final = "target_temp_step"
TARGET_TEMP_STEPS: Final = ("auto", "half", "whole")

CONF_RT_ENTITY: Final = "entity_id"
CONF_RT_INTERVAL: Final = "interval"
CONF_RT_MANAGE_SOURCE: Final = "manage_source"
DEFAULT_RT_INTERVAL: Final = 20

CONF_FAN_PARK: Final = "fan_park"
CONF_FP_DEFAULTS: Final = "fan_park_defaults"
CONF_FP_USE_DEFAULTS: Final = "use_defaults"
CONF_FP_SMART_FAN: Final = "smart_fan"
CONF_FP_FULL_AT: Final = "full_speed_at"
CONF_FP_HYSTERESIS: Final = "fan_hysteresis"
CONF_FP_FAN_HOLD: Final = "fan_hold"
CONF_FP_ENABLED: Final = "enabled"
CONF_FP_OFF_MARGIN: Final = "off_margin"
CONF_FP_MARGIN: Final = "restart_margin"
CONF_FP_DWELL: Final = "dwell"
CONF_FP_IDLE_DWELL: Final = "idle_dwell"
CONF_FP_MIN_ON: Final = "min_on"
CONF_FP_MIN_OFF: Final = "min_off"
FP_SECTIONS: Final[dict[str, tuple[str, ...]]] = {
    "fan": (CONF_FP_SMART_FAN, CONF_FP_FULL_AT, CONF_FP_HYSTERESIS, CONF_FP_FAN_HOLD),
    "auto_off": (
        CONF_FP_ENABLED,
        CONF_FP_OFF_MARGIN,
        CONF_FP_MARGIN,
        CONF_FP_DWELL,
        CONF_FP_IDLE_DWELL,
        CONF_FP_MIN_ON,
        CONF_FP_MIN_OFF,
    ),
}
# (default, min, max): temperature deltas in C, durations in seconds.
FP_LIMITS: Final[dict[str, tuple[float, float, float]]] = {
    CONF_FP_FULL_AT: (2.0, 1.0, 5.0),
    CONF_FP_HYSTERESIS: (0.25, 0.0, 1.0),
    CONF_FP_FAN_HOLD: (120, 30, 900),
    CONF_FP_OFF_MARGIN: (0.5, 0.0, 2.0),
    CONF_FP_MARGIN: (1.0, 0.5, 3.0),
    CONF_FP_DWELL: (180, 60, 1800),
    CONF_FP_IDLE_DWELL: (60, 0, 600),
    CONF_FP_MIN_ON: (600, 0, 3600),
    CONF_FP_MIN_OFF: (300, 180, 3600),
}
FP_TEMPERATURES: Final = frozenset(
    {CONF_FP_FULL_AT, CONF_FP_HYSTERESIS, CONF_FP_OFF_MARGIN, CONF_FP_MARGIN}
)
FP_MINUTES: Final = frozenset({CONF_FP_DWELL, CONF_FP_IDLE_DWELL, CONF_FP_MIN_ON, CONF_FP_MIN_OFF})

DEFAULT_POLL_INTERVAL: Final = 30
MIN_POLL_INTERVAL: Final = 15
MAX_POLL_INTERVAL: Final = 300
DEFAULT_SOCKET_IDLE_DISCONNECT: Final = 600
DEFAULT_CN105_CODES: Final = [3, 9]
DEFAULT_CN105_INTERVAL: Final = 90
MIN_CN105_INTERVAL: Final = 75
CN105_CODES: Final = (3, 9, 6)
CN105_ENTITY_CODES: Final[dict[str, frozenset[int]]] = {
    "outdoor_temperature": frozenset({3}),
    "compressor_runtime": frozenset({3}),
    "cn105_room_temperature": frozenset({3}),
    "sub_mode": frozenset({9}),
    "fan_stage": frozenset({9}),
    "compressor_frequency": frozenset({6}),
    "compressor_running": frozenset({6}),
}
CREDENTIAL_FETCH_TIMEOUT_S: Final = 60.0

DEFAULT_OPTIONS: Final[dict[str, Any]] = {
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
    CONF_SOCKET_IDLE_DISCONNECT: DEFAULT_SOCKET_IDLE_DISCONNECT,
    CONF_REFRESH_ON_CONNECT: True,
    CONF_CIDRS: [],
    CONF_IP_OVERRIDES: {},
    CONF_CN105_ENABLED: False,
    CONF_CN105_CODES: DEFAULT_CN105_CODES,
    CONF_CN105_INTERVAL: DEFAULT_CN105_INTERVAL,
    CONF_REMOTE_TEMP: {},
    CONF_FAN_PARK: {},
    CONF_FP_DEFAULTS: {},
    CONF_LOCAL_ROOM_TEMP_OFFSET: False,
    CONF_TARGET_TEMP_STEP: "auto",
}

DEFAULT_MODE_BY_METHOD: Final = {
    "local_backup": "local_only",
    "cloud_ws": "cloud_only",
    "cloud_fetch": "auto",
}

HOLD_TTL_LOCAL: Final = 12.0
HOLD_TTL_CLOUD: Final = 10.0
POST_WRITE_REFRESH_S: Final = 8.0
CLOUD_BACKSTOP_TICK_S: Final = 60
INVENTORY_REFRESH_S: Final = 86400

SIGNAL_NEW_DEVICE: Final = "{domain}_{entry_id}_new_device"


def new_device_signal(entry_id: str) -> str:
    """Dispatcher signal fired with a serial when a device is added."""
    return SIGNAL_NEW_DEVICE.format(domain=DOMAIN, entry_id=entry_id)
