"""Async customer SDK for aimlib mobile proxies and remote-browser sessions.

The SDK reads ``AIMLIB_API_KEY`` and ``AIMLIB_BASE_URL`` by default. Proxy URLs and browser
connection tokens are credentials; use them without printing or logging them.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
import importlib
import json
import math
import os
import re
import uuid
import warnings
from typing import AsyncIterator, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

__all__ = [
    "Aimlib", "Device", "Proxy", "BrowserSession", "Ticket", "Operation",
    "AimlibError", "AuthenticationError", "RateLimitError", "BadCarrierError",
    "BrowserPolicyError", "BrowserUnavailableError", "BrowserAccessDeniedError",
    "CapacityError", "LeaseInactiveError", "AccountInactiveError", "NoSessionError",
    "FootprintNotCleanError", "OperationFailedError", "OperationTimeout",
    "SessionExpiredError", "SessionNotFoundError", "SessionTimeout", "TabLimitError",
]


class AimlibError(Exception):
    """Base SDK exception with stable service diagnostics.

    ``code`` is safe for program logic. ``request_id`` can be sent to support. ``retry_after`` is
    the server-provided delay in seconds when one was available.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.request_id = request_id


class AuthenticationError(AimlibError):
    """The customer API key is missing, invalid, expired, or revoked."""


class RateLimitError(AimlibError):
    """The service asked the caller to retry after a bounded delay."""


class BadCarrierError(AimlibError):
    """The supplied carrier identifier is invalid."""


class BrowserPolicyError(AimlibError):
    """The requested browser capability is intentionally disabled by managed-browser policy."""


class CapacityError(AimlibError):
    pass


class LeaseInactiveError(AimlibError):
    pass


class AccountInactiveError(AimlibError):
    """The customer account itself is suspended or closed."""


class NoSessionError(AimlibError):
    """The operation requires an active browser session."""


class FootprintNotCleanError(AimlibError):
    """The selected footprint is not in this device's clean catalog."""


class SessionExpiredError(AimlibError):
    pass


class SessionNotFoundError(SessionExpiredError):
    pass


class SessionTimeout(AimlibError):
    pass


class OperationTimeout(AimlibError):
    def __init__(self, message: str = "operation timed out", *, operation=None, **kwargs):
        super().__init__(message, **kwargs)
        self.operation = operation


class OperationFailedError(AimlibError):
    def __init__(self, message: str = "operation failed", *, operation=None, **kwargs):
        super().__init__(message, **kwargs)
        self.operation = operation


class TabLimitError(AimlibError):
    pass


class BrowserUnavailableError(AimlibError):
    """The remote browser cannot currently be created or reached."""


class BrowserAccessDeniedError(AimlibError):
    """The browser connection token or session authorization was rejected."""


_ERROR_BY_CODE = {
    "unauthorized": AuthenticationError,
    "authentication_error": AuthenticationError,
    "rate_limited": RateLimitError,
    "bad_carrier": BadCarrierError,
    "capacity_unavailable": CapacityError,
    "lease_inactive": LeaseInactiveError,
    "account_inactive": AccountInactiveError,
    "no_session": NoSessionError,
    "footprint_not_clean": FootprintNotCleanError,
    "session_expired": SessionExpiredError,
    "session_not_found": SessionNotFoundError,
    "session_provisioning": SessionTimeout,
    "browser_unavailable": BrowserUnavailableError,
    "device_unavailable": BrowserUnavailableError,
    "browser_access_denied": BrowserAccessDeniedError,
}


def _ttl_seconds(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(v))
    if not m:
        raise ValueError(f"bad duration {v!r}")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2) or "s"]


def _retry_after_seconds(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            from datetime import datetime, timezone

            return max(0.0, (parsedate_to_datetime(str(value)) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _raise_for_typed(r: httpx.Response):
    if r.status_code < 400:
        return
    code, msg, request_id, retry_after = "", "", None, None
    try:
        body = r.json()
        code = body.get("error", "")
        msg = body.get("message", code)
        request_id = body.get("request_id")
        retry_after = body.get("retry_after", body.get("retry_after_s"))
    except Exception:  # noqa: BLE001
        msg = r.text[:200]
    request_id = request_id or r.headers.get("X-Request-ID")
    retry_after = _retry_after_seconds(retry_after or r.headers.get("Retry-After"))
    if code == "footprint_not_clean" and "list_footprints" not in msg:
        msg = (msg or "the requested footprint is unavailable") + (
            "; choose a slug returned by await device.list_footprints()"
        )
    error_type = _ERROR_BY_CODE.get(code)
    if error_type is None and r.status_code == 401:
        error_type = AuthenticationError
    if error_type is None and r.status_code == 429:
        error_type = RateLimitError
    error_type = error_type or AimlibError
    raise error_type(
        msg or f"HTTP {r.status_code}",
        code=code or None,
        status_code=r.status_code,
        retry_after=retry_after,
        request_id=request_id,
    )


class _HTTPClient:
    """Lazy async transport with bounded retries for idempotent reads."""

    def __init__(self, *, base_url: str, api_key: str, timeout: float):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self.closed = False

    def _ensure(self) -> httpx.AsyncClient:
        if self.closed:
            raise AimlibError("the Aimlib client is closed")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}) or {})
        if method == "POST" and not any(k.lower() == "idempotency-key" for k in headers):
            headers["Idempotency-Key"] = f"sdk:{uuid.uuid4()}"
        attempts = 3 if method == "GET" else 1
        last_error = None
        for attempt in range(attempts):
            try:
                response = await self._ensure().request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            if response.status_code not in {429, 502, 503, 504} or attempt + 1 >= attempts:
                return response
            delay = _retry_after_seconds(response.headers.get("Retry-After"))
            await response.aclose()
            await asyncio.sleep(min(delay if delay is not None else 0.25 * (2**attempt), 5.0))
        raise last_error or AimlibError("request failed")

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs):
        async with self._ensure().stream(method, url, **kwargs) as response:
            yield response

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
        self.closed = True


def _idempotency_headers(key: Optional[str]) -> dict[str, str]:
    return {"Idempotency-Key": key} if key else {}


async def _reject_detectable_browser_binding(*_args, **_kwargs):
    raise BrowserPolicyError(
        "page and context bindings are unavailable in managed-browser sessions"
    )


def _apply_browser_driver_policy(driver_api):
    """Disable page-visible Python bindings before returning the raw driver objects to callers."""
    try:
        driver_types = (driver_api.Page, driver_api.BrowserContext)
        async_playwright = driver_api.async_playwright
    except AttributeError as exc:
        raise AimlibError("browser driver is incompatible with this SDK release") from exc
    for driver_type in driver_types:
        driver_type.expose_function = _reject_detectable_browser_binding
        driver_type.expose_binding = _reject_detectable_browser_binding
    return async_playwright


def _async_playwright():
    """Load the supported browser client and fail closed when it is unavailable."""
    try:
        return _apply_browser_driver_policy(importlib.import_module("patchright.async_api"))
    except ImportError as driver_error:
        raise AimlibError(
            'browser support is not installed; install the SDK with: pip install "aimlib[browser]"'
        ) from driver_error


def _browser_connect_failure_code(exc: Exception) -> str:
    """Reduce a driver failure to a stable, non-secret diagnostic category.

    Browser-driver errors can include the full connection endpoint in their call log. The SDK must
    not copy that text into customer logs because it contains the browser-session identifier. A
    small category distinguishes transport, authorization, policy, and browser-startup failures
    while keeping URLs, headers, tokens, and device addresses out of the exception.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "discovery_timeout"
    if isinstance(exc, httpx.TransportError):
        return "discovery_transport_failed"
    if isinstance(exc, BrowserPolicyError):
        return "browser_policy_denied"
    text = str(exc).lower()
    if "managed download guard unavailable" in text:
        return "browser_security_unavailable"
    if "blocked by " in text or (
        "blocked" in text and ("browser" in text or "connection" in text)
    ):
        return "browser_policy_denied"
    if "403" in text and ("websocket" in text or "unexpected server response" in text):
        return "browser_access_denied"
    if "502" in text:
        return "browser_unavailable"
    if "target page, context or browser has been closed" in text or "browser closed" in text:
        return "browser_closed"
    if "websocket" in text:
        return "browser_connection_failed"
    if "timeout" in text or "timed out" in text:
        return "browser_connection_timeout"
    return "browser_connection_failed"


def _validated_ua_brand_rows(value, *, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise BrowserPolicyError(f"managed browser {label} metadata is unavailable")
    rows: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, dict):
            raise BrowserPolicyError(f"managed browser {label} metadata is invalid")
        brand = row.get("brand")
        version = row.get("version")
        if (
            not isinstance(brand, str)
            or not isinstance(version, str)
            or not 1 <= len(brand) <= 64
            or not 1 <= len(version) <= 64
        ):
            raise BrowserPolicyError(f"managed browser {label} metadata is invalid")
        rows.append({"brand": brand, "version": version})
    return rows


def _with_google_chrome_brand(rows: list[dict[str, str]], *, label: str) -> list[dict[str, str]]:
    if any(row["brand"] == "Google Chrome" for row in rows):
        return rows
    chromium_index = next(
        (index for index, row in enumerate(rows) if row["brand"] == "Chromium"),
        None,
    )
    if chromium_index is None:
        raise BrowserPolicyError(f"managed browser {label} Chromium brand is unavailable")
    branded = list(rows)
    branded.insert(
        chromium_index + 1,
        {"brand": "Google Chrome", "version": rows[chromium_index]["version"]},
    )
    return branded


def _google_chrome_user_agent_override(
    identity: object,
    model_override: Optional[str] = None,
) -> Optional[dict]:
    """Build a coherent ChromePublic to Google Chrome UA-CH override."""
    if not isinstance(identity, dict):
        raise BrowserPolicyError("managed browser identity metadata is unavailable")
    user_agent = identity.get("userAgent")
    navigator_platform = identity.get("navigatorPlatform")
    ua_platform = identity.get("platform")
    string_fields = {
        "userAgent": user_agent,
        "navigatorPlatform": navigator_platform,
        "platform": ua_platform,
        "platformVersion": identity.get("platformVersion"),
        "architecture": identity.get("architecture"),
        "model": identity.get("model"),
        "bitness": identity.get("bitness"),
    }
    if any(not isinstance(value, str) or len(value) > 512 for value in string_fields.values()):
        raise BrowserPolicyError("managed browser identity metadata is invalid")
    if not user_agent or "Android" not in user_agent or "Mobile" not in user_agent:
        raise BrowserPolicyError("managed browser did not present a mobile Android user agent")
    if ua_platform != "Android" or identity.get("mobile") is not True:
        raise BrowserPolicyError("managed browser did not present Android client hints")
    if model_override is not None and (
        not isinstance(model_override, str) or not 1 <= len(model_override) <= 128
    ):
        raise BrowserPolicyError("managed browser footprint model is invalid")

    brands = _validated_ua_brand_rows(identity.get("brands"), label="brand")
    full_versions = _validated_ua_brand_rows(
        identity.get("fullVersionList"),
        label="full-version brand",
    )
    low_has_google = any(row["brand"] == "Google Chrome" for row in brands)
    full_has_google = any(row["brand"] == "Google Chrome" for row in full_versions)
    if low_has_google != full_has_google:
        raise BrowserPolicyError("managed browser Chrome brand metadata is inconsistent")
    effective_model = model_override if model_override is not None else identity["model"]
    if low_has_google and effective_model == identity["model"]:
        return None

    return {
        "userAgent": user_agent,
        "platform": navigator_platform,
        "userAgentMetadata": {
            "brands": _with_google_chrome_brand(brands, label="brand"),
            "fullVersionList": _with_google_chrome_brand(
                full_versions,
                label="full-version brand",
            ),
            "platform": ua_platform,
            "platformVersion": identity["platformVersion"],
            "architecture": identity["architecture"],
            "model": effective_model,
            "mobile": True,
            "bitness": identity["bitness"],
            "wow64": identity.get("wow64") is True,
        },
    }


def _validated_footprint_identity(value: object, applied_footprint: str) -> Optional[dict]:
    """Validate the public, browser-observable identity for an applied footprint."""
    if not applied_footprint:
        return None
    if not isinstance(applied_footprint, str) or len(applied_footprint) > 128:
        raise BrowserPolicyError("managed browser footprint identifier is invalid")
    if not isinstance(value, dict) or value.get("name") != applied_footprint:
        raise BrowserPolicyError("managed browser footprint identity is unavailable")
    model = value.get("model")
    width = value.get("screen_width")
    height = value.get("screen_height")
    ratio = value.get("device_pixel_ratio")
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise BrowserPolicyError("managed browser footprint model is invalid")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not 320 <= width <= 10_000
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 320 <= height <= 10_000
    ):
        raise BrowserPolicyError("managed browser footprint display is invalid")
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(float(ratio))
        or not 0.5 <= float(ratio) <= 10
    ):
        raise BrowserPolicyError("managed browser footprint pixel ratio is invalid")
    return {
        "name": applied_footprint,
        "model": model,
        "screen_width": width,
        "screen_height": height,
        "device_pixel_ratio": float(ratio),
    }


def _expected_device_metrics(identity: Optional[dict]) -> Optional[dict]:
    """Convert the OS-applied physical display into expected portrait CSS metrics."""
    if identity is None:
        return None
    ratio = identity["device_pixel_ratio"]
    css_width = math.ceil(identity["screen_width"] / ratio)
    css_height = math.ceil(identity["screen_height"] / ratio)
    return {
        "screenWidth": css_width,
        "screenHeight": css_height,
        "devicePixelRatio": ratio,
    }


def _identity_verification(identity: Optional[dict]) -> Optional[dict]:
    metrics = _expected_device_metrics(identity)
    if identity is None or metrics is None:
        return None
    return {
        "model": identity["model"],
        "screenWidth": metrics["screenWidth"],
        "screenHeight": metrics["screenHeight"],
        "devicePixelRatio": metrics["devicePixelRatio"],
    }


_READ_NATIVE_BROWSER_IDENTITY = r"""async () => {
  const data = navigator.userAgentData;
  if (!data) return null;
  let high = {};
  try {
    high = await data.getHighEntropyValues([
      'architecture', 'bitness', 'fullVersionList', 'model', 'platformVersion', 'wow64'
    ]);
  } catch (_) {
    return null;
  }
  const copyBrands = value => Array.isArray(value)
    ? value.map(row => ({brand: String(row.brand || ''), version: String(row.version || '')}))
    : [];
  return {
    userAgent: String(navigator.userAgent || ''),
    navigatorPlatform: String(navigator.platform || ''),
    brands: copyBrands(data.brands),
    fullVersionList: copyBrands(high.fullVersionList),
    platform: String(data.platform || ''),
    platformVersion: String(high.platformVersion || ''),
    architecture: String(high.architecture || ''),
    model: String(high.model || ''),
    mobile: data.mobile === true,
    bitness: String(high.bitness || ''),
    wow64: high.wow64 === true,
  };
}"""


_VERIFY_MANAGED_BROWSER_IDENTITY = r"""async expected => {
  const data = navigator.userAgentData;
  // UA-CH is unavailable on Chrome's internal startup page. That is not an override failure:
  // the same target exposes the metadata after its first secure navigation.
  if (!data || !Array.isArray(data.brands)) return null;
  let high = {};
  try {
    high = await data.getHighEntropyValues(['fullVersionList', 'model']);
  } catch (_) {
    return null;
  }
  if (!Array.isArray(high.fullVersionList)) return null;
  const branded = data.brands.some(row => row.brand === 'Google Chrome') &&
    high.fullVersionList.some(row => row.brand === 'Google Chrome');
  if (!branded || !expected) return branded;
  // visualViewport.height retains subpixel precision while innerHeight is integer-rounded.
  // Allow only that bounded rounding delta; a collapsed/full-screen viewport remains far outside it.
  const headfulInsets = screen.availHeight < screen.height &&
    innerHeight < screen.availHeight &&
    visualViewport && visualViewport.height > 0 && visualViewport.height <= innerHeight + 1;
  // Android's integer density quantization can expose one CSS pixel above the physical-pixel / DPR
  // ceiling. Permit only that one-sided delta; larger or smaller display identities still fail.
  const screenMetrics = screen.width >= expected.screenWidth &&
    screen.width <= expected.screenWidth + 1 &&
    screen.height >= expected.screenHeight &&
    screen.height <= expected.screenHeight + 1;
  return high.model === expected.model &&
    screenMetrics &&
    Math.abs(devicePixelRatio - expected.devicePixelRatio) < 0.001 &&
    headfulInsets;
}"""


class Proxy:
    """A credentialed endpoint that accepts HTTP and SOCKS5 on one host and port.

    URL attributes contain credentials and must not be logged. ``protocols`` is the authoritative
    capability tuple; the scalar ``protocol`` property is deprecated compatibility metadata.
    """

    def __init__(self, d: dict):
        self.id = d.get("id")
        self.url = d.get("url")
        self.http_url = d.get("http_url") or self._with_scheme("http")
        self.socks5_url = d.get("socks5_url") or self._with_scheme("socks5")
        self.socks5h_url = d.get("socks5h_url") or self._with_scheme("socks5h")
        self.protocols = tuple(d.get("protocols") or ("http", "socks5"))
        self._protocol = d.get("protocol")
        self.status = d.get("status")
        self.status_detail = d.get("status_detail")
        parsed = urlsplit(self.url or self.http_url or self.socks5h_url or "")
        self.host = parsed.hostname
        self.port = parsed.port

    @property
    def protocol(self):
        """Return legacy scalar protocol metadata; prefer :attr:`protocols`."""
        warnings.warn(
            "Proxy.protocol is deprecated; use Proxy.protocols and an explicit URL attribute",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._protocol

    def _with_scheme(self, scheme: str) -> Optional[str]:
        if not self.url:
            return None
        parsed = urlsplit(self.url)
        return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))

    def as_httpx(self, protocol: str = "http") -> dict:
        """Return keyword arguments for ``httpx.Client`` or ``httpx.AsyncClient``."""
        if protocol == "http":
            url = self.http_url
        elif protocol in {"socks5", "socks5h"}:
            url = self.socks5h_url if protocol == "socks5h" else self.socks5_url
        else:
            raise ValueError("protocol must be http, socks5, or socks5h")
        return {"proxy": url}

    def as_requests(self, protocol: str = "http") -> dict:
        """Return keyword arguments for ``requests.Session.request``.

        SOCKS use requires ``requests[socks]`` (PySocks).
        """
        if protocol == "http":
            proxies = {"http": self.http_url, "https": self.http_url}
        elif protocol in {"socks5", "socks5h"}:
            url = self.socks5h_url if protocol == "socks5h" else self.socks5_url
            proxies = {"http": url, "https": url}
        else:
            raise ValueError("protocol must be http, socks5, or socks5h")
        return {"proxies": proxies}

    def __repr__(self):
        return (
            f"Proxy(host={self.host!r}, port={self.port!r}, "
            f"protocols={self.protocols!r}, status={self.status!r})"
        )


class Operation(dict):
    """A dict-compatible network-operation result with typed convenience properties."""

    @property
    def id(self) -> Optional[str]:
        """Return the service operation identifier."""
        return self.get("operation_id")

    @property
    def status(self) -> Optional[str]:
        """Return the current queued, running, or terminal status."""
        return self.get("status")

    @property
    def type(self) -> Optional[str]:
        """Return the customer-facing operation type."""
        return self.get("type")

    @property
    def terminal(self) -> bool:
        """Report whether the operation has reached a terminal state."""
        return self.status in {"succeeded", "failed", "timeout"}

    @property
    def succeeded(self) -> bool:
        """Report whether the operation completed successfully."""
        return self.status == "succeeded"

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __repr__(self):
        return f"Operation(id={self.id!r}, type={self.type!r}, status={self.status!r})"


def _raise_for_operation(operation: Operation):
    if operation.status == "timeout":
        raise OperationTimeout(
            operation.get("message", "the operation did not complete before its timeout"),
            operation=operation,
            code=operation.get("error", "operation_timeout"),
        )
    if operation.status == "failed":
        raise OperationFailedError(
            operation.get("message", "the operation could not be completed"),
            operation=operation,
            code=operation.get("error", "operation_failed"),
        )


class Device:
    """A device in the account's active rental inventory."""

    def __init__(self, ai: "Aimlib", d: dict):
        self._ai = ai
        self._update(d)

    def _update(self, d: dict):
        self.id = d["device_id"]
        self.region = d.get("region")
        self.carrier = d.get("carrier")
        self.carriers = list(d.get("carriers") or [])
        self.current_egress_ip = d.get("current_egress_ip")
        self.proxy = Proxy(d["proxy"]) if d.get("proxy") else None
        browser = d.get("browser") or {}
        self.browser_available = browser.get("available")
        lease = d.get("lease") or {}
        self.lease_id = lease.get("id")
        self.lease_starts_at = lease.get("starts_at")
        self.lease_ends_at = lease.get("ends_at")
        self.lease_open_ended = bool(lease.get("open_ended", self.lease_ends_at is None))
        return self

    def __repr__(self):
        return f"Device(id={self.id!r}, region={self.region!r}, carrier={self.carrier!r})"

    async def refresh(self) -> "Device":
        """Refresh this object in place with one direct device lookup."""
        r = await self._ai._http.get(f"/v1/devices/{self.id}")
        _raise_for_typed(r)
        return self._update(r.json())

    async def whoami(self) -> dict:
        """Return the device's latest public egress IP, carrier, and observation time."""
        r = await self._ai._http.get(f"/v1/devices/{self.id}/whoami")
        _raise_for_typed(r)
        data = r.json()
        self.current_egress_ip = data.get("egress_ip") or self.current_egress_ip
        self.carrier = data.get("carrier") or self.carrier
        return data

    async def egress_ip(self) -> Optional[str]:
        """Return the latest observed public egress IP."""
        return (await self.whoami()).get("egress_ip")

    async def usage(self) -> dict:
        """Return byte totals for this rental's accounting window (reporting only)."""
        r = await self._ai._http.get(f"/v1/devices/{self.id}/usage")
        _raise_for_typed(r)
        return r.json()

    async def list_footprints(self) -> list:
        """Return clean footprint records; pass a record's ``slug`` to :meth:`browser`."""
        r = await self._ai._http.get(f"/v1/devices/{self.id}/footprints")
        _raise_for_typed(r)
        return r.json().get("footprints", [])

    async def browser(
        self,
        footprint=None,
        ttl=None,
        idle_timeout=None,
        sticky=None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> "BrowserSession":
        """Create a managed-browser session without connecting it yet."""
        body: dict = {}
        if ttl is not None:
            body["ttl"] = _ttl_seconds(ttl)
        if idle_timeout is not None:
            body["idle_timeout"] = _ttl_seconds(idle_timeout)
        if footprint is not None:
            body["footprint"] = footprint
        if sticky is not None:
            warnings.warn(
                "sticky is deprecated and has no effect; rental lifecycle owns IP persistence",
                DeprecationWarning,
                stacklevel=2,
            )
        r = await self._ai._http.post(
            f"/v1/devices/{self.id}/browser",
            json=body,
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        session_data = r.json()
        sess = BrowserSession(self._ai, self, session_data)
        if footprint and session_data.get("desired_footprint") != footprint:
            try:
                fr = await self._ai._http.post(
                    f"/v1/devices/{self.id}/footprint", json={"footprint": footprint}
                )
                _raise_for_typed(fr)
                sess.desired_footprint = footprint
            except BaseException as exc:
                try:
                    await sess.stop()
                except Exception as cleanup_exc:  # noqa: BLE001
                    raise AimlibError(
                        "footprint selection failed and the new browser session could not be torn down"
                    ) from cleanup_exc
                raise exc
        elif footprint:
            sess.desired_footprint = footprint
        return sess

    def browser_session(self, **kwargs):
        """Return a direct async context manager: ``async with device.browser_session()``."""
        return _BrowserSessionContext(self, kwargs)

    async def rotate_ip(
        self,
        wait: bool = True,
        timeout=240,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Operation:
        """Request a new IP; blocking failures raise ``OperationFailedError`` or timeout."""
        timeout_s = _ttl_seconds(timeout)
        r = await self._ai._http.post(
            f"/v1/devices/{self.id}/rotate-ip",
            json={"wait": wait, "timeout_s": timeout_s},
            timeout=timeout_s + 30,
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        out = Operation(r.json())
        if wait:
            _raise_for_operation(out)
        if out.get("new_ip"):
            self.current_egress_ip = out["new_ip"]
        return out

    async def switch_carrier(
        self,
        carrier: str,
        wait: bool = True,
        timeout=200,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Operation:
        """Switch to a provisioned carrier; blocking failure results raise typed exceptions."""
        timeout_s = _ttl_seconds(timeout)
        r = await self._ai._http.post(
            f"/v1/devices/{self.id}/carrier",
            json={"carrier": carrier, "wait": wait, "timeout_s": timeout_s},
            timeout=timeout_s + 30,
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        out = Operation(r.json())
        if wait:
            _raise_for_operation(out)
        if out.get("status") == "succeeded" and out.get("carrier"):
            self.carrier = out["carrier"]
        if out.get("new_ip"):
            self.current_egress_ip = out["new_ip"]
        return out

    async def rotation_url(self, *, idempotency_key: Optional[str] = None) -> str:
        """Mint a token-scoped rotation URL for this active rental."""
        r = await self._ai._http.post(
            f"/v1/devices/{self.id}/rotation-link",
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        return r.json()["url"]


class _BrowserSessionContext:
    def __init__(self, device: Device, kwargs: dict):
        self.device = device
        self.kwargs = kwargs
        self.session: Optional[BrowserSession] = None

    async def __aenter__(self):
        self.session = await self.device.browser(**self.kwargs)
        return await self.session.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        if self.session is not None:
            return await self.session.__aexit__(exc_type, exc, traceback)
        return None


class _Devices:
    def __init__(self, ai: "Aimlib"):
        self._ai = ai

    async def list(self, *, region=None, carrier=None, browser=None) -> list[Device]:
        """List active-rental devices, optionally filtered by region, carrier, or browser support."""
        params = {
            key: value
            for key, value in {"region": region, "carrier": carrier, "browser": browser}.items()
            if value is not None
        }
        kwargs = {"params": params} if params else {}
        r = await self._ai._http.get("/v1/devices", **kwargs)
        _raise_for_typed(r)
        return [Device(self._ai, d) for d in r.json()]

    async def get(self, device_id: str) -> Device:
        """Fetch one active-rental device directly."""
        r = await self._ai._http.get(f"/v1/devices/{device_id}")
        _raise_for_typed(r)
        return Device(self._ai, r.json())


class _Operations:
    def __init__(self, ai: "Aimlib"):
        self._ai = ai

    async def get(self, operation_id: str) -> Operation:
        """Fetch a customer-owned IP-rotation or carrier-switch operation."""
        r = await self._ai._http.get(f"/v1/operations/{operation_id}")
        _raise_for_typed(r)
        return Operation(r.json())

    async def wait(self, operation_id: str, timeout=300, poll_interval=1.5) -> Operation:
        """Poll an operation until terminal; polling duration accepts seconds or ``5m`` syntax."""
        timeout_s = _ttl_seconds(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            operation = await self.get(operation_id)
            if operation.terminal:
                _raise_for_operation(operation)
                return operation
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OperationTimeout("operation polling timed out", operation=operation)
            await asyncio.sleep(min(poll_interval, remaining))


class BrowserSession:
    """One managed browser session bound to the leased device."""

    def __init__(self, ai: "Aimlib", device: Device, data: dict):
        self._ai = ai
        self.device = device
        self.id = data["session_id"]
        self.status = data.get("status")
        self.connect_url = data["connect_url"]
        self.connect_token = data.get("connect_token")
        self.max_tabs = int(data.get("max_tabs") or 5)  # per-session tab cap (server-configured)
        self._gave_initial = False
        self.egress_ip = None
        self.fingerprint = data.get("resolved_profile") or {}
        self.desired_footprint = data.get("desired_footprint") or ""
        self.applied_footprint = data.get("applied_footprint") or ""
        self.expires_at = data.get("expires_at")
        self.region = data.get("region")
        self.ready_in_s = data.get("ready_in_s")
        self._pw = None
        self.browser = None  # the live Playwright Browser once connected
        self._identity_cdp_sessions = []
        self._identity_pages: set[int] = set()
        self._native_browser_identity = None

    async def wait_until_ready(self, timeout=180):
        """Wait for this exact remote session to become driveable."""
        timeout_s = _ttl_seconds(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            r = await self._ai._http.get(f"/v1/devices/{self.device.id}/browser")
            if r.status_code == 404:
                raise SessionExpiredError("session not found / expired")
            _raise_for_typed(r)
            data = r.json()
            if data.get("session_id") != self.id:
                raise SessionExpiredError("session was replaced by a newer browser session")
            self.status = data.get("status")
            self.applied_footprint = data.get("applied_footprint") or ""
            self.expires_at = data.get("expires_at") or self.expires_at
            self.egress_ip = data.get("egress_ip")
            self.fingerprint = data.get("resolved_profile") or {}
            # Do not connect until a requested browser footprint is fully active.
            footprint_ok = (not self.desired_footprint) or (self.applied_footprint == self.desired_footprint)
            if self.status in ("ready", "active", "idle") and footprint_ok:
                _validated_footprint_identity(self.fingerprint, self.applied_footprint)
                return
            if self.status in ("expiring", "gone"):
                raise SessionExpiredError(f"session {self.status}")
            if self.status == "failed":
                raise AimlibError("session failed")
            await asyncio.sleep(2)
        raise SessionTimeout(f"session not ready within {timeout_s}s")

    async def connect(self, timeout=180):
        """Wait until ready and connect the supported async browser client.

        Transient connection failures are retried against the same remote session. Failed local
        client instances are stopped before retrying.
        """
        if self.browser is not None:
            try:
                if self.browser.is_connected():
                    return self.browser
            except (AttributeError, TypeError):
                pass
            await self._disconnect()

        timeout_s = _ttl_seconds(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        await self.wait_until_ready(timeout_s)
        # The browser client expects the HTTP form of the returned connection endpoint.
        disco = self.connect_url.replace("wss://", "https://").replace("ws://", "http://")
        last_error: Exception | None = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                # Check reachability before the browser client reduces transport failures to a
                # generic exception. Authentication stays in a header and never enters a URL or log.
                discovery = await self._ai._http.get(
                    disco.rstrip("/") + "/json/version",
                    headers={"Authorization": f"Bearer {self.connect_token}"},
                    timeout=max(1, min(remaining, 15)),
                )
                if discovery.status_code >= 500:
                    raise BrowserUnavailableError("remote browser is temporarily unavailable")
                _raise_for_typed(discovery)
                try:
                    ws_url = discovery.json().get("webSocketDebuggerUrl")
                except (ValueError, AttributeError, TypeError):
                    ws_url = None
                if not ws_url:
                    raise BrowserUnavailableError("remote browser returned invalid connection metadata")
                async_playwright = _async_playwright()
                self._pw = await async_playwright().start()
                self.browser = await self._pw.chromium.connect_over_cdp(
                    disco,
                    headers={"Authorization": f"Bearer {self.connect_token}"},
                    timeout=max(1, int(min(remaining, 30) * 1000)),
                )
                for context in self.browser.contexts:
                    for page in context.pages:
                        await self._apply_page_identity(page)
                return self.browser
            except Exception as exc:  # noqa: BLE001 - retry a bounded transient connection failure
                last_error = exc
                await self._disconnect()
                if isinstance(exc, BrowserPolicyError):
                    raise
                if attempt < attempts and loop.time() < deadline:
                    await asyncio.sleep(min(3, max(0, deadline - loop.time())))
        if isinstance(last_error, BrowserUnavailableError):
            raise BrowserUnavailableError(
                f"remote browser did not become reachable after {attempts} attempts"
            ) from last_error
        failure_code = _browser_connect_failure_code(last_error) if last_error else "deadline_exhausted"
        raise SessionTimeout(
            f"remote browser did not become reachable within {timeout_s}s after {attempts} attempts "
            f"({failure_code})"
        ) from last_error

    async def set_footprint(
        self,
        footprint: Optional[str],
        timeout=120,
        *,
        idempotency_key: Optional[str] = None,
    ):
        """Apply an available footprint and reconnect this session.

        Pass ``None`` or an empty string to restore the device default. Select non-empty values from
        ``device.list_footprints()``.
        """
        fp = footprint or ""
        timeout_s = _ttl_seconds(timeout)
        r = await self._ai._http.post(
            f"/v1/devices/{self.device.id}/footprint",
            json={"footprint": fp},
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        self.desired_footprint = fp
        await self._disconnect()  # Applying a footprint invalidates the existing connection.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            r = await self._ai._http.get(f"/v1/devices/{self.device.id}/browser")
            if r.status_code == 404:
                raise SessionExpiredError("session not found / expired")
            _raise_for_typed(r)
            data = r.json()
            if data.get("session_id") != self.id:
                raise SessionExpiredError("session was replaced by a newer browser session")
            self.status = data.get("status")
            self.applied_footprint = data.get("applied_footprint") or ""
            self.fingerprint = data.get("resolved_profile") or {}
            if self.applied_footprint == fp and self.status in ("ready", "active", "idle"):
                _validated_footprint_identity(self.fingerprint, self.applied_footprint)
                await self.connect(timeout=timeout_s)  # transparent reconnect to the same session_id
                return
            await asyncio.sleep(2)
        raise SessionTimeout(f"footprint {fp!r} not applied within {timeout_s}s")

    async def _disconnect(self):
        for cdp_session in reversed(self._identity_cdp_sessions):
            try:
                await cdp_session.detach()
            except Exception:  # noqa: BLE001
                pass
        self._identity_cdp_sessions = []
        self._identity_pages.clear()
        self._native_browser_identity = None
        for closer in (lambda: self.browser and self.browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                c = closer()
                if c is not None:
                    await c
            except Exception:  # noqa: BLE001
                pass
        self.browser = None
        self._pw = None
        self._gave_initial = False

    async def _apply_page_identity(self, page):
        """Apply the managed browser identity consistently."""
        page_key = id(page)
        if page_key in self._identity_pages:
            return
        cdp_session = None
        try:
            footprint_identity = _validated_footprint_identity(
                self.fingerprint,
                self.applied_footprint,
            )
            identity = await self._native_identity_for_page(page, footprint_identity)
            override = _google_chrome_user_agent_override(
                identity,
                footprint_identity["model"] if footprint_identity else None,
            )
            expected = _identity_verification(footprint_identity)
            if override is not None:
                cdp_session = await page.context.new_cdp_session(page)
                await cdp_session.send("Emulation.setUserAgentOverride", override)
            if override is not None or expected is not None:
                verified = await page.evaluate(
                    _VERIFY_MANAGED_BROWSER_IDENTITY,
                    expected,
                )
                # Internal/about:blank startup pages cannot expose UA-CH. The override has already
                # been validated on the secure bootstrap tab below; a definitive false result on a
                # page that does expose UA-CH still fails closed.
                if verified is False:
                    raise BrowserPolicyError("managed browser identity did not apply")
                if cdp_session is not None:
                    self._identity_cdp_sessions.append(cdp_session)
                    cdp_session = None
            self._identity_pages.add(page_key)
        except BrowserPolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - never expose CDP endpoint or device details
            raise BrowserPolicyError("managed browser identity policy could not be applied") from exc
        finally:
            if cdp_session is not None:
                try:
                    await cdp_session.detach()
                except Exception:  # noqa: BLE001
                    pass

    async def _native_identity_for_page(self, page, footprint_identity: Optional[dict] = None):
        """Read UA-CH in a secure first-party context when Chrome's startup tab cannot expose it."""
        if self._native_browser_identity is not None:
            return self._native_browser_identity
        identity = await page.evaluate(_READ_NATIVE_BROWSER_IDENTITY)
        if identity is None:
            bootstrap_page = None
            bootstrap_cdp_session = None
            try:
                bootstrap_page = await page.context.new_page()
                await bootstrap_page.goto(
                    "https://docs.aimlib.com/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                identity = await bootstrap_page.evaluate(_READ_NATIVE_BROWSER_IDENTITY)
                override = _google_chrome_user_agent_override(
                    identity,
                    footprint_identity["model"] if footprint_identity else None,
                )
                expected = _identity_verification(footprint_identity)
                if override is not None:
                    bootstrap_cdp_session = await page.context.new_cdp_session(
                        bootstrap_page
                    )
                    await bootstrap_cdp_session.send(
                        "Emulation.setUserAgentOverride",
                        override,
                    )
                if override is not None or expected is not None:
                    verified = await bootstrap_page.evaluate(
                        _VERIFY_MANAGED_BROWSER_IDENTITY,
                        expected,
                    )
                    if verified is not True:
                        raise BrowserPolicyError(
                            "managed browser identity did not apply"
                        )
            except Exception as exc:  # noqa: BLE001 - keep URL/session details out of the error
                if isinstance(exc, BrowserPolicyError):
                    raise
                raise BrowserPolicyError(
                    "managed browser identity bootstrap was unavailable"
                ) from exc
            finally:
                if bootstrap_cdp_session is not None:
                    try:
                        await bootstrap_cdp_session.detach()
                    except Exception:  # noqa: BLE001
                        pass
                if bootstrap_page is not None:
                    try:
                        await bootstrap_page.close()
                    except Exception:  # noqa: BLE001
                        pass
        if identity is None:
            raise BrowserPolicyError("managed browser identity metadata is unavailable")
        self._native_browser_identity = identity
        return identity

    async def disconnect(self):
        """Disconnect this local SDK client without stopping the remote session.

        A later connect() or new_page() reconnects to the same session. Use stop() when the remote
        browser should be torn down.
        """
        await self._disconnect()

    async def new_page(self):
        """Open a tab. Reuses the session's initial tab on the first call, then opens new tabs up to
        the per-session limit (self.max_tabs, server-configured). Raises TabLimitError past the
        limit — open a separate session for more parallelism (tabs share one IP + fingerprint)."""
        b = self.browser
        if b is not None:
            try:
                if not b.is_connected():
                    b = None
            except (AttributeError, TypeError):
                pass
        if b is None:
            b = await self.connect()
        ctx = b.contexts[0] if b.contexts else await b.new_context()
        if not self._gave_initial and ctx.pages:
            self._gave_initial = True
            page = ctx.pages[0]
            await self._apply_page_identity(page)
            return page
        if len(ctx.pages) >= self.max_tabs:
            raise TabLimitError(
                f"tab limit reached ({self.max_tabs} per session); close a tab or start a separate session"
            )
        page = await ctx.new_page()
        await self._apply_page_identity(page)
        return page

    async def wait_until_stopped(self, timeout=120):
        """Wait until the service confirms this session is no longer live."""
        timeout_s = _ttl_seconds(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            r = await self._ai._http.get(f"/v1/devices/{self.device.id}/browser")
            if r.status_code == 404:
                self.status = "gone"
                return
            _raise_for_typed(r)
            data = r.json()
            if data.get("session_id") != self.id or data.get("status") in ("gone", "failed"):
                self.status = "gone"
                return
            self.status = data.get("status") or self.status
            await asyncio.sleep(2)
        raise SessionTimeout(f"session teardown not confirmed within {timeout_s}s")

    async def stop(self, wait: bool = True, timeout=120):
        """Stop the remote session within one shared deadline, including local cleanup."""
        timeout_s = _ttl_seconds(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        response = None
        request_error = None
        try:
            response = await asyncio.wait_for(
                self._ai._http.delete(
                    f"/v1/devices/{self.device.id}/browser",
                    timeout=max(0.1, deadline - loop.time()),
                ),
                timeout=max(0.1, deadline - loop.time()),
            )
        except Exception as exc:  # noqa: BLE001
            request_error = exc
        try:
            await asyncio.wait_for(
                self._disconnect(),
                timeout=max(0.1, deadline - loop.time()),
            )
        except asyncio.TimeoutError as exc:
            raise SessionTimeout(
                f"local browser cleanup exceeded the {timeout_s}s stop deadline"
            ) from exc
        if request_error is not None:
            raise AimlibError("could not request browser teardown") from request_error
        if response.status_code == 404:
            self.status = "gone"
            return
        _raise_for_typed(response)
        self.status = "expiring"
        if wait:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SessionTimeout(f"session teardown not confirmed within {timeout_s}s")
            try:
                await asyncio.wait_for(
                    self.wait_until_stopped(max(1, math.ceil(remaining))),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                raise SessionTimeout(
                    f"session teardown not confirmed within {timeout_s}s"
                ) from exc

    def __del__(self):
        if self.browser is None and self._pw is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._disconnect())

    async def __aenter__(self):
        try:
            await self.connect()
            return self
        except BaseException as exc:
            try:
                await self.stop()
            except Exception as cleanup_exc:  # noqa: BLE001
                raise AimlibError(
                    f"browser connection failed: {exc}; automatic teardown was not confirmed"
                ) from cleanup_exc
            raise exc

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            await self.stop()
        except Exception as cleanup_exc:  # noqa: BLE001
            if exc is not None:
                raise AimlibError(
                    f"browser operation failed: {exc}; automatic teardown was not confirmed"
                ) from cleanup_exc
            raise


class Ticket:
    """A support ticket. A ticket is only COMPLETE (`status == "closed"`) once BOTH you and the
    operator have closed it; any new message reopens it. `awaiting_close_from` says who still needs
    to close ('customer', 'operator', or 'both').

    `acknowledged` is True once a support agent has opened or replied to the ticket — i.e. an
    investigation is underway and you can check back later. `status_detail` is a human-readable
    summary of where it stands (e.g. "A support agent is reviewing your ticket")."""

    def __init__(self, ai: "Aimlib", d: dict):
        self._ai = ai
        self.id = d["id"]
        self.subject = d.get("subject")
        self.status = d.get("status")
        self.acknowledged = bool(d.get("acknowledged"))
        self.acknowledged_at = d.get("acknowledged_at")
        self.status_detail = d.get("status_detail")
        self.customer_closed = bool(d.get("customer_closed"))
        self.operator_closed = bool(d.get("operator_closed"))
        self.awaiting_close_from = d.get("awaiting_close_from")
        self.messages = d.get("messages", [])  # [{author: 'customer'|'operator', body, created_at}]
        self.created_at = d.get("created_at")
        self.updated_at = d.get("updated_at")
        self.closed_at = d.get("closed_at")

    @property
    def complete(self) -> bool:
        """Report whether both customer and operator have closed the ticket."""
        return self.status == "closed"

    async def reply(self, body: str, *, idempotency_key: Optional[str] = None) -> "Ticket":
        """Post a customer reply and return the refreshed ticket."""
        return await self._ai.tickets.reply(self.id, body, idempotency_key=idempotency_key)

    async def close(self, *, idempotency_key: Optional[str] = None) -> "Ticket":
        """Request customer-side closure and return the refreshed ticket."""
        return await self._ai.tickets.close(self.id, idempotency_key=idempotency_key)

    def __repr__(self):
        return f"Ticket(id={self.id!r}, subject={self.subject!r}, status={self.status!r})"


class _Tickets:
    def __init__(self, ai: "Aimlib"):
        self._ai = ai

    async def create(
        self, subject: str, body: str, *, idempotency_key: Optional[str] = None
    ) -> Ticket:
        """File a new support ticket (subject + first message)."""
        r = await self._ai._http.post(
            "/v1/tickets",
            json={"subject": subject, "body": body},
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        return Ticket(self._ai, r.json())

    async def list(self) -> "list[Ticket]":
        """List tickets visible to the authenticated customer."""
        r = await self._ai._http.get("/v1/tickets")
        _raise_for_typed(r)
        return [Ticket(self._ai, d) for d in r.json()]

    async def get(self, ticket_id: str) -> Ticket:
        """Fetch one ticket including its full message thread."""
        r = await self._ai._http.get(f"/v1/tickets/{ticket_id}")
        _raise_for_typed(r)
        return Ticket(self._ai, r.json())

    async def reply(
        self, ticket_id: str, body: str, *, idempotency_key: Optional[str] = None
    ) -> Ticket:
        """Post a message on a ticket (reopens it if it was closed)."""
        r = await self._ai._http.post(
            f"/v1/tickets/{ticket_id}/messages",
            json={"body": body},
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        return Ticket(self._ai, r.json())

    async def close(
        self, ticket_id: str, *, idempotency_key: Optional[str] = None
    ) -> Ticket:
        """Mark the ticket closed from your side. It's only complete once the operator closes too."""
        r = await self._ai._http.post(
            f"/v1/tickets/{ticket_id}/close",
            headers=_idempotency_headers(idempotency_key),
        )
        _raise_for_typed(r)
        return Ticket(self._ai, r.json())


class Aimlib:
    """Authenticated async client for the regional aimlib customer API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60,
    ):
        self.api_key = api_key or os.environ.get("AIMLIB_API_KEY")
        if not self.api_key:
            raise AuthenticationError("no API key (pass api_key= or set AIMLIB_API_KEY)")
        # Use the regional customer API URL assigned to the account unless explicitly overridden.
        self.base_url = (base_url or os.environ.get("AIMLIB_BASE_URL", "https://uswest.aimlib.com")).rstrip("/")
        self._http = _HTTPClient(base_url=self.base_url, api_key=self.api_key, timeout=timeout)
        self.devices = _Devices(self)
        self.operations = _Operations(self)
        self.tickets = _Tickets(self)

    async def device(self, device_id: str) -> Device:
        """Compatibility alias for ``devices.get(device_id)``."""
        return await self.devices.get(device_id)

    async def usage(self, device_id: Optional[str] = None) -> dict:
        """Return account usage totals, or one device's active-rental totals."""
        path = f"/v1/devices/{device_id}/usage" if device_id else "/v1/usage"
        r = await self._http.get(path)
        _raise_for_typed(r)
        return r.json()

    async def events(self) -> AsyncIterator[dict]:
        """Yield authenticated operation and lease lifecycle events from the SSE feed.

        The service intentionally ends streams periodically. This iterator reconnects until it is
        closed or the surrounding task is cancelled. Delivery is at-least-once.
        """
        while True:
            async with self._http.stream("GET", "/v1/events") as response:
                _raise_for_typed(response)
                event_name = "message"
                data_lines = []
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                payload = raw
                            yield {"event": event_name, "data": payload}
                        event_name = "message"
                        data_lines = []
            await asyncio.sleep(1)

    async def aclose(self):
        """Close the underlying HTTP transport. Safe to call more than once."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def __del__(self):
        transport = getattr(self, "_http", None)
        if transport is None or transport.closed or transport._client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(transport.aclose())
