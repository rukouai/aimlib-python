# Contributing

Issues and pull requests are welcome. Keep changes focused on the public Python SDK and avoid
including customer data or private infrastructure details.

## Development setup

Python 3.10 or newer is required.

```sh
python -m venv .venv
python -m pip install -e ".[browser]"
python -m unittest discover -s tests -v
python -m compileall -q aimlib tests
python -m pip check
```

Tests must use mock credentials and reserved example domains. Do not run an ordinary pull request
against production devices, rentals, or customer accounts.

## Pull requests

- Add or update tests for customer-visible behavior.
- Preserve backwards compatibility and clearly identify any breaking behavior in the pull request.
- Keep API keys, proxy credentials, browser tokens, cookies, captured pages, and device data out of
  commits, tests, screenshots, and logs.
- Keep internal topology, hostnames, routing details, infrastructure paths, and operational
  implementation names out of the public repository.
- Do not change the package version in an unrelated pull request.
- Keep `CHANGELOG.md` as a pointer to the customer documentation; maintainers publish release notes
  with the corresponding package release.

Run the public-disclosure audit before opening a pull request:

```sh
python tools/audit_public_package.py
```

Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md), not a public
issue.
