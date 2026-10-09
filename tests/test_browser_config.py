from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.browser import create_browser_pool, prepare_browser_executable
from core.crawler import create_crawler, create_proxy_configuration
from core.models import Proxy


class WindowsBrowserTests(unittest.TestCase):
    def test_workaround_preserves_original_and_binary_size(self):
        dependency = (
            b'<dependency><dependentAssembly><assemblyIdentity type="win32" '
            b'name="mozglue" version="1.0.0.0" /></dependentAssembly></dependency>'
        )
        data = b"MZ-test-prefix<assembly>" + dependency + b"</assembly>test-suffix"
        with TemporaryDirectory() as directory:
            original = Path(directory) / "camoufox.exe"
            original.write_bytes(data)
            original.with_name("mozglue.dll").write_bytes(b"test-dll")
            with patch("core.browser.sys.platform", "win32"):
                target = Path(prepare_browser_executable(str(original)))
                self.assertEqual(prepare_browser_executable(str(original)), str(target))
            self.assertEqual(original.read_bytes(), data)
            patched = target.read_bytes()
            self.assertEqual(len(patched), len(data))
            self.assertNotIn(dependency, patched)
            self.assertTrue(patched.startswith(b"MZ-test-prefix<assembly>"))
            self.assertTrue(patched.endswith(b"</assembly>test-suffix"))

    def test_fixed_vendor_binary_is_used_without_changes(self):
        with TemporaryDirectory() as directory:
            original = Path(directory) / "camoufox.exe"
            original.write_bytes(b"MZ-vendor-fixed-test")
            with patch("core.browser.sys.platform", "win32"):
                self.assertEqual(prepare_browser_executable(str(original)), str(original))
            self.assertFalse(original.with_name("camoufox-windows.exe").exists())

    def test_other_platforms_do_not_read_or_patch_browser(self):
        with patch("core.browser.sys.platform", "linux"):
            self.assertIsNone(prepare_browser_executable())
            self.assertEqual(prepare_browser_executable("/custom/browser"), "/custom/browser")


class BrowserConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_rotation_creates_separate_contexts(self):
        first_page = SimpleNamespace(on=Mock())
        second_page = SimpleNamespace(on=Mock())
        first_context = SimpleNamespace(new_page=AsyncMock(return_value=first_page))
        second_context = SimpleNamespace(new_page=AsyncMock(return_value=second_page))
        browser = SimpleNamespace(
            contexts=[], browser_type=SimpleNamespace(name="firefox"),
            new_context=AsyncMock(side_effect=[first_context, second_context]),
        )
        pool = create_browser_pool()
        plugin = pool.plugins[0]
        plugin._playwright = object()
        configuration = create_proxy_configuration([
            Proxy("http", "first.test", 8080, "user", "pass:one"),
            Proxy("http", "second.test", 8080, "user", "pass:two"),
        ])
        first_proxy = await configuration.new_proxy_info(session_id="first", request=None, proxy_tier=None)
        second_proxy = await configuration.new_proxy_info(session_id="second", request=None, proxy_tier=None)

        with patch("core.browser.prepare_browser_executable", return_value="test.exe"), patch(
            "core.browser.AsyncNewBrowser", AsyncMock(return_value=browser),
        ):
            controller = await plugin.new_browser()
        await controller.new_page(proxy_info=first_proxy)
        controller._on_page_close(first_page)
        await controller.new_page(proxy_info=second_proxy)

        self.assertEqual(browser.new_context.await_count, 2)
        calls = browser.new_context.await_args_list
        self.assertEqual(calls[0].kwargs["proxy"]["server"], "http://first.test:8080")
        self.assertEqual(calls[1].kwargs["proxy"]["server"], "http://second.test:8080")
        self.assertEqual(calls[0].kwargs["proxy"]["password"], "pass:one")
        self.assertIsNone(calls[0].kwargs["extra_http_headers"])

    async def test_explicit_empty_proxy_list_is_rejected(self):
        with self.assertRaises(ValueError):
            create_proxy_configuration([])
        with self.assertRaises(ValueError):
            create_proxy_configuration(None)

    async def test_crawler_options_reach_camoufox(self):
        proxy = Proxy("http", "proxy.test", 8080, "user", "pass")
        with patch("core.crawler.create_browser_pool", wraps=create_browser_pool) as factory:
            create_crawler(proxies=[proxy], os="windows", locale="en-US", geoip=True)
        self.assertNotIn("proxy", factory.call_args.kwargs)
        self.assertEqual(factory.call_args.kwargs["geoip"], True)
        self.assertEqual(factory.call_args.kwargs["os"], "windows")
        self.assertEqual(factory.call_args.kwargs["locale"], "en-US")

    async def test_geoip_uses_account_proxy_at_browser_launch(self):
        proxies = [
            Proxy("http", "first.test", 8080, "user", "pass"),
            Proxy("http", "second.test", 8080, "user", "pass"),
        ]
        with patch("core.crawler.create_browser_pool") as factory:
            create_crawler(proxies=proxies, geoip=True)
        self.assertTrue(factory.call_args.kwargs["geoip"])
        self.assertNotIn("proxy", factory.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
