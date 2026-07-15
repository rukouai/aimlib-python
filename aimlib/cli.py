"""Small, dependency-free command line interface for common aimlib workflows."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from . import Aimlib, AimlibError


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


async def _devices_list(args) -> int:
    async with Aimlib() as ai:
        devices = await ai.devices.list(
            region=args.region,
            carrier=args.carrier,
            browser=args.browser,
        )
        for device in devices:
            print(
                _json(
                    {
                        "id": device.id,
                        "region": device.region,
                        "carrier": device.carrier,
                        "carriers": device.carriers,
                        "browser_available": device.browser_available,
                        "lease_ends_at": device.lease_ends_at,
                        "lease_open_ended": device.lease_open_ended,
                        "proxy_status": device.proxy.status if device.proxy else None,
                    }
                )
            )
    return 0


async def _rotate_ip(args) -> int:
    async with Aimlib() as ai:
        device = await ai.devices.get(args.device_id)
        operation = await device.rotate_ip(wait=not args.no_wait, timeout=args.timeout)
        print(_json(dict(operation)))
    return 0


async def _proxy(args) -> int:
    async with Aimlib() as ai:
        device = await ai.devices.get(args.device_id)
        if device.proxy is None:
            raise AimlibError("the rental proxy is not ready")
        urls = {
            "http": device.proxy.http_url,
            "socks5": device.proxy.socks5_url,
            "socks5h": device.proxy.socks5h_url,
        }
        print(urls[args.protocol])
    return 0


async def _tickets_tail(args) -> int:
    seen: set[tuple[str, str, str]] = set()
    async with Aimlib() as ai:
        while True:
            for summary in await ai.tickets.list():
                ticket = await ai.tickets.get(summary.id)
                for message in ticket.messages:
                    key = (
                        ticket.id,
                        str(message.get("created_at", "")),
                        str(message.get("body", "")),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    print(
                        _json(
                            {
                                "ticket_id": ticket.id,
                                "subject": ticket.subject,
                                "author": message.get("author"),
                                "body": message.get("body"),
                                "created_at": message.get("created_at"),
                            }
                        ),
                        flush=True,
                    )
            if args.once:
                return 0
            await asyncio.sleep(args.interval)


def _optional_bool(value: str) -> Optional[bool]:
    normalized = value.lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aimlib")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="inspect active-rental devices")
    devices_sub = devices.add_subparsers(dest="devices_command", required=True)
    devices_list = devices_sub.add_parser("list", help="list devices")
    devices_list.add_argument("--region")
    devices_list.add_argument("--carrier")
    devices_list.add_argument("--browser", type=_optional_bool)
    devices_list.set_defaults(handler=_devices_list)

    rotate = subparsers.add_parser("rotate-ip", help="rotate a device's mobile IP")
    rotate.add_argument("device_id")
    rotate.add_argument("--timeout", default="4m")
    rotate.add_argument("--no-wait", action="store_true")
    rotate.set_defaults(handler=_rotate_ip)

    proxy = subparsers.add_parser("proxy", help="print one credentialed proxy URL")
    proxy.add_argument("device_id")
    proxy.add_argument("--protocol", choices=("http", "socks5", "socks5h"), default="http")
    proxy.set_defaults(handler=_proxy)

    tickets = subparsers.add_parser("tickets", help="work with support tickets")
    tickets_sub = tickets.add_subparsers(dest="tickets_command", required=True)
    tail = tickets_sub.add_parser("tail", help="follow ticket messages")
    tail.add_argument("--interval", type=float, default=5.0)
    tail.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    tail.set_defaults(handler=_tickets_tail)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except AimlibError as error:
        detail = str(error)
        if error.request_id:
            detail += f" (request_id={error.request_id})"
        print(f"aimlib: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
