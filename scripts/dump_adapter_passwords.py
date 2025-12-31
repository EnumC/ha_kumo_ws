"""Dump adapter_update password values from the Kumo Cloud websocket."""

from __future__ import annotations

import argparse
import asyncio

from custom_components.ha_kumo_ws.pykumo2 import MitsubishiComfortClient
from custom_components.ha_kumo_ws.pykumo2.payloads import AdapterUpdatePayload
from custom_components.ha_kumo_ws.pykumo2.socket import SocketUpdateManager


async def _resolve_site_ids(client: MitsubishiComfortClient) -> list[str]:
    sites = await client.async_get_sites()
    return [site.get("id") for site in sites if site.get("id")]


async def _fetch_devices(client: MitsubishiComfortClient, site_ids: list[str]):
    devices: dict[str, str] = {}
    for site_id in site_ids:
        for serial, device in (await client.async_get_devices(site_id)).items():
            devices[serial] = device.name
    return devices


async def _run(username: str, password: str, timeout: float) -> int:
    client = MitsubishiComfortClient(username=username, password=password, site_ids=[])
    try:
        site_ids = await _resolve_site_ids(client)
        if not site_ids:
            print("No site IDs found for this account.")
            return 1
        client.site_ids = site_ids

        devices = await _fetch_devices(client, site_ids)
        if not devices:
            print("No devices found.")
            return 1

        print("Devices:")
        for serial, name in devices.items():
            print(f"{serial} - {name}")

        pending = set(devices)
        found: dict[str, str] = {}
        done = asyncio.Event()

        async def _on_event(event: str, payload: dict) -> None:
            if event != "adapter_update":
                return
            update = AdapterUpdatePayload.model_validate(payload)
            serial = update.deviceSerial
            if not serial or serial not in devices:
                return
            if update.password is None or serial in found:
                return
            found[serial] = update.password
            print(f"{serial} ({devices[serial]}): {update.password}")
            pending.discard(serial)
            if not pending:
                done.set()

        manager = SocketUpdateManager(
            client=client,
            device_serials=list(devices),
            callback=_on_event,
            refresh_on_connect=True,
            request_types=("adapterStatus",),
        )

        await manager.start()
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if pending:
                print("Timed out waiting for adapter_update on: " + ", ".join(sorted(pending)))
        finally:
            await manager.stop()
    finally:
        await client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump adapter_update password values for all devices."
    )
    parser.add_argument("--username", required=True, help="Kumo Cloud username")
    parser.add_argument("--password", required=True, help="Kumo Cloud password")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for adapter_update events.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.username, args.password, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
