"""Dump adapter passwords for every unit on the account (masked unless --reveal)."""

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ha_kumo_ws.pykumo2.clock import SystemClock
from custom_components.ha_kumo_ws.pykumo2.cloud.budget import CloudCallLedger
from custom_components.ha_kumo_ws.pykumo2.cloud.rest import (
    CloudRestClient,
    default_client_factory,
)
from custom_components.ha_kumo_ws.pykumo2.cloud.socket import CloudSocketSession
from custom_components.ha_kumo_ws.pykumo2.cloud.tokens import TokenManager
from custom_components.ha_kumo_ws.pykumo2.errors import KumoError


async def _units(rest: CloudRestClient) -> dict[str, str]:
    """serial -> zone name for every site."""
    units: dict[str, str] = {}
    for site in await rest.get_sites():
        site_id = site.get("id")
        if not isinstance(site_id, str):
            continue
        for zone in await rest.get_zones(site_id):
            adapter = zone.get("adapter") or {}
            serial = adapter.get("deviceSerial")
            if isinstance(serial, str) and serial:
                units[serial] = str(zone.get("name") or serial)
    return units


def _print_table(units: dict[str, str], found: dict[str, str], reveal: bool) -> None:
    rows = [("SERIAL", "NAME", "PASSWORD")]
    for serial, name in sorted(units.items()):
        password = found.get(serial)
        if password is None:
            shown = "(not received)"
        elif reveal:
            shown = str.__str__(password)
        else:
            shown = f"******** ({len(password)} chars)"
        rows.append((serial, name, shown))
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    for serial, name, shown in rows:
        print(f"{serial:<{widths[0]}}  {name:<{widths[1]}}  {shown}")


async def _run(username: str, password: str, wait_s: float, reveal: bool) -> int:
    clock = SystemClock()
    ledger = CloudCallLedger(clock)
    http_client = await asyncio.to_thread(default_client_factory)
    rest = CloudRestClient(lambda: http_client, ledger, clock)
    tokens = TokenManager(rest, username, password, clock)
    rest.bind_tokens(tokens)
    session = CloudSocketSession(tokens, ledger, clock)
    try:
        units = await _units(rest)
        if not units:
            print("No units found for this account.", file=sys.stderr)
            return 1
        found = await session.request_adapter_status(units, timeout=wait_s)
    except KumoError as err:
        print(f"Error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    finally:
        await session.async_close()
        await rest.async_close()
    _print_table(units, found, reveal)
    print(f"\n{len(found)}/{len(units)} passwords, {ledger.total} cloud calls", file=sys.stderr)
    return 0 if found.keys() >= units.keys() else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait")
    parser.add_argument("--reveal", action="store_true", help="print passwords in clear")
    args = parser.parse_args()
    username = os.environ.get("KUMO_USERNAME") or input("Kumo Cloud username: ")
    password = os.environ.get("KUMO_PASSWORD") or getpass.getpass("Kumo Cloud password: ")
    return asyncio.run(_run(username, password, args.timeout, args.reveal))


if __name__ == "__main__":
    raise SystemExit(main())
