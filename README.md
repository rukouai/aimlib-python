# aimlib Python SDK

[![PyPI](https://img.shields.io/pypi/v/aimlib)](https://pypi.org/project/aimlib/)
[![Python](https://img.shields.io/pypi/pyversions/aimlib)](https://pypi.org/project/aimlib/)

Asynchronous Python client for aimlib mobile proxies, managed remote-browser sessions, network
operations, and support tickets. Python 3.10 or newer is required.

## Install

```sh
python -m pip install --upgrade aimlib
```

Include the optional browser integration when needed:

```sh
python -m pip install --upgrade "aimlib[browser]"
```

## Documentation

Setup, examples, the supported API contract, and customer-visible release notes are maintained at
[docs.aimlib.com](https://docs.aimlib.com/).

## Development

```sh
python -m pip install -e ".[browser]"
python -m unittest discover -s tests -v
```

Report security issues privately as described in the
[security policy](https://github.com/rukouai/aimlib-python/blob/main/SECURITY.md). Never include API
keys, credential-bearing proxy URLs, browser connection tokens, or customer data in an issue or log.
