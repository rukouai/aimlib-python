# Changelog

Customer-visible SDK and API changes are documented here. PyPI releases are immutable; fixes are
published as a new version.

## 0.4.3 - 2026-07-14

- Fixed managed-browser footprint selection so each selected profile presents its own reported
  device model, screen size, and device pixel ratio.
- Added strict identity validation so a selected footprint cannot silently fall back to a different
  visible profile.

## 0.4.2 - 2026-07-14

- Published `aimlib` through PyPI for the first time.
- Added public package metadata, source links, supported-Python classifiers, and a rendered project
  description.
- Added isolated GitHub OIDC trusted publishing with no long-lived PyPI upload token.
- Added public CI across Python 3.10 through 3.14 and package-install verification.
- No runtime API behavior changed from `0.4.1`.

## 0.4.1 - 2026-07-14

- Added `proxy.socks5h_url` so remote DNS intent is explicit while retaining `socks5_url` and legacy
  fields for compatibility.
- Added `ai.operations.get()` and `ai.operations.wait()` for queued IP rotations and carrier
  switches.
- Added support for the tenant-scoped `GET /v1/operations/{id}` endpoint.
- Reduced radio-operation results to stable customer fields.
- Normalized temporary remote-browser reachability failures as `browser_unavailable`.
- Removed the unsupported development browser fallback from the browser extra.
- Expanded documentation for proxies, the REST API, remote browsers, security, and support.

## 0.4.0 - 2026-07-14

- Added active-rental and browser-availability metadata to device objects.
- Added credential-safe HTTP and SOCKS5 proxy properties for the shared endpoint.
- Made browser creation and initial footprint selection atomic.
- Added confirmed teardown, idempotent stop, explicit disconnect/reconnect, and cleanup that
  preserves the original customer operation error.
- Added cellular-only browser egress enforcement and WebRTC local-address protection.
- Added browser connection errors that do not expose credentialed connection URLs.
- Disabled page and context function bindings under managed-browser policy.

## 0.3.1 - 2026-06-10

- Added `device.rotate_ip()` with bounded waiting for a confirmed cellular egress address.
- Added `device.switch_carrier()` for supported provisioned carriers.
- Added queued and blocking radio-operation modes with structured terminal results.

## 0.3.0 - 2026-06-09

- Moved remote-browser sessions onto the leased phone.
- Added `device.list_footprints()`, `device.browser(footprint=...)`, and
  `session.set_footprint()`.
- Kept proxy-only installation lightweight with an optional browser extra.

## 0.2.1 - 2026-06-08

- Added customer-visible ticket acknowledgement and review status.

## 0.2.0 - 2026-06-07

- Added customer support ticket creation, listing, retrieval, replies, and two-sided closure.

## 0.1.0 - 2026-06-07

- Initial asynchronous Python SDK.
- Added leased-device discovery, proxy metadata, browser lifecycle, and typed customer errors.
