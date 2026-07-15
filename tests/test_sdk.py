import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from aimlib import (
    AimlibError,
    BrowserPolicyError,
    BrowserSession,
    BrowserUnavailableError,
    DataCapError,
    Device,
    OperationTimeout,
    Proxy,
    SessionExpiredError,
    SessionTimeout,
    _Operations,
    _apply_browser_driver_policy,
    _async_playwright,
    _expected_device_metrics,
    _google_chrome_user_agent_override,
    _raise_for_typed,
    _ttl_seconds,
    _validated_footprint_identity,
    _VERIFY_MANAGED_BROWSER_IDENTITY,
)


def response(status, payload, method="GET", path="/v1/devices"):
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request(method, "https://example.test" + path),
    )


def session_for(http):
    ai = MagicMock()
    ai._http = http
    device = Device(
        ai,
        {
            "device_id": "dev-1",
            "browser": {"available": True},
            "lease": {"id": "lease-1", "ends_at": "2026-07-13T01:00:00Z"},
        },
    )
    return BrowserSession(
        ai,
        device,
        {
            "session_id": "session-1",
            "status": "provisioning",
            "connect_url": "wss://browser.example.test/session-1",
            "connect_token": "connect-secret",
            "expires_at": "2026-07-13T00:30:00Z",
            "max_tabs": 5,
        },
    )


class DurationTests(unittest.TestCase):
    def test_duration_units(self):
        self.assertEqual(_ttl_seconds(30), 30)
        self.assertEqual(_ttl_seconds(" 5m "), 300)
        self.assertEqual(_ttl_seconds("2h"), 7200)

    def test_bad_duration(self):
        with self.assertRaises(ValueError):
            _ttl_seconds("tomorrow")


class ErrorMappingTests(unittest.TestCase):
    @staticmethod
    def response(status, payload):
        return response(status, payload)

    def test_typed_error(self):
        with self.assertRaisesRegex(DataCapError, "monthly cap"):
            _raise_for_typed(
                self.response(
                    409,
                    {"error": "data_cap_exceeded", "message": "monthly cap reached"},
                )
            )

    def test_browser_unavailable_error(self):
        for code in ("browser_unavailable", "device_unavailable"):
            with self.subTest(code=code), self.assertRaises(BrowserUnavailableError):
                _raise_for_typed(self.response(503, {"error": code}))

    def test_unknown_error_uses_base_class(self):
        with self.assertRaises(AimlibError):
            _raise_for_typed(self.response(500, {"error": "unexpected"}))


class BrowserDriverSelectionTests(unittest.TestCase):
    @staticmethod
    def driver_api():
        class FakePage:
            async def expose_function(self, _name, _callback):
                return None

            async def expose_binding(self, _name, _callback):
                return None

        class FakeBrowserContext:
            async def expose_function(self, _name, _callback):
                return None

            async def expose_binding(self, _name, _callback):
                return None

        factory = MagicMock(name="async_playwright")
        return SimpleNamespace(
            Page=FakePage,
            BrowserContext=FakeBrowserContext,
            async_playwright=factory,
        )

    def test_hardened_patchright_driver_is_selected(self):
        driver = self.driver_api()

        with patch("aimlib.importlib.import_module", return_value=driver) as importer:
            factory = _async_playwright()

        self.assertIs(factory, driver.async_playwright)
        importer.assert_called_once_with("patchright.async_api")

    def test_missing_supported_driver_fails_closed(self):
        with (
            patch.dict("aimlib.os.environ", {}, clear=True),
            patch("aimlib.importlib.import_module", side_effect=ImportError) as importer,
            self.assertRaisesRegex(AimlibError, "browser support is not installed"),
        ):
            _async_playwright()

        importer.assert_called_once_with("patchright.async_api")

class BrowserDriverPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_and_context_bindings_are_disabled_before_cdp(self):
        driver = BrowserDriverSelectionTests.driver_api()

        factory = _apply_browser_driver_policy(driver)

        self.assertIs(factory, driver.async_playwright)
        for driver_type in (driver.Page, driver.BrowserContext):
            instance = driver_type()
            for method_name in ("expose_function", "expose_binding"):
                with self.subTest(driver_type=driver_type.__name__, method=method_name):
                    with self.assertRaisesRegex(
                        BrowserPolicyError,
                        "bindings are unavailable",
                    ):
                        await getattr(instance, method_name)("visibleName", lambda: None)


class BrowserIdentityPolicyTests(unittest.TestCase):
    @staticmethod
    def chromium_identity():
        return {
            "userAgent": (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.7883.0 Mobile Safari/537.36"
            ),
            "navigatorPlatform": "Linux armv81",
            "brands": [
                {"brand": "Chromium", "version": "151"},
                {"brand": "Not=A?Brand", "version": "99"},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": "151.0.7883.0"},
                {"brand": "Not=A?Brand", "version": "99.0.0.0"},
            ],
            "platform": "Android",
            "platformVersion": "16.0.0",
            "architecture": "",
            "model": "Pixel 8",
            "mobile": True,
            "bitness": "",
            "wow64": False,
        }

    def test_adds_matching_google_chrome_low_and_high_entropy_brands(self):
        identity = self.chromium_identity()

        override = _google_chrome_user_agent_override(identity)

        self.assertEqual(override["userAgent"], identity["userAgent"])
        self.assertEqual(override["platform"], identity["navigatorPlatform"])
        metadata = override["userAgentMetadata"]
        self.assertIn(
            {"brand": "Google Chrome", "version": "151"},
            metadata["brands"],
        )
        self.assertIn(
            {"brand": "Google Chrome", "version": "151.0.7883.0"},
            metadata["fullVersionList"],
        )
        self.assertEqual(metadata["model"], "Pixel 8")
        self.assertEqual(metadata["platformVersion"], "16.0.0")
        self.assertTrue(metadata["mobile"])

    def test_does_not_rewrite_an_already_branded_google_chrome(self):
        identity = self.chromium_identity()
        identity["brands"].append({"brand": "Google Chrome", "version": "151"})
        identity["fullVersionList"].append(
            {"brand": "Google Chrome", "version": "151.0.7883.0"}
        )

        self.assertIsNone(_google_chrome_user_agent_override(identity))

    def test_rewrites_model_even_when_google_chrome_is_already_branded(self):
        identity = self.chromium_identity()
        identity["brands"].append({"brand": "Google Chrome", "version": "151"})
        identity["fullVersionList"].append(
            {"brand": "Google Chrome", "version": "151.0.7883.0"}
        )

        override = _google_chrome_user_agent_override(identity, "Pixel 6 Pro")

        self.assertEqual(override["userAgentMetadata"]["model"], "Pixel 6 Pro")
        self.assertEqual(override["userAgent"], identity["userAgent"])

    def test_validates_public_identity_and_builds_pixel_6_pro_expectations(self):
        profile = _validated_footprint_identity(
            {
                "name": "pixel-6-pro",
                "model": "Pixel 6 Pro",
                "screen_width": 1440,
                "screen_height": 3120,
                "device_pixel_ratio": 3.5,
            },
            "pixel-6-pro",
        )

        self.assertEqual(
            _expected_device_metrics(profile),
            {
                "screenWidth": 412,
                "screenHeight": 892,
                "devicePixelRatio": 3.5,
            },
        )

    def test_footprint_verification_requires_headful_browser_insets(self):
        self.assertIn("screen.availHeight < screen.height", _VERIFY_MANAGED_BROWSER_IDENTITY)
        self.assertIn("innerHeight < screen.availHeight", _VERIFY_MANAGED_BROWSER_IDENTITY)
        self.assertIn("visualViewport.height <= innerHeight", _VERIFY_MANAGED_BROWSER_IDENTITY)

    def test_footprint_verification_allows_subpixel_viewport_rounding(self):
        self.assertIn(
            "visualViewport.height <= innerHeight + 1",
            _VERIFY_MANAGED_BROWSER_IDENTITY,
        )

    def test_footprint_verification_allows_one_sided_density_rounding(self):
        profile = _validated_footprint_identity(
            {
                "name": "pixel-9-pro-xl",
                "model": "Pixel 9 Pro XL",
                "screen_width": 1344,
                "screen_height": 2992,
                "device_pixel_ratio": 3.5,
            },
            "pixel-9-pro-xl",
        )

        self.assertEqual(
            _expected_device_metrics(profile),
            {
                "screenWidth": 384,
                "screenHeight": 855,
                "devicePixelRatio": 3.5,
            },
        )
        self.assertIn("screen.width >= expected.screenWidth", _VERIFY_MANAGED_BROWSER_IDENTITY)
        self.assertIn("screen.width <= expected.screenWidth + 1", _VERIFY_MANAGED_BROWSER_IDENTITY)
        self.assertIn("screen.height >= expected.screenHeight", _VERIFY_MANAGED_BROWSER_IDENTITY)
        self.assertIn("screen.height <= expected.screenHeight + 1", _VERIFY_MANAGED_BROWSER_IDENTITY)
        self.assertNotIn("screen.width === expected.screenWidth", _VERIFY_MANAGED_BROWSER_IDENTITY)

    def test_rejects_missing_or_mismatched_selected_identity(self):
        for profile in ({}, {"name": "pixel-6a"}):
            with self.subTest(profile=profile), self.assertRaises(BrowserPolicyError):
                _validated_footprint_identity(profile, "pixel-6-pro")

    def test_fails_closed_on_inconsistent_or_non_android_metadata(self):
        inconsistent = self.chromium_identity()
        inconsistent["brands"].append({"brand": "Google Chrome", "version": "151"})
        non_android = self.chromium_identity()
        non_android["platform"] = "Windows"

        for identity in (inconsistent, non_android):
            with self.subTest(identity=identity), self.assertRaises(BrowserPolicyError):
                _google_chrome_user_agent_override(identity)


class BrowserIdentityApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstraps_native_hints_in_a_closed_first_party_tab(self):
        session = session_for(MagicMock())
        startup_page = MagicMock()
        startup_page.evaluate = AsyncMock(side_effect=(None, None))
        bootstrap_page = MagicMock()
        bootstrap_page.goto = AsyncMock()
        bootstrap_page.evaluate = AsyncMock(
            side_effect=(BrowserIdentityPolicyTests.chromium_identity(), True)
        )
        bootstrap_page.close = AsyncMock()
        startup_page.context.new_page = AsyncMock(return_value=bootstrap_page)
        bootstrap_cdp_session = MagicMock()
        bootstrap_cdp_session.send = AsyncMock()
        bootstrap_cdp_session.detach = AsyncMock()
        target_cdp_session = MagicMock()
        target_cdp_session.send = AsyncMock()
        target_cdp_session.detach = AsyncMock()
        startup_page.context.new_cdp_session = AsyncMock(
            side_effect=(bootstrap_cdp_session, target_cdp_session)
        )

        await session._apply_page_identity(startup_page)

        bootstrap_page.goto.assert_awaited_once_with(
            "https://docs.aimlib.com/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        bootstrap_page.close.assert_awaited_once()
        bootstrap_cdp_session.send.assert_awaited_once()
        bootstrap_cdp_session.detach.assert_awaited_once()
        target_cdp_session.send.assert_awaited_once()
        target_cdp_session.detach.assert_not_awaited()
        self.assertEqual(startup_page.context.new_cdp_session.await_count, 2)
        self.assertEqual(bootstrap_page.evaluate.await_count, 2)

    async def test_bootstrap_fails_closed_when_secure_brand_check_fails(self):
        session = session_for(MagicMock())
        startup_page = MagicMock()
        startup_page.evaluate = AsyncMock(return_value=None)
        bootstrap_page = MagicMock()
        bootstrap_page.goto = AsyncMock()
        bootstrap_page.evaluate = AsyncMock(
            side_effect=(BrowserIdentityPolicyTests.chromium_identity(), False)
        )
        bootstrap_page.close = AsyncMock()
        startup_page.context.new_page = AsyncMock(return_value=bootstrap_page)
        bootstrap_cdp_session = MagicMock()
        bootstrap_cdp_session.send = AsyncMock()
        bootstrap_cdp_session.detach = AsyncMock()
        startup_page.context.new_cdp_session = AsyncMock(
            return_value=bootstrap_cdp_session
        )

        with self.assertRaisesRegex(
            BrowserPolicyError,
            "managed browser identity did not apply",
        ):
            await session._apply_page_identity(startup_page)

        bootstrap_cdp_session.detach.assert_awaited_once()
        bootstrap_page.close.assert_awaited_once()

    async def test_applies_and_retains_the_target_override_until_disconnect(self):
        session = session_for(MagicMock())
        page = MagicMock()
        page.evaluate = AsyncMock(
            side_effect=(BrowserIdentityPolicyTests.chromium_identity(), True)
        )
        cdp_session = MagicMock()
        cdp_session.send = AsyncMock()
        cdp_session.detach = AsyncMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        await session._apply_page_identity(page)
        await session._apply_page_identity(page)

        page.context.new_cdp_session.assert_awaited_once_with(page)
        cdp_session.send.assert_awaited_once()
        method, params = cdp_session.send.await_args.args
        self.assertEqual(method, "Emulation.setUserAgentOverride")
        self.assertIn(
            {"brand": "Google Chrome", "version": "151"},
            params["userAgentMetadata"]["brands"],
        )
        self.assertEqual(page.evaluate.await_count, 2)
        cdp_session.detach.assert_not_awaited()

        await session._disconnect()

        cdp_session.detach.assert_awaited_once()

    async def test_does_not_open_a_cdp_session_for_official_chrome(self):
        identity = BrowserIdentityPolicyTests.chromium_identity()
        identity["brands"].append({"brand": "Google Chrome", "version": "151"})
        identity["fullVersionList"].append(
            {"brand": "Google Chrome", "version": "151.0.7883.0"}
        )
        session = session_for(MagicMock())
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=identity)
        page.context.new_cdp_session = AsyncMock()

        await session._apply_page_identity(page)

        page.context.new_cdp_session.assert_not_awaited()

    async def test_applies_selected_model_and_validates_os_display_metrics(self):
        session = session_for(MagicMock())
        session.applied_footprint = "pixel-6-pro"
        session.fingerprint = {
            "name": "pixel-6-pro",
            "model": "Pixel 6 Pro",
            "screen_width": 1440,
            "screen_height": 3120,
            "device_pixel_ratio": 3.5,
        }
        page = MagicMock()
        page.evaluate = AsyncMock(
            side_effect=(BrowserIdentityPolicyTests.chromium_identity(), True)
        )
        cdp_session = MagicMock()
        cdp_session.send = AsyncMock()
        cdp_session.detach = AsyncMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        await session._apply_page_identity(page)

        self.assertEqual(cdp_session.send.await_count, 1)
        ua_call = cdp_session.send.await_args
        self.assertEqual(ua_call.args[0], "Emulation.setUserAgentOverride")
        self.assertEqual(
            ua_call.args[1]["userAgentMetadata"]["model"],
            "Pixel 6 Pro",
        )
        _, expected = page.evaluate.await_args.args
        self.assertEqual(
            expected,
            {
                "model": "Pixel 6 Pro",
                "screenWidth": 412,
                "screenHeight": 892,
                "devicePixelRatio": 3.5,
            },
        )


class ModelTests(unittest.TestCase):
    def test_device_and_proxy_mapping(self):
        device = Device(
            object(),
            {
                "device_id": "dev-1",
                "region": "uswest1",
                "carrier": "att",
                "proxy": {
                    "id": "proxy-1",
                    "url": "socks5h://example.test:1234",
                    "protocol": "socks5",
                    "status": "active",
                },
            },
        )
        self.assertEqual(device.id, "dev-1")
        self.assertEqual(device.proxy.id, "proxy-1")
        self.assertEqual(device.proxy.protocol, "socks5")

    def test_device_exposes_browser_and_lease_metadata(self):
        device = Device(
            object(),
            {
                "device_id": "dev-1",
                "browser": {"available": False},
                "lease": {"id": "lease-1", "ends_at": "2026-07-13T01:00:00Z"},
            },
        )
        self.assertFalse(device.browser_available)
        self.assertEqual(device.lease_id, "lease-1")
        self.assertEqual(device.lease_ends_at, "2026-07-13T01:00:00Z")

    def test_proxy_exposes_both_protocols_without_leaking_credentials_in_repr(self):
        proxy = Proxy(
            {
                "id": "proxy-1",
                "url": "socks5h://customer:secret@example.test:43117",
                "protocol": "socks5",
                "status": "active",
                "status_detail": "ready",
            }
        )
        self.assertEqual(proxy.http_url, "http://customer:secret@example.test:43117")
        self.assertEqual(proxy.socks5_url, "socks5://customer:secret@example.test:43117")
        self.assertEqual(proxy.socks5h_url, "socks5h://customer:secret@example.test:43117")
        self.assertEqual(proxy.protocols, ("http", "socks5"))
        self.assertEqual(proxy.status_detail, "ready")
        self.assertEqual(proxy.host, "example.test")
        self.assertEqual(proxy.port, 43117)
        self.assertNotIn("customer", repr(proxy))
        self.assertNotIn("secret", repr(proxy))


class OperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_returns_safe_customer_operation(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                200,
                {
                    "operation_id": "op-1",
                    "type": "ip_rotation",
                    "status": "succeeded",
                    "new_ip": "203.0.113.8",
                },
                path="/v1/operations/op-1",
            )
        )
        operations = _Operations(SimpleNamespace(_http=http))

        result = await operations.get("op-1")

        self.assertEqual(result["new_ip"], "203.0.113.8")
        http.get.assert_awaited_once_with("/v1/operations/op-1")

    async def test_wait_polls_until_terminal(self):
        http = MagicMock()
        http.get = AsyncMock(
            side_effect=(
                response(200, {"operation_id": "op-1", "status": "queued"}),
                response(200, {"operation_id": "op-1", "status": "succeeded"}),
            )
        )
        operations = _Operations(SimpleNamespace(_http=http))

        with patch("aimlib.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await operations.wait("op-1", timeout=10, poll_interval=0.1)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(http.get.await_count, 2)
        sleep.assert_awaited_once()

    async def test_wait_uses_a_distinct_client_timeout(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(200, {"operation_id": "op-1", "status": "running"})
        )
        operations = _Operations(SimpleNamespace(_http=http))

        with self.assertRaisesRegex(OperationTimeout, "polling timed out"):
            await operations.wait("op-1", timeout=0)


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_accepts_active_session_and_refreshes_metadata(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                200,
                {
                    "session_id": "session-1",
                    "status": "active",
                    "egress_ip": "203.0.113.9",
                    "expires_at": "2026-07-13T00:40:00Z",
                    "resolved_profile": {"name": "pixel-9"},
                },
            )
        )
        session = session_for(http)

        await session.wait_until_ready(timeout=1)

        self.assertEqual(session.status, "active")
        self.assertEqual(session.egress_ip, "203.0.113.9")
        self.assertEqual(session.expires_at, "2026-07-13T00:40:00Z")
        self.assertEqual(session.fingerprint, {"name": "pixel-9"})

    async def test_ready_rejects_replaced_session(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                200,
                {"session_id": "session-2", "status": "ready"},
            )
        )
        session = session_for(http)

        with self.assertRaisesRegex(SessionExpiredError, "replaced"):
            await session.wait_until_ready(timeout=1)

    async def test_ready_fails_closed_when_selected_identity_is_missing(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                200,
                {
                    "session_id": "session-1",
                    "status": "ready",
                    "desired_footprint": "pixel-6-pro",
                    "applied_footprint": "pixel-6-pro",
                    "resolved_profile": {"name": "pixel-6-pro"},
                },
            )
        )
        session = session_for(http)
        session.desired_footprint = "pixel-6-pro"

        with self.assertRaisesRegex(BrowserPolicyError, "footprint model is invalid"):
            await session.wait_until_ready(timeout=1)

    async def test_set_footprint_refreshes_identity_before_reconnect(self):
        http = MagicMock()
        http.post = AsyncMock(return_value=response(200, {"status": "accepted"}, "POST"))
        profile = {
            "name": "pixel-6-pro",
            "model": "Pixel 6 Pro",
            "screen_width": 1440,
            "screen_height": 3120,
            "device_pixel_ratio": 3.5,
        }
        http.get = AsyncMock(
            return_value=response(
                200,
                {
                    "session_id": "session-1",
                    "status": "ready",
                    "applied_footprint": "pixel-6-pro",
                    "resolved_profile": profile,
                },
            )
        )
        session = session_for(http)
        session._disconnect = AsyncMock()
        session.connect = AsyncMock()

        await session.set_footprint("pixel-6-pro", timeout=1)

        self.assertEqual(session.fingerprint, profile)
        session.connect.assert_awaited_once_with(timeout=1)

    async def test_connect_retries_and_stops_failed_playwright_instance(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                200,
                {"webSocketDebuggerUrl": "wss://browser.example.test/session-1/browser/test"},
                path="/session-1/json/version",
            )
        )
        session = session_for(http)
        session.wait_until_ready = AsyncMock()

        pw1 = MagicMock()
        pw1.stop = AsyncMock()
        pw1.chromium.connect_over_cdp = AsyncMock(side_effect=RuntimeError("connection timeout"))
        manager1 = MagicMock()
        manager1.start = AsyncMock(return_value=pw1)

        browser = MagicMock()
        browser.is_connected.return_value = True
        initial_page = MagicMock()
        initial_context = MagicMock()
        initial_context.pages = [initial_page]
        browser.contexts = [initial_context]
        pw2 = MagicMock()
        pw2.stop = AsyncMock()
        pw2.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        manager2 = MagicMock()
        manager2.start = AsyncMock(return_value=pw2)
        playwright_factory = MagicMock(side_effect=(manager1, manager2))
        session._apply_page_identity = AsyncMock()

        with (
            patch("aimlib._async_playwright", return_value=playwright_factory),
            patch("aimlib.asyncio.sleep", new=AsyncMock()),
        ):
            connected = await session.connect(timeout=10)

        self.assertIs(connected, browser)
        pw1.stop.assert_awaited_once()
        self.assertEqual(pw1.chromium.connect_over_cdp.await_count, 1)
        self.assertEqual(pw2.chromium.connect_over_cdp.await_count, 1)
        session._apply_page_identity.assert_awaited_once_with(initial_page)

    async def test_connect_surfaces_unavailable_browser_endpoint(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                503,
                {"error": "browser_unavailable", "message": "browser endpoint not reachable"},
                path="/session-1/json/version",
            )
        )
        session = session_for(http)
        session.wait_until_ready = AsyncMock()

        with patch("aimlib.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(BrowserUnavailableError, "did not become reachable"):
                await session.connect(timeout=10)

        self.assertEqual(http.get.await_count, 3)

    async def test_connect_reports_redacted_driver_failure_category(self):
        http = MagicMock()
        http.get = AsyncMock(
            return_value=response(
                200,
                {"webSocketDebuggerUrl": "wss://browser.invalid/browser/opaque"},
                path="/session-1/json/version",
            )
        )
        session = session_for(http)
        session.wait_until_ready = AsyncMock()
        manager = MagicMock()
        manager.start = AsyncMock(return_value=MagicMock())
        manager.start.return_value.chromium.connect_over_cdp = AsyncMock(
            side_effect=Exception(
                "WebSocket error: wss://browser.invalid/private-session 403 Forbidden"
            )
        )

        with (
            patch("aimlib._async_playwright", return_value=lambda: manager),
            patch("aimlib.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(SessionTimeout, r"\(browser_access_denied\)$") as caught:
                await session.connect(timeout=10)

        self.assertNotIn("private-session", str(caught.exception))

    async def test_connect_reports_discovery_timeout_without_transport_details(self):
        http = MagicMock()
        http.get = AsyncMock(
            side_effect=httpx.ReadTimeout(
                "timed out at https://browser.invalid/private-session/json/version"
            )
        )
        session = session_for(http)
        session.wait_until_ready = AsyncMock()

        with patch("aimlib.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(SessionTimeout, r"\(discovery_timeout\)$") as caught:
                await session.connect(timeout=10)

        self.assertNotIn("private-session", str(caught.exception))

    async def test_connect_is_idempotent_while_browser_is_connected(self):
        http = MagicMock()
        session = session_for(http)
        browser = MagicMock()
        browser.is_connected.return_value = True
        session.browser = browser
        session.wait_until_ready = AsyncMock()

        self.assertIs(await session.connect(), browser)
        session.wait_until_ready.assert_not_awaited()

    async def test_public_disconnect_only_closes_local_driver(self):
        http = MagicMock()
        http.delete = AsyncMock()
        session = session_for(http)
        session._disconnect = AsyncMock()

        await session.disconnect()

        session._disconnect.assert_awaited_once()
        http.delete.assert_not_awaited()

    async def test_new_page_applies_identity_before_returning_the_initial_tab(self):
        session = session_for(MagicMock())
        page = MagicMock()
        context = MagicMock()
        context.pages = [page]
        browser = MagicMock()
        browser.is_connected.return_value = True
        browser.contexts = [context]
        session.browser = browser
        session._apply_page_identity = AsyncMock()

        returned = await session.new_page()

        self.assertIs(returned, page)
        session._apply_page_identity.assert_awaited_once_with(page)

    async def test_context_entry_failure_requests_and_confirms_teardown(self):
        http = MagicMock()
        session = session_for(http)
        session.connect = AsyncMock(side_effect=SessionTimeout("connect failed"))
        session.stop = AsyncMock()

        with self.assertRaisesRegex(SessionTimeout, "connect failed"):
            await session.__aenter__()

        session.stop.assert_awaited_once()

    async def test_context_entry_preserves_connection_failure_when_cleanup_also_fails(self):
        http = MagicMock()
        session = session_for(http)
        session.connect = AsyncMock(side_effect=BrowserUnavailableError("browser endpoint unavailable"))
        session.stop = AsyncMock(side_effect=SessionTimeout("teardown timeout"))

        with self.assertRaisesRegex(
            AimlibError,
            "browser connection failed: browser endpoint unavailable; automatic teardown was not confirmed",
        ):
            await session.__aenter__()

        session.stop.assert_awaited_once()

    async def test_context_exit_preserves_operation_failure_when_cleanup_also_fails(self):
        http = MagicMock()
        session = session_for(http)
        session.stop = AsyncMock(side_effect=SessionTimeout("teardown timeout"))
        operation_error = RuntimeError("page failed")

        with self.assertRaisesRegex(AimlibError, "page failed.*teardown was not confirmed"):
            await session.__aexit__(RuntimeError, operation_error, None)

    async def test_stop_surfaces_server_failure(self):
        http = MagicMock()
        http.delete = AsyncMock(return_value=response(500, {"error": "unexpected"}, "DELETE"))
        session = session_for(http)
        session._disconnect = AsyncMock()

        with self.assertRaises(AimlibError):
            await session.stop()

        session._disconnect.assert_awaited_once()

    async def test_stop_is_idempotent_when_session_is_already_gone(self):
        http = MagicMock()
        http.delete = AsyncMock(return_value=response(404, {"error": "session_not_found"}, "DELETE"))
        session = session_for(http)
        session._disconnect = AsyncMock()

        await session.stop()

        self.assertEqual(session.status, "gone")

    async def test_stop_waits_for_server_confirmation(self):
        http = MagicMock()
        http.delete = AsyncMock(return_value=response(200, {"status": "stopping"}, "DELETE"))
        session = session_for(http)
        session._disconnect = AsyncMock()
        session.wait_until_stopped = AsyncMock()

        await session.stop(timeout=9)

        session.wait_until_stopped.assert_awaited_once_with(9)

    async def test_invalid_footprint_tears_down_new_session(self):
        http = MagicMock()
        http.post = AsyncMock(
            side_effect=(
                response(
                    201,
                    {
                        "session_id": "session-1",
                        "status": "provisioning",
                        "connect_url": "wss://browser.example.test/session-1",
                    },
                    "POST",
                ),
                response(
                    400,
                    {"error": "footprint_not_clean", "message": "not available"},
                    "POST",
                ),
            )
        )
        ai = MagicMock()
        ai._http = http
        device = Device(ai, {"device_id": "dev-1"})

        with patch.object(BrowserSession, "stop", new=AsyncMock()) as stop:
            with self.assertRaisesRegex(AimlibError, "not available"):
                await device.browser(footprint="wrong-gpu")

        stop.assert_awaited_once()

    async def test_atomic_footprint_create_does_not_use_legacy_second_request(self):
        http = MagicMock()
        http.post = AsyncMock(
            return_value=response(
                201,
                {
                    "session_id": "session-1",
                    "status": "provisioning",
                    "connect_url": "wss://browser.example.test/session-1",
                    "desired_footprint": "pixel-9",
                },
                "POST",
            )
        )
        ai = MagicMock()
        ai._http = http
        device = Device(ai, {"device_id": "dev-1"})

        session = await device.browser(footprint="pixel-9")

        self.assertEqual(session.desired_footprint, "pixel-9")
        http.post.assert_awaited_once_with(
            "/v1/devices/dev-1/browser",
            json={"footprint": "pixel-9"},
        )


if __name__ == "__main__":
    unittest.main()
