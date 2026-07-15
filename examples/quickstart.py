"""Runnable aimlib quickstart: python examples/quickstart.py"""

import asyncio

import httpx

from aimlib import Aimlib


async def main() -> None:
    async with Aimlib() as ai:
        devices = await ai.devices.list()
        if not devices:
            raise RuntimeError("this account has no active device rental")
        device = devices[0]
        print("device", device.id, device.region, device.carrier)

        if device.proxy:
            async with httpx.AsyncClient(**device.proxy.as_httpx()) as client:
                response = await client.get("https://example.com", timeout=30)
                response.raise_for_status()
                print("proxy request", response.status_code)

        if device.browser_available:
            async with device.browser_session(ttl="10m", idle_timeout="2m") as session:
                page = await session.new_page()
                await page.goto("https://example.com", wait_until="domcontentloaded")
                print("browser title", await page.title())


if __name__ == "__main__":
    asyncio.run(main())
