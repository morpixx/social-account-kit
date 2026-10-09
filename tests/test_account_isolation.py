import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from crawlee import Request
from crawlee.browsers import BrowserPool
from crawlee.storage_clients import MemoryStorageClient

from core.browser import create_browser_pool
from core.crawler import create_crawler, create_proxy_configuration
from core.models import Proxy


class AccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_is_kept_on_retry_and_not_given_to_another_account(self):
        proxies = [Proxy("http", "first.test", 8080, "user", "pass"),
                   Proxy("http", "second.test", 8080, "user", "pass")]
        configuration = create_proxy_configuration(proxies)
        first = Request.from_url("https://example.invalid", unique_key="account:one")
        second = Request.from_url("https://example.invalid", unique_key="account:two")
        third = Request.from_url("https://example.invalid", unique_key="account:three")

        first_proxy = await configuration.new_proxy_info("session:one", first, None)
        retry_proxy = await configuration.new_proxy_info("session:retry", first, None)
        second_proxy = await configuration.new_proxy_info("session:two", second, None)
        self.assertEqual(first_proxy.url, retry_proxy.url)
        self.assertNotEqual(first_proxy.url, second_proxy.url)
        with self.assertRaises(ValueError):
            await configuration.new_proxy_info("session:three", third, None)
        self.assertEqual(await configuration.new_url(request=first), first_proxy.url)

    async def test_duplicate_proxy_addresses_are_rejected(self):
        with self.assertRaises(ValueError):
            create_proxy_configuration([
                Proxy("http", "HOST.test", 8080, "user", "pass"),
                Proxy("http", "host.test", 8080, "user", "pass"),
            ])

    async def test_each_parallel_browser_receives_its_own_launch_proxy(self):
        pool = create_browser_pool()
        plugin = pool.plugins[0]
        plugin._playwright = object()
        configuration = create_proxy_configuration([
            Proxy("http", "first.test", 8080, "user", "pass"),
            Proxy("http", "second.test", 8080, "user", "pass"),
        ])
        first = await configuration.new_proxy_info("first", None, None)
        second = await configuration.new_proxy_info("second", None, None)
        launches = []

        async def launch(playwright, **options):
            await asyncio.sleep(0)
            launches.append(options["proxy"]["server"])
            return SimpleNamespace(contexts=[], browser_type=SimpleNamespace(name="firefox"))

        async def new_page(**options):
            await asyncio.sleep(0)
            await plugin.new_browser()

        with patch.object(BrowserPool, "new_page", side_effect=new_page), patch(
            "core.browser.prepare_browser_executable", return_value="test.exe",
        ), patch("core.browser.AsyncNewBrowser", side_effect=launch):
            await asyncio.gather(pool.new_page(proxy_info=first), pool.new_page(proxy_info=second))
        self.assertCountEqual(launches, ["http://first.test:8080", "http://second.test:8080"])

    async def test_page_without_proxy_is_rejected_before_launch(self):
        pool = create_browser_pool()
        with patch("core.browser.AsyncNewBrowser", new_callable=AsyncMock) as launch:
            with self.assertRaises(ValueError):
                await pool.new_page()
            launch.assert_not_awaited()

    async def test_persistent_options_and_session_reuse_are_rejected(self):
        for options in ({"enable_cache": True}, {"retire_browser_after_page_count": 2},
                        {"max_open_pages_per_browser": 2}, {"max_open_pages_per_browser": True},
                        {"extra_options": {"persistent_context": True}},
                        {"extra_options": {"enable_cache": True}},
                        {"extra_options": {"user_data_dir": "test-profile"}}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    create_browser_pool(**options)
        with self.assertRaises(ValueError):
            create_crawler(proxies=[Proxy("http", "host.test", 8080, "user", "pass")],
                           session_max_usage_count=2)

    async def test_cache_and_proxy_preferences_cannot_be_overridden(self):
        pool = create_browser_pool(extra_options={"firefox_user_prefs": {
            "browser.cache.disk.enable": True, "network.proxy.allow_hijacking_localhost": False,
            "custom.test.pref": 42,
        }})
        prefs = pool.plugins[0].browser_launch_options["firefox_user_prefs"]
        self.assertFalse(prefs["browser.cache.disk.enable"])
        self.assertFalse(prefs["browser.cache.memory.enable"])
        self.assertFalse(prefs["network.http.use-cache"])
        self.assertTrue(prefs["network.proxy.allow_hijacking_localhost"])
        self.assertEqual(prefs["custom.test.pref"], 42)

    async def test_crawler_storage_is_memory_only(self):
        with patch("core.crawler.PlaywrightCrawler") as factory:
            create_crawler(proxies=[Proxy("http", "host.test", 8080, "user", "pass")])
        self.assertIsInstance(factory.call_args.kwargs["storage_client"], MemoryStorageClient)


if __name__ == "__main__":
    unittest.main()
