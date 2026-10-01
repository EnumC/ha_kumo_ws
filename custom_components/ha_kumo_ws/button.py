"""Button platform: adapter reboot and credential refresh."""

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import KumoDeviceCoordinator
from .entity import KumoHubEntity, KumoLocalEntity, async_setup_units
from .hub import KumoConfigEntry, KumoHub
from .pykumo2.domain.commands import RebootAdapter

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KumoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add local-only buttons; the credential refresh needs a cloud login too."""
    hub = entry.runtime_data
    if not hub.local_capable:
        return
    async_setup_units(hass, entry, async_add_entities, lambda c: [KumoRebootButton(c)])
    if hub.has_cloud and hub.credentials is not None:
        async_add_entities([KumoRefreshCredentialsButton(hub)])


class KumoRebootButton(KumoLocalEntity, ButtonEntity):
    """Reboot the Wi-Fi adapter."""

    _attr_translation_key = "reboot_adapter"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: KumoDeviceCoordinator) -> None:
        super().__init__(coordinator, "reboot_adapter")

    async def async_press(self) -> None:
        await self.coordinator.async_execute(RebootAdapter())


class KumoRefreshCredentialsButton(KumoHubEntity, ButtonEntity):
    """Fetch the local credentials of every unit from Kumo Cloud again."""

    _attr_translation_key = "refresh_credentials"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: KumoHub) -> None:
        super().__init__(hub, "refresh_credentials")

    async def async_press(self) -> None:
        service = self.hub.credentials
        assert service is not None
        result = await self.hub.async_create_task(
            service.async_request_refresh(list(self.hub.devices), reason="manual", force=True),
            "credential refresh",
        )
        if result.error or result.missing:
            _LOGGER.warning("Credential refresh failed: %s", result.error or sorted(result.missing))
        if result.error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="refresh_credentials_failed",
                translation_placeholders={"error": result.error},
            )
