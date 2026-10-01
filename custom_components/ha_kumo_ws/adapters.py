"""Adapters from pykumo2 I/O classes to the credential and routing ports."""

import asyncio
import ipaddress
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .pykumo2.cloud.rest import CloudRestClient
from .pykumo2.cloud.socket import CloudSocketSession
from .pykumo2.credentials.models import UnitCredentials
from .pykumo2.credentials.provider import CloudUnitInfo
from .pykumo2.credentials.service import CredentialService
from .pykumo2.local import discovery
from .pykumo2.util.coerce import as_str

_LOGGER = logging.getLogger(__name__)

MAX_PREFIX_SCAN = 24


@dataclass(frozen=True, slots=True)
class ZoneRecord:
    """One /zones item with the site it came from."""

    site_id: str
    zone: Mapping[str, Any]

    @property
    def adapter(self) -> Mapping[str, Any]:
        adapter = self.zone.get("adapter")
        return adapter if isinstance(adapter, Mapping) else {}

    @property
    def serial(self) -> str | None:
        return as_str(self.adapter.get("deviceSerial"))

    @property
    def mac(self) -> str:
        return as_str(self.adapter.get("macAddress")) or as_str(self.adapter.get("mac")) or ""

    def unit_info(self) -> CloudUnitInfo | None:
        serial = self.serial
        if serial is None:
            return None
        return CloudUnitInfo(
            serial=serial,
            label=as_str(self.zone.get("name")) or "",
            mac=self.mac,
            unit_type=as_str(self.adapter.get("unitType")) or "ductless",
            site_id=self.site_id,
        )


class CloudInventory:
    """Sites (optionally filtered) then zones; also the provider's CloudInventory."""

    def __init__(self, rest: CloudRestClient, site_ids: Sequence[str]) -> None:
        self._rest = rest
        self._site_ids = set(site_ids)

    async def async_zones(self) -> list[ZoneRecord]:
        records: list[ZoneRecord] = []
        for site in await self._rest.get_sites():
            site_id = str(site.get("id", ""))
            if not site_id or (self._site_ids and site_id not in self._site_ids):
                continue
            records.extend(ZoneRecord(site_id, z) for z in await self._rest.get_zones(site_id))
        return records

    async def async_list_units(self) -> list[CloudUnitInfo]:
        return [info for rec in await self.async_zones() if (info := rec.unit_info())]


class CryptoSerialSource:
    """cryptoSerial from /devices/{serial}/status."""

    def __init__(self, rest: CloudRestClient) -> None:
        self._rest = rest

    async def async_crypto_serial(self, serial: str) -> str | None:
        return as_str((await self._rest.get_device_status(serial)).get("cryptoSerial"))


class PasswordSource:
    """Adapter passwords from socket adapter_update events."""

    def __init__(self, socket: CloudSocketSession) -> None:
        self._socket = socket

    async def async_passwords(
        self,
        serials: Sequence[str],
        timeout: float,  # noqa: ASYNC109
    ) -> Mapping[str, str]:
        return await self._socket.request_adapter_status(serials, timeout)


class SignedProber:
    """credentials.service.Prober over discovery.signed_probe."""

    async def probe(self, creds: UnitCredentials) -> bool:
        if not creds.address:
            return False
        return await discovery.signed_probe(creds.address, creds)


class SubnetScanner:
    """routing.ports.Scanner: fingerprint scan, then signed matching."""

    def __init__(self, credentials: CredentialService) -> None:
        self._credentials = credentials

    async def scan(self, cidrs: Sequence[str]) -> list[str]:
        return await discovery.fingerprint_scan(cidrs)

    async def match(self, ips: Sequence[str], serials: Sequence[str]) -> dict[str, str]:
        units = {s: unit for s in serials if (unit := self._credentials.get(s)) is not None}
        return await discovery.match_units(ips, units)


async def async_default_cidrs(hass: HomeAssistant) -> list[str]:
    """IPv4 networks of enabled HA adapters, narrowed to at most a /24 each."""
    cidrs: list[str] = []
    for adapter in await network.async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for ipv4 in adapter["ipv4"]:
            address = ipaddress.IPv4Address(ipv4["address"])
            if address.is_loopback or address.is_link_local:
                continue
            prefix = max(ipv4["network_prefix"], MAX_PREFIX_SCAN)
            cidr = str(ipaddress.IPv4Network(f"{address}/{prefix}", strict=False))
            if cidr not in cidrs:
                cidrs.append(cidr)
    return cidrs


async def async_discover_addresses(
    hass: HomeAssistant, units: Mapping[str, UnitCredentials], cidrs: Sequence[str]
) -> dict[str, str]:
    """Verified address per serial: signed probe of known addresses, then a subnet scan."""
    known = [unit for unit in units.values() if unit.address and unit.has_secrets]
    results = await asyncio.gather(*(discovery.signed_probe(u.address, u) for u in known))
    found = {unit.serial: unit.address for unit, ok in zip(known, results, strict=True) if ok}
    lost = {s: unit for s, unit in units.items() if s not in found and unit.has_secrets}
    if not lost:
        return found
    try:
        ips = await discovery.fingerprint_scan(list(cidrs) or await async_default_cidrs(hass))
        taken = set(found.values())
        found.update(await discovery.match_units([ip for ip in ips if ip not in taken], lost))
    except (OSError, ValueError) as err:
        _LOGGER.warning("Unit discovery failed: %s", err)
    return found
