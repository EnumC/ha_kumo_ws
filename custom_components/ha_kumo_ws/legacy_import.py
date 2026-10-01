"""Detect and read an existing hass-kumo / pykumo setup (read only)."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import load_json

from .pykumo2.clock import SystemClock
from .pykumo2.credentials import codec
from .pykumo2.credentials.models import UnitCredentials
from .pykumo2.credentials.repository import InMemoryCredentialRepository
from .pykumo2.credentials.service import CredentialService

_LOGGER = logging.getLogger(__name__)

KUMO_DOMAIN = "kumo"
KUMO_FILES = ("kumo_cache.json", "kumo.cfg")


@dataclass(slots=True)
class LegacySetup:
    """What an existing hass-kumo / pykumo install provides. Holds secrets; never log it."""

    files: list[str] = field(default_factory=list)
    units: list[UnitCredentials] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    entry: ConfigEntry | None = None
    entry_count: int = 0
    username: str | None = None
    password: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.files or self.entry is not None)

    @property
    def has_account(self) -> bool:
        return bool(self.username and self.password)


def mask_username(username: str) -> str:
    """a***@example.com"""
    name, sep, domain = username.partition("@")
    return f"{name[:1]}***{sep}{domain}"


def _read_files(config_dir: str) -> list[tuple[str, Any, float]]:
    """Executor job: (name, parsed JSON, mtime) for each existing file, oldest first."""
    found: list[tuple[str, Any, float]] = []
    for name in KUMO_FILES:
        path = os.path.join(config_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            data = load_json(path, None)
            mtime = os.path.getmtime(path)
        except (HomeAssistantError, OSError) as err:
            _LOGGER.warning("Cannot read %s: %s", name, type(err).__name__)
            continue
        if data is not None:
            found.append((name, data, mtime))
    return sorted(found, key=lambda item: item[2])


async def async_detect(hass: HomeAssistant) -> LegacySetup:
    """Read kumo_cache.json, kumo.cfg and the hass-kumo entry. Newest file wins on merge."""
    result = LegacySetup()
    files = await hass.async_add_executor_job(_read_files, hass.config.config_dir)
    merger = CredentialService(
        InMemoryCredentialRepository(), provider=None, prober=None, clock=SystemClock()
    )
    cfg_username: str | None = None
    for name, data, _mtime in files:
        result.files.append(name)
        decoded = codec.decode_data(data)
        result.errors.extend(decoded.errors)
        cfg_username = cfg_username or decoded.account_username
        if decoded.units:
            await merger.async_merge(decoded.units)
    result.units = sorted(merger.all().values(), key=lambda unit: unit.serial)
    entries = hass.config_entries.async_entries(KUMO_DOMAIN)
    result.entry_count = len(entries)
    entry = next((e for e in entries if e.disabled_by is None), entries[0] if entries else None)
    if entry is not None:
        result.entry = entry
        result.username = entry.data.get(CONF_USERNAME) or None
        result.password = entry.data.get(CONF_PASSWORD) or None
    result.username = result.username or cfg_username
    return result
