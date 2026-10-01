"""Sensor platform: unit diagnostics, humidity, wireless sensors and cloud usage."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfFrequency,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import new_device_signal
from .coordinator import KumoDeviceCoordinator
from .entity import KumoEntity, KumoHubEntity, KumoLocalEntity
from .hub import KumoConfigEntry, KumoHub
from .pykumo2.domain.enums import LinkState, TempSource
from .pykumo2.domain.state import Cn105Telemetry, WirelessSensor
from .pykumo2.local.cn105 import STAGE_NAMES, SUB_MODE_NAMES
from .pykumo2.transport import TransportKind

PARALLEL_UPDATES = 0
SCAN_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, kw_only=True)
class KumoSensorDescription(SensorEntityDescription):
    """Unit sensor description; dynamic ones appear on first value."""

    value_fn: Callable[[KumoDeviceCoordinator], StateType]
    dynamic: bool = False
    local_only: bool = False
    local_link: bool = False
    cn105_code: int | None = None


@dataclass(frozen=True, kw_only=True)
class WirelessSensorDescription(SensorEntityDescription):
    value_fn: Callable[[WirelessSensor], StateType]


@dataclass(frozen=True, kw_only=True)
class HubSensorDescription(SensorEntityDescription):
    value_fn: Callable[[KumoHub], StateType]


def _active_transport(c: KumoDeviceCoordinator) -> StateType:
    kind = c.link.active_transport
    return None if kind is None else kind.value


def _link_state(c: KumoDeviceCoordinator) -> StateType:
    return c.link.state.value


def _active_thermistor(c: KumoDeviceCoordinator) -> StateType:
    source = c.data.active_thermistor
    return None if source is None else source.value


SUB_MODES = [name.lower() for name in SUB_MODE_NAMES.values()]
STAGES = [name.lower() for name in STAGE_NAMES.values()]


def _cn105(c: KumoDeviceCoordinator) -> Cn105Telemetry:
    return c.data.cn105 or Cn105Telemetry()


def _enum(value: str | None, options: list[str]) -> str | None:
    lowered = None if value is None else value.lower()
    return lowered if lowered in options else None


UNIT_SENSORS: tuple[KumoSensorDescription, ...] = (
    KumoSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.wifi_rssi,
    ),
    KumoSensorDescription(
        key="two_figures_code",
        translation_key="two_figures_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.error_code,
    ),
    KumoSensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.effective_humidity,
        dynamic=True,
    ),
    KumoSensorDescription(
        key="active_transport",
        translation_key="active_transport",
        device_class=SensorDeviceClass.ENUM,
        options=[kind.value for kind in TransportKind],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_active_transport,
    ),
    KumoSensorDescription(
        key="link_state",
        translation_key="link_state",
        device_class=SensorDeviceClass.ENUM,
        options=[state.value for state in LinkState],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_link_state,
    ),
    KumoSensorDescription(
        key="active_thermistor",
        translation_key="active_thermistor",
        device_class=SensorDeviceClass.ENUM,
        options=[source.value for source in TempSource],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_active_thermistor,
        local_only=True,
        local_link=True,
    ),
    KumoSensorDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: _cn105(c).outdoor_temperature,
        local_only=True,
        cn105_code=3,
    ),
    KumoSensorDescription(
        key="compressor_runtime",
        translation_key="compressor_runtime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _cn105(c).compressor_runtime_minutes,
        local_only=True,
        cn105_code=3,
    ),
    KumoSensorDescription(
        key="cn105_room_temperature",
        translation_key="cn105_room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _cn105(c).room_temperature,
        local_only=True,
        cn105_code=3,
    ),
    KumoSensorDescription(
        key="sub_mode",
        translation_key="sub_mode",
        device_class=SensorDeviceClass.ENUM,
        options=SUB_MODES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _enum(_cn105(c).sub_mode, SUB_MODES),
        local_only=True,
        cn105_code=9,
    ),
    KumoSensorDescription(
        key="fan_stage",
        translation_key="fan_stage",
        device_class=SensorDeviceClass.ENUM,
        options=STAGES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _enum(_cn105(c).stage, STAGES),
        local_only=True,
        cn105_code=9,
    ),
    KumoSensorDescription(
        key="compressor_frequency",
        translation_key="compressor_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _cn105(c).compressor_frequency,
        local_only=True,
        cn105_code=6,
        dynamic=True,
    ),
)

WIRELESS_SENSORS: tuple[WirelessSensorDescription, ...] = (
    WirelessSensorDescription(
        key="temperature",
        translation_key="sensor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.temperature,
    ),
    WirelessSensorDescription(
        key="battery",
        translation_key="sensor_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery,
    ),
    WirelessSensorDescription(
        key="rssi",
        translation_key="sensor_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.rssi,
    ),
)

HUB_SENSORS: tuple[HubSensorDescription, ...] = (
    HubSensorDescription(
        key="cloud_calls_last_hour",
        translation_key="cloud_calls_last_hour",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: hub.ledger.count_last_hour(),
    ),
    HubSensorDescription(
        key="cloud_rate_limit_remaining",
        translation_key="cloud_rate_limit_remaining",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: hub.ledger.remaining,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add unit, wireless and hub sensors; dynamic ones as values appear."""
    hub = entry.runtime_data
    if hub.has_cloud:
        async_add_entities(KumoHubSensor(hub, description) for description in HUB_SENSORS)

    @callback
    def _setup(coordinator: KumoDeviceCoordinator) -> None:
        added: set[str] = set()

        @callback
        def _discover() -> None:
            new: list[SensorEntity] = []
            for description in UNIT_SENSORS:
                if description.key in added or (description.local_only and not hub.local_capable):
                    continue
                if description.cn105_code is not None and not (
                    hub.cn105_enabled and description.cn105_code in hub.cn105_codes
                ):
                    continue
                if description.dynamic and description.value_fn(coordinator) is None:
                    continue
                added.add(description.key)
                cls = KumoLocalSensor if description.local_link else KumoSensor
                new.append(cls(coordinator, description))
            for sensor in coordinator.data.sensors:
                if not sensor.uuid or f"sensor_{sensor.uuid}" in added:
                    continue
                added.add(f"sensor_{sensor.uuid}")
                new.extend(
                    KumoWirelessSensor(coordinator, sensor, description)
                    for description in WIRELESS_SENSORS
                )
            if new:
                async_add_entities(new)

        _discover()
        entry.async_on_unload(coordinator.async_add_listener(_discover))

    for coordinator in hub.coordinators.values():
        _setup(coordinator)

    @callback
    def _on_new_device(serial: str) -> None:
        _setup(hub.coordinators[serial])

    entry.async_on_unload(
        async_dispatcher_connect(hass, new_device_signal(entry.entry_id), _on_new_device)
    )


class KumoSensor(KumoEntity, SensorEntity):
    """Value read from the unit's state or link."""

    entity_description: KumoSensorDescription

    def __init__(
        self, coordinator: KumoDeviceCoordinator, description: KumoSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator)


class KumoLocalSensor(KumoLocalEntity, KumoSensor):
    """Unit sensor that is only available on a local link."""


class KumoWirelessSensor(KumoEntity, SensorEntity):
    """One reading of a paired wireless sensor, tracked by uuid."""

    entity_description: WirelessSensorDescription

    def __init__(
        self,
        coordinator: KumoDeviceCoordinator,
        sensor: WirelessSensor,
        description: WirelessSensorDescription,
    ) -> None:
        assert sensor.uuid is not None
        super().__init__(coordinator, f"sensor_{sensor.uuid}_{description.key}")
        self.entity_description = description
        self._uuid = sensor.uuid
        self._attr_translation_placeholders = {"index": str(sensor.index + 1)}

    def _sensor(self) -> WirelessSensor | None:
        return next((s for s in self.coordinator.data.sensors if s.uuid == self._uuid), None)

    @property
    def available(self) -> bool:
        return super().available and self._sensor() is not None

    @property
    def native_value(self) -> StateType:
        sensor = self._sensor()
        return None if sensor is None else self.entity_description.value_fn(sensor)


class KumoHubSensor(KumoHubEntity, SensorEntity):
    """Cloud call accounting, updated on every recorded call."""

    entity_description: HubSensorDescription
    _attr_should_poll = True

    def __init__(self, hub: KumoHub, description: HubSensorDescription) -> None:
        super().__init__(hub, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hub.ledger.add_listener(self._on_ledger))

    @callback
    def _on_ledger(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.hub)
