import asyncio
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from core.models import MailAccount, Proxy
from core.registration import generate_profile
from core.storage import AccountStorage
from marketplaces.lzt import LztMarketClient, LztMarketError


class LztMarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = AccountStorage(self.directory.name)
        self.proxy = Proxy("http", "proxy.test", 8080, "test-user", "test:password")
        self.cookies = [{"name": "session", "value": "test-session", "domain": ".tiktok.com", "path": "/",
                         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}]
        self.mail = MailAccount("test@example.com", "test:email-password")
        self.storage.reserve_proxy("account-one", [self.proxy])
        self.storage.save_fingerprint("account-one", {"config": {"audio:seed": 123}})
        self.storage.save_account("account-one", self.cookies, self.proxy, mail_account=self.mail)
        self.requests = []
        self.handler = self.success
        self.market = LztMarketClient("test-token", timeout=1)
        await self.market.client.aclose()
        self.market.client = httpx.AsyncClient(base_url="https://api.lzt.market",
            headers={"Authorization": "Bearer test-token"}, transport=httpx.MockTransport(self.respond), trust_env=False)
        self.addAsyncCleanup(self.market.close)

    def respond(self, request):
        self.requests.append(request)
        return self.handler(request)

    def success(self, request):
        if request.url.path == "/me":
            return httpx.Response(200, json={"user": {"balance": "12.34"}})
        state = "awaiting" if request.url.path == "/item/add" else "active"
        return httpx.Response(200, json={"status": "ok", "item": {"item_id": 123, "item_state": state}})

    async def publish(self, **options):
        return await self.market.publish_saved_account(self.storage, "account-one", title="Created account",
                                                      price=10, **options)

    async def test_saved_account_transfers_cookies_proxy_and_private_email(self):
        item = await self.publish(login="test-login", password="test-login-password")
        self.assertEqual(item["item_id"], 123)
        self.assertEqual([request.url.path for request in self.requests], ["/item/add", "/123/goods/check"])
        create, check = [json.loads(request.content) for request in self.requests]
        self.assertEqual(create["item_origin"], "autoreg")
        self.assertEqual(create["category_id"], 20)
        self.assertEqual(create["email_login_data"], "test@example.com:test:email-password")
        self.assertEqual(create["description"], "")
        self.assertEqual(json.loads(create["information"].split("\n", 1)[1]),
                         {"scheme": "http", "host": "proxy.test", "port": 8080,
                          "username": "test-user", "password": "test:password"})
        self.assertEqual(check["login"], "test-login")
        self.assertEqual(check["password"], "test-login-password")
        self.assertEqual(check["email_type"], "autoreg")
        self.assertEqual(json.loads(check["extra"]["cookies"]), self.cookies)
        self.assertEqual(check["extra"]["proxy"], "proxy.test:8080:test-user:test:password")
        self.assertTrue(check["extra"]["cookie_login"])
        for request in self.requests:
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            self.assertEqual(request.url.host, "api.lzt.market")
            self.assertEqual(request.url.query, b"")
            self.assertNotIn("fingerprint", json.loads(request.content))
        saved = self.storage.load_account("account-one")
        self.assertEqual(saved.marketplace["state"], "published")
        self.assertEqual(saved.marketplace["item_id"], 123)
        self.assertEqual(saved.fingerprint, {"config": {"audio:seed": 123}})
        self.assertEqual(saved.mail_account, self.mail)

    async def test_buyer_proxy_preserves_private_note_and_stays_out_of_public_description(self):
        await self.publish(information="Private instructions", description="Public description")
        create = json.loads(self.requests[0].content)
        self.assertEqual(create["description"], "Public description")
        self.assertEqual(create["title"], "Created account")
        self.assertTrue(create["information"].startswith("Private instructions\n\nAccount proxy (JSON):\n"))
        self.assertNotIn(self.proxy.password, create["description"])
        self.assertNotIn("fingerprint", create["information"])
        saved = self.storage.load_account("account-one")
        self.assertEqual(saved.marketplace["settings"]["information"], create["information"])

    async def test_each_buyer_receives_the_proxy_of_the_selected_account(self):
        await self.publish()
        proxy = Proxy("http", "another.proxy.test", 8081, "another-user", "another-password")
        self.storage.reserve_proxy("account-two", [proxy])
        self.storage.save_fingerprint("account-two", {"config": {"audio:seed": 456}})
        self.storage.save_account("account-two", self.cookies, proxy)
        def second_sale(request):
            state = "awaiting" if request.url.path == "/item/add" else "active"
            return httpx.Response(200, json={"item": {"item_id": 456, "item_state": state}})
        self.handler = second_sale
        await self.market.publish_saved_account(self.storage, "account-two", title="Second account", price=10)
        create = json.loads(self.requests[2].content)
        buyer_proxy = json.loads(create["information"].split("\n", 1)[1])
        self.assertEqual(buyer_proxy, {"scheme": "http", "host": proxy.host, "port": proxy.port,
                                      "username": proxy.username, "password": proxy.password})
        self.assertEqual(json.loads(self.requests[3].content)["extra"]["proxy"],
                         "another.proxy.test:8081:another-user:another-password")

    async def test_registration_profile_supplies_login_password_and_requires_confirmation(self):
        account_id = "registered-account"
        proxy = Proxy("http", "proxy.test", 8081, "test-user", "test-password")
        profile = generate_profile("tiktok")
        self.storage.reserve_proxy(account_id, [proxy])
        self.storage.save_fingerprint(account_id, {"config": {"seed": 123}})
        self.storage.reserve_profile(account_id, profile, MailAccount("another@example.com", "test-password"))
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            await self.market.publish_saved_account(self.storage, account_id, title="Created account", price=10)
        self.assertEqual(self.requests, [])
        self.storage.save_account(account_id, self.cookies, proxy, registration_complete=True, platform_user_id="123")
        with self.assertRaisesRegex(ValueError, "category"):
            await self.market.publish_saved_account(self.storage, account_id, title="Created account", price=10, category_id=10)
        self.assertEqual(self.requests, [])
        await self.market.publish_saved_account(self.storage, account_id, title="Created account", price=10)
        payload = json.loads(self.requests[1].content)
        self.assertEqual(payload["login"], profile.username)
        self.assertEqual(payload["password"], profile.password)

    async def test_check_retry_reuses_created_item(self):
        failed = False
        def handler(request):
            nonlocal failed
            if request.url.path.endswith("/goods/check") and not failed:
                failed = True
                return httpx.Response(400, json={"errors": ["retry_request"]})
            return self.success(request)
        self.handler = handler
        with self.assertRaises(LztMarketError):
            await self.publish()
        self.assertEqual(self.storage.load_account("account-one").marketplace["state"], "awaiting")
        await self.publish()
        self.assertEqual(sum(request.url.path == "/item/add" for request in self.requests), 1)
        self.assertEqual(sum(request.url.path == "/123/goods/check" for request in self.requests), 2)

    async def test_ambiguous_creation_is_not_repeated(self):
        def timeout(request):
            raise httpx.ReadTimeout("test timeout", request=request)
        self.handler = timeout
        with self.assertRaises(LztMarketError) as result:
            await self.publish()
        self.assertTrue(result.exception.result_unknown)
        self.assertEqual(self.storage.load_account("account-one").marketplace["state"], "unknown")
        with self.assertRaises(RuntimeError):
            await self.publish()
        self.assertEqual(len(self.requests), 1)

    async def test_missing_item_id_does_not_create_another_listing(self):
        self.handler = lambda request: httpx.Response(200, json={"status": "ok"})
        with self.assertRaises(LztMarketError):
            await self.publish()
        with self.assertRaises(RuntimeError):
            await self.publish()
        self.assertEqual(len(self.requests), 1)

    async def test_published_account_is_only_read_on_repeat(self):
        await self.publish()
        await self.publish()
        self.assertEqual([(request.method, request.url.path) for request in self.requests],
                         [("POST", "/item/add"), ("POST", "/123/goods/check"), ("GET", "/123")])

    async def test_older_published_listing_is_read_without_editing_or_creating_duplicate(self):
        settings = self.market._listing_payload("Created account", 10, 20, "rub", "", "Old private note", 86400)
        self.storage.begin_sale("account-one", settings)
        self.storage.save_sale("account-one", 123, "published")
        await self.publish(information="Old private note")
        self.assertEqual([(request.method, request.url.path) for request in self.requests], [("GET", "/123")])
        self.assertEqual(self.storage.load_account("account-one").marketplace["settings"], settings)

    async def test_different_sale_settings_are_rejected(self):
        await self.publish()
        with self.assertRaises(ValueError):
            await self.market.publish_saved_account(self.storage, "account-one", title="Changed", price=20)
        self.assertEqual(len(self.requests), 2)

    async def test_instagram_does_not_receive_tiktok_cookie_flag(self):
        await self.publish(category_id=10, mail_account=MailAccount("native@example.com", "test-pass"), email_type="native")
        payload = json.loads(self.requests[1].content)
        self.assertNotIn("cookie_login", payload["extra"])
        self.assertEqual(payload["email_type"], "native")
        self.assertEqual(payload["email_login_data"], "native@example.com:test-pass")

    async def test_balance_preserves_numeric_precision(self):
        self.handler = lambda request: httpx.Response(200, content=b'{"user":{"balance":12.34567890123456789}}')
        self.assertEqual(await self.market.get_balance(), Decimal("12.34567890123456789"))

    async def test_rate_limit_exposes_wait_without_retry_or_secret_in_message(self):
        self.handler = lambda request: httpx.Response(429, headers={"Retry-After": "24"},
                                                     json={"errors": ["test-secret-value"]})
        with self.assertRaises(LztMarketError) as result:
            await self.publish()
        self.assertEqual(result.exception.status_code, 429)
        self.assertEqual(result.exception.retry_after, 24)
        self.assertNotIn("test-secret-value", str(result.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.storage.load_account("account-one").marketplace["state"], "failed")

    async def test_invalid_inputs_never_create_a_listing(self):
        for options in ({"login": "test-login"}, {"category_id": True}, {"email_type": "wrong"}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    await self.publish(**options)
        for price in (True, float("nan"), float("inf"), 0):
            with self.assertRaises(ValueError):
                await self.market.add_account(title="Test", price=price)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.storage.load_account("account-one").marketplace, {})

    async def test_wrong_response_account_is_not_marked_published(self):
        def handler(request):
            if request.url.path.endswith("/goods/check"):
                return httpx.Response(200, json={"item": {"item_id": 456, "item_state": "active"}})
            return self.success(request)
        self.handler = handler
        with self.assertRaises(LztMarketError):
            await self.publish()
        self.assertEqual(self.storage.load_account("account-one").marketplace["state"], "check_unknown")

    async def test_non_http_market_proxy_is_rejected(self):
        proxy = Proxy("socks5", "proxy.test", 1080, "", "")
        with self.assertRaises(ValueError):
            await self.market.check_account(123, cookies=self.cookies, proxy=proxy)
        self.assertEqual(self.requests, [])

    async def test_oversized_response_stops_stream(self):
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b"a" * 65536
                yield b"b" * 65536
                raise AssertionError("Should not read beyond the response limit")
            async def aclose(self):
                self.closed = True
        stream = Stream()
        self.market.max_response_bytes = 70000
        self.handler = lambda request: httpx.Response(200, stream=stream)
        with self.assertRaises(LztMarketError):
            await self.market.get_profile()
        self.assertTrue(stream.closed)

    async def test_total_timeout_closes_slow_stream(self):
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                await asyncio.sleep(1)
                yield b'{}'
            async def aclose(self):
                self.closed = True
        stream = Stream()
        self.market.timeout = 0.01
        self.handler = lambda request: httpx.Response(200, stream=stream)
        with self.assertRaises(LztMarketError):
            await self.market.get_profile()
        self.assertTrue(stream.closed)

    async def test_context_manager_closes_client_after_error(self):
        with self.assertRaises(RuntimeError):
            async with self.market:
                raise RuntimeError("test failure")
        self.assertTrue(self.market.client.is_closed)


if __name__ == "__main__":
    unittest.main()
