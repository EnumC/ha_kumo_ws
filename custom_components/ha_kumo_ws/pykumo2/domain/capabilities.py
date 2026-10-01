"""Unit capabilities derived from the indoor unit profile."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from ..util.coerce import as_float, as_int
from .enums import FanSpeed, HvacMode, VaneDirection

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_C = 16.0
DEFAULT_MAX_C = 31.0

_FIVE_SPEEDS = (
    FanSpeed.SUPER_QUIET,
    FanSpeed.QUIET,
    FanSpeed.LOW,
    FanSpeed.POWERFUL,
    FanSpeed.SUPER_POWERFUL,
)
_FOUR_SPEEDS = (FanSpeed.QUIET, FanSpeed.LOW, FanSpeed.POWERFUL, FanSpeed.SUPER_POWERFUL)
_VANES = (
    VaneDirection.HORIZONTAL,
    VaneDirection.MIDHORIZONTAL,
    VaneDirection.MIDPOINT,
    VaneDirection.MIDVERTICAL,
    VaneDirection.VERTICAL,
    VaneDirection.AUTO,
)


@dataclass(frozen=True, slots=True)
class SetpointRange:
    """Inclusive setpoint range in Celsius."""

    low: float = DEFAULT_MIN_C
    high: float = DEFAULT_MAX_C

    def clamp(self, value: float) -> float:
        return min(max(value, self.low), self.high)


@dataclass(frozen=True, slots=True)
class SetpointLimits:
    """Per-mode setpoint ranges."""

    heat: SetpointRange = SetpointRange()
    cool: SetpointRange = SetpointRange()
    auto: SetpointRange = SetpointRange()


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a unit supports; built from the same profile keys locally and in the cloud."""

    hvac_modes: frozenset[HvacMode]
    fan_speeds: tuple[FanSpeed, ...]
    vane_directions: tuple[VaneDirection, ...]
    setpoints: SetpointLimits = SetpointLimits()
    uses_setpoint_in_dry: bool = False
    has_defrost: bool = False
    has_standby: bool = False
    has_hot_adjust: bool = False
    number_of_fan_speeds: int | None = None
    raw_low_label: bool = False

    @property
    def has_vane(self) -> bool:
        return bool(self.vane_directions)

    @property
    def has_swing(self) -> bool:
        return VaneDirection.SWING in self.vane_directions

    def heat_range(self, mode: HvacMode | None) -> SetpointRange:
        """Range for spHeat in mode; auto also honors the heat ceiling."""
        if mode is HvacMode.AUTO:
            return _intersect(self.setpoints.auto, self.setpoints.heat.high, high=True)
        return self.setpoints.heat

    def cool_range(self, mode: HvacMode | None) -> SetpointRange:
        """Range for spCool in mode; auto also honors the cool floor."""
        if mode is HvacMode.AUTO:
            return _intersect(self.setpoints.auto, self.setpoints.cool.low, high=False)
        return self.setpoints.cool

    @classmethod
    def default(cls) -> Self:
        """Conservative fallback when no profile is known."""
        return cls(
            hvac_modes=frozenset({HvacMode.OFF, HvacMode.COOL, HvacMode.HEAT}),
            fan_speeds=(*_FIVE_SPEEDS, FanSpeed.AUTO),
            vane_directions=(),
        )

    @classmethod
    def from_profile(
        cls, profile: Mapping[str, Any], overlay: Mapping[str, Any] | None = None
    ) -> Self:
        """Build from a unit profile, masked by adapter/cloud user settings."""
        overlay = overlay or {}
        number_of_speeds = as_int(profile.get("numberOfFanSpeeds"))
        return cls(
            hvac_modes=_modes(profile, overlay),
            fan_speeds=_fan_speeds(profile, number_of_speeds),
            vane_directions=_vanes(profile),
            setpoints=_limits(profile, overlay),
            uses_setpoint_in_dry=bool(profile.get("usesSetPointInDryMode")),
            has_defrost=bool(profile.get("hasDefrost")),
            has_standby=bool(profile.get("hasStandby")),
            has_hot_adjust=bool(profile.get("hasHotAdjust")),
            number_of_fan_speeds=number_of_speeds,
            raw_low_label=number_of_speeds == 4,
        )


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _has_auto(profile: Mapping[str, Any], overlay: Mapping[str, Any]) -> bool:
    """Honor autoModePrevention, but trust auto setpoints in the profile (pykumo quirk)."""
    if not _first(overlay, "autoModePrevention", "autoModeDisable"):
        return True
    max_sp = profile.get("maximumSetPoints") or {}
    min_sp = profile.get("minimumSetPoints") or {}
    return "auto" in max_sp or "auto" in min_sp


_LOCAL_KEYS = ("userHasModeDry", "userHasModeHeat", "autoModePrevention")


def _user_allows(overlay: Mapping[str, Any], local_key: str, cloud_key: str) -> bool:
    """Local overlays need the flag set (pykumo parity); cloud ones mask only when false."""
    if any(key in overlay for key in _LOCAL_KEYS):
        return bool(overlay.get(local_key))
    value = overlay.get(cloud_key)
    return value is None or bool(value)


def _modes(profile: Mapping[str, Any], overlay: Mapping[str, Any]) -> frozenset[HvacMode]:
    modes = {HvacMode.OFF, HvacMode.COOL}
    if profile.get("hasModeDry") and _user_allows(overlay, "userHasModeDry", "modeDry"):
        modes.add(HvacMode.DRY)
    if profile.get("hasModeHeat") and _user_allows(overlay, "userHasModeHeat", "modeHeat"):
        modes.add(HvacMode.HEAT)
    if profile.get("hasModeVent"):
        modes.add(HvacMode.VENT)
    if _has_auto(profile, overlay):
        modes.add(HvacMode.AUTO)
    return frozenset(modes)


def _fan_speeds(profile: Mapping[str, Any], speeds: int | None) -> tuple[FanSpeed, ...]:
    if speeds is not None and speeds not in (3, 4, 5):
        _LOGGER.info("Unit reports %s fan speeds, expected 3, 4 or 5", speeds)
    # 3-speed units under-report; hardware accepts all five speeds.
    base = _FOUR_SPEEDS if speeds == 4 else _FIVE_SPEEDS
    return (*base, FanSpeed.AUTO) if profile.get("hasFanSpeedAuto") else base


def _vanes(profile: Mapping[str, Any]) -> tuple[VaneDirection, ...]:
    if not profile.get("hasVaneDir"):
        return ()
    return (*_VANES, VaneDirection.SWING) if profile.get("hasVaneSwing") else _VANES


def _range(profile: Mapping[str, Any], mode: str) -> SetpointRange:
    lows = profile.get("minimumSetPoints")
    highs = profile.get("maximumSetPoints")
    low = as_float(lows.get(mode)) if isinstance(lows, Mapping) else None
    high = as_float(highs.get(mode)) if isinstance(highs, Mapping) else None
    low = DEFAULT_MIN_C if low is None else low
    high = DEFAULT_MAX_C if high is None else high
    return SetpointRange(low, high) if low <= high else SetpointRange()


def _narrow(rng: SetpointRange, low: float | None, high: float | None) -> SetpointRange:
    new_low = max(rng.low, low) if low is not None else rng.low
    new_high = min(rng.high, high) if high is not None else rng.high
    return SetpointRange(new_low, new_high) if new_low <= new_high else rng


def _intersect(rng: SetpointRange, bound: float, *, high: bool) -> SetpointRange:
    return _narrow(rng, None, bound) if high else _narrow(rng, bound, None)


def _limits(profile: Mapping[str, Any], overlay: Mapping[str, Any]) -> SetpointLimits:
    user_min_cool = as_float(_first(overlay, "userMinCoolSetPoint", "minSetpoint", "minSetPoint"))
    user_max_heat = as_float(_first(overlay, "userMaxHeatSetPoint", "maxSetpoint", "maxSetPoint"))
    return SetpointLimits(
        heat=_narrow(_range(profile, "heat"), None, user_max_heat),
        cool=_narrow(_range(profile, "cool"), user_min_cool, None),
        auto=_range(profile, "auto"),
    )
