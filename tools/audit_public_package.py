#!/usr/bin/env python3
"""Reject secrets and private infrastructure details from the public SDK snapshot."""

from __future__ import annotations

import ipaddress
import re
import sys
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "wheel-test",
}
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml"}

FORBIDDEN = {
    "system path": re.compile(
        r"(?:^|[\s\"'(])(?:/(?:etc|opt|srv|var|data|home|root)/|[A-Z]:\\)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "private key material": re.compile(
        r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY", re.IGNORECASE
    ),
    "credential-shaped token": re.compile(
        r"(?:AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|github_pat_|ghp_[A-Za-z0-9]{30,}|"
        r"sk-[A-Za-z0-9]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{35})"
    ),
}

# These fingerprints are generated from the private release policy. The source terms intentionally
# remain outside the public repository so the disclosure gate does not become a disclosure itself.
FORBIDDEN_POLICY_HASHES = {
    "0c4243c45502e6c965536517cc132e91c66ebfe8155a47aaa6cfdeab35b24664",
    "28387a164997aec602d65711a6a74d4ee162d8fb5987702e7962031a61262887",
    "42a377b433d48c538f1c691b5fee99f88472fa6bc7017f0cefc8730ce0a82c41",
    "44bd64db8aafcfbb3e18597c3b7fcdd1111730625b4a6b43b28f1076d8bb5389",
    "44fe0f5649b09435cfc50d53d088e08ae335e14c3a1b975af91410123f176a1c",
    "51e0ea525114ecfc155d029a43f54f9c90f412c7c20c3327e18ff4a7b5a7664f",
    "584b64d60e0dd8be1d26889c981d5965b5d22dd77f36b8f1c76d2b7fca38ff87",
    "682fbae20f3428bcec4c117c57bea18d438c4758d972909b41dbe22884e0d6b8",
    "795b104abe3e4134960ca245ded0e617f162c751209347b7f003bc35e062f43e",
    "94a35fdc30df7d129a447217f9ffade7e4917bdcc90460d7db2f0460c08c0e06",
    "a32b176c56bea1f4e551e76e17d60bb6acac7822850dc3673b944ae8f3db8dce",
    "d076aa69c63d1a2adcf2211a5b95ed32eb96ff440348a81e9685071516d5828f",
    "eaca63dd3283fbec39f0efb5caae51b4794e32eb8e5fcb78c96ddfbcb0a75cbd",
}

ALLOWED_HOSTS = {
    "docs.aimlib.com",
    "example.com",
    "github.com",
    "img.shields.io",
    "pypi.org",
    "uswest1.aimlib.com",
}
ALLOWED_TEST_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>`]+", re.IGNORECASE)
AIMLIB_HOST_PATTERN = re.compile(r"\b(?:[a-z0-9-]+\.)+aimlib\.com\b", re.IGNORECASE)
IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
POLICY_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9._/-]*", re.IGNORECASE)
def public_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {".gitignore", "LICENSE"}:
            files.append(path)
    return sorted(files)


def allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return (
        host in ALLOWED_HOSTS
        or host == "example.test"
        or host.endswith(".example.test")
        or host.endswith(".invalid")
    )


def audit(path: Path) -> list[str]:
    relative = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for label, pattern in FORBIDDEN.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{relative}:{line}: {label}")

    token_matches = list(POLICY_TOKEN_PATTERN.finditer(text.lower()))
    for width in (1, 2, 3):
        for index in range(len(token_matches) - width + 1):
            window = token_matches[index : index + width]
            candidate = " ".join(match.group(0) for match in window)
            digest = sha256(candidate.encode()).hexdigest()
            if digest in FORBIDDEN_POLICY_HASHES:
                line = text.count("\n", 0, window[0].start()) + 1
                errors.append(f"{relative}:{line}: private release-policy fingerprint")

    for match in AIMLIB_HOST_PATTERN.finditer(text):
        host = match.group(0)
        if not allowed_host(host):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{relative}:{line}: aimlib hostname is not public-approved")

    for match in URL_PATTERN.finditer(text):
        value = match.group(0).rstrip(".,);]")
        parsed = urlsplit(value)
        host = parsed.hostname
        if host and not allowed_host(host):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{relative}:{line}: URL host is not public-approved: {host}")
        if (parsed.username is not None or parsed.password is not None) and host and not (
            host == "example.test"
            or host.endswith(".example.test")
            or host.endswith(".invalid")
        ):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{relative}:{line}: credential-bearing URL outside test data")

    for match in IPV4_PATTERN.finditer(text):
        try:
            address = ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line_text = text[line_start : None if line_end == -1 else line_end]
        if re.search(r"\b(?:version|chrome|chromium)\b", line_text, re.IGNORECASE):
            continue
        if not any(address in network for network in ALLOWED_TEST_NETWORKS):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{relative}:{line}: non-documentation IPv4 address")
    return errors


def main() -> int:
    files = public_files()
    errors = [error for path in files for error in audit(path)]
    if errors:
        print("Public SDK disclosure audit failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Public SDK disclosure audit passed ({len(files)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
