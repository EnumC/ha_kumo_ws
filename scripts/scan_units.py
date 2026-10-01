#!/usr/bin/env python3
"""Scan a CIDR for Kumo WiFi adapters and print matching IPs as JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ha_kumo_ws.pykumo2.local.discovery import fingerprint_scan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kumo_scan.py",
        description="Scan CIDR for port 80, send PUT /api?m=, and match auth error response.",
    )
    parser.add_argument("cidr", help='IP range in CIDR notation, e.g. "192.168.4.0/24"')
    parser.add_argument(
        "--workers", type=int, default=255, help="Concurrent workers (default: 255)"
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=1,
        help="TCP connect timeout seconds (default: 1)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=10,
        help="HTTP read timeout seconds (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Accepted for compatibility. Non-matching bodies are not printed.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"Scanning {args.cidr} ...", file=sys.stderr)
    try:
        matches = asyncio.run(
            fingerprint_scan(
                [args.cidr],
                concurrency=args.workers,
                connect_timeout=args.connect_timeout,
                request_timeout=args.http_timeout,
            )
        )
    except ValueError as exc:
        print(f"Invalid CIDR '{args.cidr}': {exc}", file=sys.stderr)
        return 2
    for ip in matches:
        print(f"MATCH  {ip}", file=sys.stderr)
    print(f"Done. Matches: {len(matches)}", file=sys.stderr)
    if args.verbose:
        print("Non-matching hosts are not listed.", file=sys.stderr)
    print(json.dumps(matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
