# aimlib Python SDK

[![PyPI](https://img.shields.io/pypi/v/aimlib)](https://pypi.org/project/aimlib/)
[![Python](https://img.shields.io/pypi/pyversions/aimlib)](https://pypi.org/project/aimlib/)
[![CI](https://github.com/rukouai/aimlib-python/actions/workflows/ci.yml/badge.svg)](https://github.com/rukouai/aimlib-python/actions/workflows/ci.yml)

Asynchronous Python client for aimlib mobile proxies, on-device remote-browser sessions, radio
operations, and support tickets. Python 3.10 or newer is required.

## Install

For proxy, device, radio-operation, and support APIs:

```sh
python -m pip install --upgrade aimlib
```

Include managed remote-browser support when needed:

```sh
python -m pip install --upgrade "aimlib[browser]"
```

The browser extra connects to Chromium on the leased phone. It does not download or launch a browser
binary on your computer.

## Configure

```sh
export AIMLIB_API_KEY="<api-key>"
export AIMLIB_BASE_URL="https://uswest1.aimlib.com"
```

API keys, credential-bearing proxy URLs, and browser connection tokens are secrets. Do not print or
log them.

## Example

```python
from aimlib import Aimlib

async with Aimlib() as ai:
    device = (await ai.devices.list())[0]

    http_proxy = device.proxy.http_url
    socks_remote_dns_proxy = device.proxy.socks5h_url

    async with await device.browser(ttl="10m") as session:
        page = await session.new_page()
        await page.goto("https://example.com")
        print(await page.title())
```

Only devices in your active rentals are returned. A remote browser can be created only after a
rental is active and the phone has been assigned to it.

## Main APIs

- `ai.devices.list()` and `ai.device(id)` return devices in active rentals.
- `device.proxy.http_url`, `.socks5_url`, and `.socks5h_url` address the same proxy host and port.
- `device.browser()` creates a remote-browser session for the active rental.
- `session.new_page()`, `.disconnect()`, `.connect()`, and `.stop()` manage that session.
- `device.rotate_ip()` and `device.switch_carrier()` support blocking or queued operation modes.
- `ai.operations.get(id)` and `.wait(id)` poll queued radio operations.
- `ai.tickets` creates, lists, reads, replies to, and closes support tickets.

## Development

```sh
python -m pip install -e ".[browser]"
python -m unittest discover -s tests -v
```

See the [customer documentation](https://docs.aimlib.com/) for the complete API contract, limits,
security policy, and examples. Release notes are kept in the
[changelog](https://github.com/rukouai/aimlib-python/blob/main/CHANGELOG.md).

Report security issues privately as described in the
[security policy](https://github.com/rukouai/aimlib-python/blob/main/SECURITY.md). Never include API
keys, credential-bearing proxy URLs, browser connection tokens, or customer data in an issue or log.
