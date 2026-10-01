"""Find adapters on the LAN, then match them to stored credentials."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
from collections.abc import Iterable, Mapping

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from ..credentials.models import UnitCredentials
from ..errors import LocalError
from .client import LocalUnitClient
from .const import DEVICE_AUTHENTICATION_ERROR

_LOGGER = logging.getLogger(__name__)

_STATUS_BODY = b'{"c":{"indoorUnit":{"status":{}}}}'
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
_MAX_ADDRESSES = 1024


def _expand_cidrs(cidrs: Iterable[str]) -> list[str]:
    """Hosts in each CIDR, in order. Broader than /22 raises ValueError."""
    hosts: list[str] = []
    seen: set[str] = set()
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        if network.prefixlen < 22 or network.num_addresses > _MAX_ADDRESSES:
            raise ValueError(f"CIDR broader than /22: {network}")
        for host in network.hosts():
            text = str(host)
            if text not in seen:
                seen.add(text)
                hosts.append(text)
    return hosts


def _http_host(ip: str, port: int) -> str:
    if ":" in ip:
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"


def _is_auth_challenge(raw: bytes) -> bool:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("_api_error") == DEVICE_AUTHENTICATION_ERROR


async def _port_open(ip: str, port: int, connect_timeout: float) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), connect_timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def fingerprint_scan(
    cidrs: Iterable[str],
    *,
    port: int = 80,
    concurrency: int = 32,
    connect_timeout: float = 1.0,
    request_timeout: float = 5.0,
) -> list[str]:
    """Return hosts that answer the unsigned status query with an auth error."""
    hosts = _expand_cidrs(cidrs)
    if not hosts:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))
    timeout = ClientTimeout(sock_connect=connect_timeout, sock_read=request_timeout)
    connector = aiohttp.TCPConnector(limit=max(1, concurrency))

    async with ClientSession(
        connector=connector, connector_owner=True, timeout=timeout, trust_env=False
    ) as session:

        async def check(ip: str) -> str | None:
            async with sem:
                if not await _port_open(ip, port, connect_timeout):
                    return None
                url = f"http://{_http_host(ip, port)}/api?m="
                try:
                    async with session.put(url, data=_STATUS_BODY, headers=_HEADERS) as resp:
                        raw = await resp.read()
                except (TimeoutError, aiohttp.ClientError, OSError):
                    return None
                return ip if _is_auth_challenge(raw) else None

        found = await asyncio.gather(*(check(ip) for ip in hosts))
    return [ip for ip in found if ip is not None]


async def signed_probe(
    address: str,
    creds: UnitCredentials,
    *,
    timeout: float = 3.0,  # noqa: ASYNC109
) -> bool:
    """True when a signed status read returns an object that has ``r``."""
    client = LocalUnitClient(address, creds, timeouts=(timeout, timeout))
    try:
        await client.request(_STATUS_BODY)
    except (LocalError, ValueError):
        return False
    finally:
        await client.aclose()
    return True


def _usable(creds: UnitCredentials) -> bool:
    if not creds.has_secrets:
        return False
    try:
        creds.validate()
    except ValueError:
        return False
    return True


async def match_units(
    ips: Iterable[str],
    creds: Mapping[str, UnitCredentials],
    *,
    concurrency: int = 16,
    timeout: float = 3.0,  # noqa: ASYNC109
) -> dict[str, str]:
    """Map serial to address. Each address is given to at most one serial."""
    addresses = list(dict.fromkeys(ip for ip in ips if ip))
    usable = {serial: unit for serial, unit in creds.items() if _usable(unit)}
    if not addresses or not usable:
        return {}
    serials = list(usable)
    claimed: set[str] = set()
    claimed_addresses: set[str] = set()
    sem = asyncio.Semaphore(max(1, concurrency))
    found: dict[str, str] = {}
    lock = asyncio.Lock()

    async def probe(address: str) -> None:
        for serial in serials:
            async with lock:
                if serial in claimed:
                    continue
            async with sem:
                accepted = await signed_probe(address, usable[serial], timeout=timeout)
            if not accepted:
                continue
            async with lock:
                if address in claimed_addresses:
                    return
                if serial in claimed:
                    continue
                claimed_addresses.add(address)
                claimed.add(serial)
                found[serial] = address
                return

    await asyncio.gather(*(probe(address) for address in addresses))
    missed = [serial for serial in serials if serial not in found]
    if missed:
        _LOGGER.info("unmatched serials=%s", ",".join(missed))
    return found
