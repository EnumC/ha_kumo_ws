#!/usr/bin/env python3
"""
Scan a CIDR (e.g. 192.168.1.0/24). For hosts with TCP/80 open, send:

PUT http://<ip>/api?m=
Content-Type: application/json
Body: {"c":{"indoorUnit":{"status":{}}}}

Then report hosts whose response body matches:
{"_api_error": "device_authentication_error"}

Outputs a JSON list of matching IPs to stdout; progress/logs go to stderr.

"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests

PUT_PATH = "/api?m="
PUT_JSON = {"c": {"indoorUnit": {"status": {}}}}
TARGET_STR = '{"_api_error": "device_authentication_error"}'


@dataclass
class Result:
    ip: str
    port_open: bool
    matched: bool
    status_code: Optional[int] = None
    body: Optional[str] = None
    error: Optional[str] = None


def tcp_port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_host(ip: str, connect_timeout: float, http_timeout: float) -> Result:
    if not tcp_port_open(ip, 80, timeout=connect_timeout):
        return Result(ip=ip, port_open=False, matched=False)

    url = f"http://{ip}{PUT_PATH}"
    try:
        r = requests.put(
            url,
            json=PUT_JSON,
            headers={"Content-Type": "application/json"},
            timeout=(http_timeout),
        )
        body = (r.text or "").strip()
        matched = TARGET_STR in body
        return Result(
            ip=ip,
            port_open=True,
            matched=matched,
            status_code=r.status_code,
            body=body,
        )
    except requests.RequestException as e:
        return Result(ip=ip, port_open=True, matched=False, error=str(e))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kumo_scan.py",
        description="Scan CIDR for port 80, send PUT /api?m=, and match auth error response.",
    )
    p.add_argument(
        "cidr",
        help='IP range in CIDR notation, e.g. "192.168.4.0/24"',
    )
    p.add_argument("--workers", type=int, default=255, help="Concurrent workers (default: 255)")
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=1,
        help="TCP connect timeout seconds (default: 1)",
    )
    p.add_argument(
        "--http-timeout",
        type=float,
        default=10,
        help="HTTP read timeout seconds (default: 10)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print open hosts even if they don't match.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        net = ipaddress.ip_network(args.cidr, strict=False)
    except ValueError as e:
        print(f"Invalid CIDR '{args.cidr}': {e}", file=sys.stderr)
        return 2

    hosts = [str(ip) for ip in net.hosts()]
    total = len(hosts)
    print(f"Scanning {net} ({total} hosts) ...", file=sys.stderr)

    open_count = 0
    match_count = 0
    matched_devices: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(check_host, ip, args.connect_timeout, args.http_timeout): ip
            for ip in hosts
        }

        for fut in as_completed(futures):
            res = fut.result()

            if not res.port_open:
                continue

            open_count += 1

            if res.error:
                if args.verbose:
                    print(f"OPEN   {res.ip}  ERROR  {res.error}", file=sys.stderr)
                continue

            if res.matched:
                match_count += 1
                matched_devices.append(res.ip)
                print(
                    f"MATCH  {res.ip}  HTTP {res.status_code}  {TARGET_STR}",
                    file=sys.stderr,
                )
            else:
                if args.verbose:
                    body_preview = (res.body or "").replace("\n", "\\n")
                    if len(body_preview) > 200:
                        body_preview = body_preview[:200] + "…"
                    print(
                        f"OPEN   {res.ip}  HTTP {res.status_code}  body={body_preview}",
                        file=sys.stderr,
                    )

    print(f"Done. Port 80 open: {open_count} | Matches: {match_count}", file=sys.stderr)
    print(json.dumps(matched_devices))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
