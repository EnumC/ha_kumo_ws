"""Fetch unit credentials from the cloud through injected sources."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..clock import Clock
from ..errors import AuthenticationError, CloudError, RateLimitedError
from .models import Secret, UnitCredentials

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CloudUnitInfo:
    """Non-secret unit info from the cloud account."""

    serial: str
    label: str = ""
    mac: str = ""
    unit_type: str = "ductless"
    site_id: str = ""


class CloudInventory(Protocol):
    async def async_list_units(self) -> list[CloudUnitInfo]: ...


class CryptoSerialSource(Protocol):
    async def async_crypto_serial(self, serial: str) -> str | None: ...


class PasswordSource(Protocol):
    async def async_passwords(
        self,
        serials: Sequence[str],
        timeout: float,  # noqa: ASYNC109
    ) -> Mapping[str, str]: ...


@dataclass(slots=True)
class FetchResult:
    """Fetched units and the reason each other serial is missing."""

    units: list[UnitCredentials] = field(default_factory=list)
    missing: dict[str, str] = field(default_factory=dict)


class CloudCredentialProvider:
    """Assemble credentials from inventory, crypto serials and passwords."""

    def __init__(
        self,
        inventory: CloudInventory,
        crypto: CryptoSerialSource,
        passwords: PasswordSource,
        clock: Clock,
    ) -> None:
        self._inventory = inventory
        self._crypto = crypto
        self._passwords = passwords
        self._clock = clock

    async def fetch(
        self,
        serials: Sequence[str] | None = None,
        timeout: float = 60.0,  # noqa: ASYNC109
    ) -> FetchResult:
        """Fetch credentials for serials (all when None). Partial results are kept."""
        result = FetchResult()
        infos = {info.serial: info for info in await self._inventory.async_list_units()}
        wanted = list(infos) if serials is None else list(serials)
        crypto_by_serial: dict[str, str] = {}
        for serial in wanted:
            if serial not in infos:
                result.missing[serial] = "not_in_account"
                continue
            try:
                crypto = await self._crypto.async_crypto_serial(serial)
            except (AuthenticationError, RateLimitedError):
                raise
            except CloudError:
                result.missing[serial] = "crypto_error"
                continue
            if crypto:
                crypto_by_serial[serial] = crypto
            else:
                result.missing[serial] = "no_crypto_serial"
        if not crypto_by_serial:
            return result
        passwords = await self._passwords.async_passwords(list(crypto_by_serial), timeout)
        now = self._clock.now()
        for serial, crypto in crypto_by_serial.items():
            password = passwords.get(serial)
            if not password:
                result.missing[serial] = "no_password"
                continue
            info = infos[serial]
            result.units.append(
                UnitCredentials(
                    serial=serial,
                    password_b64=Secret(password),
                    crypto_serial_hex=Secret(crypto),
                    label=info.label,
                    mac=info.mac,
                    unit_type=info.unit_type,
                    source="cloud",
                    updated_at=now,
                )
            )
        _LOGGER.debug("cloud fetch: %d units, %d missing", len(result.units), len(result.missing))
        return result
