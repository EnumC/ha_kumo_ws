"""Credential persistence in a private HA Store."""

import hashlib
from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .pykumo2.clock import SystemClock
from .pykumo2.credentials.models import UnitCredentials
from .pykumo2.credentials.repository import StoredCredentials, from_json_dict, to_json_dict
from .pykumo2.credentials.service import CredentialService, Prober

STORAGE_VERSION = 1


def storage_key(unique_id: str) -> str:
    """Store key derived from the entry unique_id, so a flow can write it first."""
    digest = hashlib.sha1(unique_id.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{DOMAIN}.{digest}.credentials"


def _store(hass: HomeAssistant, unique_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, storage_key(unique_id), private=True)


class HaCredentialRepository:
    """CredentialRepository backed by homeassistant.helpers.storage.Store."""

    def __init__(self, hass: HomeAssistant, unique_id: str) -> None:
        self._store = _store(hass, unique_id)

    async def async_load(self) -> StoredCredentials:
        raw = await self._store.async_load()
        return StoredCredentials() if raw is None else from_json_dict(raw)

    async def async_save(self, data: StoredCredentials) -> None:
        await self._store.async_save(to_json_dict(data))


async def async_remove_store(hass: HomeAssistant, unique_id: str) -> None:
    """Delete the credential Store for unique_id."""
    await _store(hass, unique_id).async_remove()


async def async_save_units(
    hass: HomeAssistant, unique_id: str, units: Iterable[UnitCredentials]
) -> None:
    """Replace the Store contents with units (used by flows before the entry exists)."""
    data = StoredCredentials(units={unit.serial: unit for unit in units})
    await HaCredentialRepository(hass, unique_id).async_save(data)


async def async_load_service(
    hass: HomeAssistant, unique_id: str, prober: Prober | None = None
) -> CredentialService:
    """Store-backed CredentialService without a cloud provider, for unloaded entries."""
    service = CredentialService(
        HaCredentialRepository(hass, unique_id), provider=None, prober=prober, clock=SystemClock()
    )
    await service.async_load()
    return service
