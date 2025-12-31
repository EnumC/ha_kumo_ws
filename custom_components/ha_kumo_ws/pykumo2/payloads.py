"""Typed payloads for Kumo Cloud responses."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .models import DeviceState

_LOGGER = logging.getLogger(__name__)


class KumoBaseModel(BaseModel):
    """Base model that preserves unknown fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def model_post_init(self, __context: Any) -> None:
        extras = getattr(self, "__pydantic_extra__", None) or {}
        if extras:
            _LOGGER.warning(
                "Unexpected keys for %s: %s",
                self.__class__.__name__,
                sorted(extras.keys()),
            )

    def raw_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    def store_raw(self, device: "DeviceState", key: str | None = None) -> None:
        payload = self.raw_payload()
        if key:
            device.raw[key] = payload
        else:
            device.raw.update(payload)


class DisplayConfigPayload(KumoBaseModel):
    filter: bool | None = None
    defrost: bool | None = None
    hotAdjust: bool | None = None
    standby: bool | None = None


class DeviceStatePayload(KumoBaseModel):
    deviceSerial: str | None = None
    rssi: int | None = None
    power: int | None = None
    operationMode: str | None = None
    humidity: float | None = None
    scheduleOwner: str | None = None
    fanSpeed: str | None = None
    airDirection: str | None = None
    roomTemp: float | None = None
    twoFiguresCode: str | None = None
    spCool: float | None = None
    spHeat: float | None = None
    spAuto: float | None = None
    serialNumber: str | None = None
    modelNumber: str | None = None
    connected: bool | None = None
    displayConfig: DisplayConfigPayload | None = None

    def apply_to_device(self, device: "DeviceState") -> None:
        if self.roomTemp is not None:
            device.room_temp = self.roomTemp
        if self.spCool is not None:
            device.sp_cool = self.spCool
        if self.spHeat is not None:
            device.sp_heat = self.spHeat
        if self.humidity is not None:
            device.humidity = self.humidity
        if self.operationMode is not None:
            device.operation_mode = self.operationMode
        if self.power is not None:
            device.power = bool(self.power)
        if self.fanSpeed is not None and self.fanSpeed in device.VALID_FAN_SPEEDS:
            device.fan_speed = self.fanSpeed
        if self.airDirection is not None and self.airDirection in device.VALID_AIR_DIRECTIONS:
            device.air_direction = self.airDirection
        if self.scheduleOwner is not None:
            device.schedule_owner = self.scheduleOwner
        if self.rssi is not None:
            device.rssi = self.rssi
        if self.twoFiguresCode is not None:
            device.two_figures_code = self.twoFiguresCode
        if self.serialNumber is not None:
            device.serial_number = self.serialNumber
        if self.modelNumber is not None:
            device.model_number = self.modelNumber
        if self.connected is not None:
            device.connected = bool(self.connected)
        if self.displayConfig is not None:
            device.display_config = self.displayConfig.raw_payload()
        self.store_raw(device)


class DeviceUpdatePayload(DeviceStatePayload):
    id: str | None = None
    scheduleHoldEndTime: int | None = None
    unusualFigures: int | None = None
    statusDisplay: int | None = None
    runTest: int | None = None
    activeThermistor: str | None = None
    tempSource: str | None = None
    isSimulator: bool | None = None
    ledDisabled: bool | None = None
    isHeadless: bool | None = None
    previousOperationMode: str | None = None
    lastStatusChangeAt: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    timeZone: str | None = None
    collectMethod: str | None = None
    realValues: dict[str, Any] | None = None
    date: str | None = None


class DeviceStatusV2Payload(KumoBaseModel):
    deviceSerial: str | None = None
    status: str | None = None
    lastTimeConnected: str | None = None
    serverId: str | None = None
    hasIduCommunicationError: str | bool | None = None
    lastDisconnectedReason: str | None = None
    lastTimeDisconnected: str | None = None
    date: str | None = None

    def apply_to_device(self, device: "DeviceState") -> None:
        if self.status is not None:
            device.connected = self.status == "connected"
        if self.lastTimeConnected is not None:
            device.raw["lastTimeConnected"] = self.lastTimeConnected
        if self.lastTimeDisconnected is not None:
            device.raw["lastTimeDisconnected"] = self.lastTimeDisconnected
        if self.lastDisconnectedReason is not None:
            device.raw["lastDisconnectedReason"] = self.lastDisconnectedReason
        self.store_raw(device, "device_status_v2")


class ProfileUpdatePayload(KumoBaseModel):
    hasModeDry: bool | None = None
    hasModeHeat: bool | None = None
    hasVaneDir: bool | None = None
    hasVaneSwing: bool | None = None
    hasModeVent: bool | None = None
    hasFanSpeedAuto: bool | None = None
    hasInitialSettings: bool | None = None
    hasModeTest: bool | None = None
    numberOfFanSpeeds: int | None = None
    extendedTemps: bool | None = None
    usesSetPointInDryMode: bool | None = None
    hasHotAdjust: bool | None = None
    hasDefrost: bool | None = None
    hasStandby: bool | None = None
    maximumSetPoints: dict[str, Any] | None = None
    minimumSetPoints: dict[str, Any] | None = None
    deviceSerial: str | None = None
    date: str | None = None

    def apply_to_device(self, device: "DeviceState") -> None:
        self.store_raw(device, "profile_update")


class AdapterUpdatePayload(KumoBaseModel):
    id: str | None = None
    deviceSerial: str | None = None
    zoneName: str | None = None
    autoModeDisable: bool | None = None
    firmwareVersion: str | None = None
    roomTempDisplayOffset: float | None = None
    routerSsid: str | None = None
    routerRssi: int | None = None
    optimalStart: str | None = None
    minSetpoint: float | None = None
    maxSetpoint: float | None = None
    password: str | None = None
    modeHeat: bool | None = None
    modeDry: bool | None = None
    lastUpdated: str | None = None
    receiverRelay: str | None = None
    date: str | None = None

    def apply_to_device(self, device: "DeviceState") -> None:
        if self.roomTempDisplayOffset is not None:
            device.room_temp_offset = self.roomTempDisplayOffset
        self.store_raw(device, "adapter_update")


class AcoilUpdatePayload(KumoBaseModel):
    deviceSerial: str | None = None
    date: str | None = None

    def apply_to_device(self, device: "DeviceState") -> None:
        self.store_raw(device, "acoil_update")


class DeviceStatusResponse(KumoBaseModel):
    autoModeDisable: bool | None = None
    firmwareVersion: str | None = None
    roomTempDisplayOffset: float | None = None
    routerSsid: str | None = None
    routerRssi: int | None = None
    optimalStart: str | None = None
    modeHeat: bool | None = None
    modeDry: bool | None = None
    receiverRelay: str | None = None
    lastUpdated: str | None = None
    cryptoSerial: str | None = None
    cryptoKeySet: str | None = None

    def apply_to_device(self, device: "DeviceState") -> None:
        if self.roomTempDisplayOffset is not None:
            device.room_temp_offset = self.roomTempDisplayOffset
        self.store_raw(device, "device_status")


class GalleryInfo(KumoBaseModel):
    id: str | None = None
    name: str | None = None
    imageUrl: str | None = None
    imageAlt: str | None = None


class DeviceModelInfo(KumoBaseModel):
    id: str | None = None
    brand: str | None = None
    material: str | None = None
    basicMaterial: str | None = None
    replacementMaterial: str | None = None
    materialDescription: str | None = None
    family: str | None = None
    subFamily: str | None = None
    materialGroupName: str | None = None
    serialProfile: str | None = None
    materialGroupSeries: str | None = None
    isIndoorUnit: bool | None = None
    isDuctless: bool | None = None
    isSwing: bool | None = None
    isPowerfulMode: bool | None = None
    modeDescription: str | None = None
    isActive: bool | None = None
    frontendAnimation: str | None = None
    gallery: GalleryInfo | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


class DeviceDetailsResponse(DeviceUpdatePayload):
    model_info: DeviceModelInfo | None = Field(default=None, alias="model")

    def apply_to_device(self, device: "DeviceState") -> None:
        super().apply_to_device(device)
        self.store_raw(device, "device_details")


class ZoneGroupPayload(KumoBaseModel):
    id: str | None = None
    name: str | None = None
    isActive: bool | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    masterZone: str | None = None
    systemChangeoverEnabled: bool | None = None
    minRuntime: int | None = None
    maxStandby: int | None = None


class ZoneAdapterPayload(DeviceStatePayload):
    id: str | None = None
    isSimulator: bool | None = None
    scheduleHoldEndTime: int | None = None
    previousOperationMode: str | None = None
    hasSensor: bool | None = None
    hasMhk2: bool | None = None
    timeZone: str | None = None
    isHeadless: bool | None = None
    lastStatusChangeAt: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


class ZoneResponse(KumoBaseModel):
    id: str | None = None
    name: str | None = None
    isActive: bool | None = None
    adapter: ZoneAdapterPayload | None = None
    group: ZoneGroupPayload | None = None
    createdAt: str | None = None
    updatedAt: str | None = None

    def apply_to_device(self, device: DeviceState) -> None:
        if self.name:
            device.name = self.name
        if self.adapter is not None:
            self.adapter.apply_to_device(device)
        self.store_raw(device, "zone")
