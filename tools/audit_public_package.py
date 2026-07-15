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
EXPECTED_FILES = {
    ".github/dependabot.yml",
    ".gitignore",
    "aimlib/__init__.py",
    "aimlib/cli.py",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "pyproject.toml",
    "README.md",
    "SECURITY.md",
    "examples/quickstart.py",
    "tests/test_sdk.py",
}
README_HEADINGS = {
    "# aimlib Python SDK",
    "## Install",
    "## Documentation",
    "## Development",
}

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
    "000f4ef5a9a4092da2fabd18e79c62cbddeae1ebba5a57978d676615043d62fd",
    "1614c183d554138f5d3d74ff5512a17c39b78bac296520cdf75593a449fb8ec6",
    "28387a164997aec602d65711a6a74d4ee162d8fb5987702e7962031a61262887",
    "313c9a69a5dc6f9586d94caa581ed66a2b61e23c2e68ef9195e3028ad7220625",
    "42a377b433d48c538f1c691b5fee99f88472fa6bc7017f0cefc8730ce0a82c41",
    "44bd64db8aafcfbb3e18597c3b7fcdd1111730625b4a6b43b28f1076d8bb5389",
    "44fe0f5649b09435cfc50d53d088e08ae335e14c3a1b975af91410123f176a1c",
    "45569da57f4b7bf472d7a864ef4781451cae6383fee9fb0ae40c59aa1ce475b7",
    "51e0ea525114ecfc155d029a43f54f9c90f412c7c20c3327e18ff4a7b5a7664f",
    "584b64d60e0dd8be1d26889c981d5965b5d22dd77f36b8f1c76d2b7fca38ff87",
    "5bc83522bc742ce6181c8a9bd840fef4de6fad4b27aa8ce49dbc8987748cb767",
    "64e008dda8a9b420d54fb89c28dca9a0b4319b02d1771cda68e606ddb269f4a8",
    "6622e7e8f3f91c6f456f8e4181674a93d8540fdd9132d61949642241c293698b",
    "682fbae20f3428bcec4c117c57bea18d438c4758d972909b41dbe22884e0d6b8",
    "795b104abe3e4134960ca245ded0e617f162c751209347b7f003bc35e062f43e",
    "881488d0cf87191ed5f7a941ece04ca3231657299ebc7cdb156997ba8b9333f1",
    "8f7ce8d80965ce6b36163108930dfda410732c20e0a1b3904f1043728ee14da6",
    "94a35fdc30df7d129a447217f9ffade7e4917bdcc90460d7db2f0460c08c0e06",
    "9496da1fe020f9e828455651ad831d6db483a0a1e62f231dbc15e87b01ea2282",
    "9dcb6415df7246e5e10003a994179ff3c2b46997d4878af594f9597f7baed2c9",
    "a32b176c56bea1f4e551e76e17d60bb6acac7822850dc3673b944ae8f3db8dce",
    "a35b6fe780f12c863fbfc091480383582583e577c78372ada6dcd0ce0b5122b1",
    "ab7c5fd867e5a801efc95cc738da1360ac95743cc6625fe9a57ca78b15ad37a2",
    "d076aa69c63d1a2adcf2211a5b95ed32eb96ff440348a81e9685071516d5828f",
    "eaca63dd3283fbec39f0efb5caae51b4794e32eb8e5fcb78c96ddfbcb0a75cbd",
    "f760fe3239c6a43bcaddb516a9978b885ca34505965076b2dce18060189bc6b9",
}

FORBIDDEN_DOCUMENTATION_HASHES = {
    "000f4ef5a9a4092da2fabd18e79c62cbddeae1ebba5a57978d676615043d62fd",
    "0a443a771fa4d8481a1b539f17df6607440a678ba53676a752c45c2dc62ed2e5",
    "188d6d79cb5bdb083c49d0cf3084ebf22732c38d41f369f7bdd08671651efde3",
    "313c9a69a5dc6f9586d94caa581ed66a2b61e23c2e68ef9195e3028ad7220625",
    "45569da57f4b7bf472d7a864ef4781451cae6383fee9fb0ae40c59aa1ce475b7",
    "5bc83522bc742ce6181c8a9bd840fef4de6fad4b27aa8ce49dbc8987748cb767",
    "6064640044b6aa881fb94ec61b949b948b58b8089a71d488ce1d766ea42b58a0",
    "64edaa3fb9310e98cdb183cddbf156d9964a05c017fa7f8ee3c262909fa36759",
    "6622e7e8f3f91c6f456f8e4181674a93d8540fdd9132d61949642241c293698b",
    "700ab86bfeead33ad06430d68af54876510bc4d73843cac030348c4075f09a3f",
    "745b48aad78ec779a5ba68c94c4885ada6f49de33c0c0b6f4e48743506b226eb",
    "7ce54cbababdd64826b853179905315617306f430e5154177ddbed04c282b7da",
    "8e4fc4aa918f707a2f0d020f6c3e966fd92f47b924841bddd5f3eb1cb89b394f",
    "96a737cd06c7a9a2177b393c7526b05c7d07e684b8c7770dcf00451df637f188",
    "a35b6fe780f12c863fbfc091480383582583e577c78372ada6dcd0ce0b5122b1",
    "c060b0f7de6776cd059d6c8655cf1ca8c70cec50ae5675150db47635d2af5035",
    "ddf8d310d21693308b1ed6553e4972c38073537b656b6e8b95f635b2b129aa56",
    "debdc6fdb6c19c94ff78b653767bfac108d84ed51a54a58a72866dc635b15729",
    "efb7b4fa5b973a130179252ae699d4bc117c9744126803d723b003df0b5e7912",
    "f015362a586d8fb8c8390091870b7295b9bce67d1db09d839c59470463fbc70b",
    "f1ea8dbd316e2039eb985d48d4550b7543fa139171bf0ecc04b14438be221848",
    "fa1236b01ff592437be065a04abcd407b361cf63078d79e17b18a1e550994925",
}

ALLOWED_HOSTS = {
    "docs.aimlib.com",
    "example.com",
    "github.com",
    "img.shields.io",
    "pypi.org",
    "uswest.aimlib.com",
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
    policy_hashes = FORBIDDEN_POLICY_HASHES
    if path.suffix.lower() == ".md":
        policy_hashes = policy_hashes | FORBIDDEN_DOCUMENTATION_HASHES
    for width in (1, 2, 3):
        for index in range(len(token_matches) - width + 1):
            window = token_matches[index : index + width]
            candidate = " ".join(match.group(0) for match in window)
            digest = sha256(candidate.encode()).hexdigest()
            if digest in policy_hashes:
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


def audit_documentation_shape() -> list[str]:
    errors: list[str] = []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = {line for line in readme.splitlines() if line.startswith("#")}
    if headings != README_HEADINGS:
        errors.append("README.md: public README must remain a minimal package landing page")
    if "https://docs.aimlib.com/" not in readme or len(readme.encode()) > 4096:
        errors.append("README.md: public README documentation link or size is invalid")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_headings = [line for line in changelog.splitlines() if line.startswith("#")]
    if changelog_headings != ["# Changelog"] or "https://docs.aimlib.com/changelog.html" not in changelog:
        errors.append("CHANGELOG.md: release notes must remain on the customer documentation site")
    if len(changelog.encode()) > 1024:
        errors.append("CHANGELOG.md: public changelog exceeds its pointer-only size limit")
    return errors


def main() -> int:
    files = public_files()
    actual_files = {path.relative_to(ROOT).as_posix() for path in files}
    errors: list[str] = []
    if actual_files != EXPECTED_FILES:
        errors.append("public SDK file allowlist does not match the snapshot")
    errors.extend(audit_documentation_shape())
    errors.extend(error for path in files for error in audit(path))
    if errors:
        print("Public SDK disclosure audit failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Public SDK disclosure audit passed ({len(files)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
