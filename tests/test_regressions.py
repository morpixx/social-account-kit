from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import unittest

import httpx

from core.mail import extract_verification_code, get_verification_code, parse_letter_date
from core.models import MailAccount, MailLetter, Proxy
from core.proxy import load_proxies, parse_proxy
from mail.notletters import NotLettersClient


class MailTests(unittest.TestCase):
    def test_code_is_not_part_of_long_number(self):
        self.assertIsNone(extract_verification_code("Order 123456789"))

    def test_code_is_selected_by_context(self):
        text = "Order 123456; verification code: 654321"
        self.assertEqual(extract_verification_code(text), "654321")

    def test_russian_code_and_ambiguous_numbers(self):
        self.assertEqual(extract_verification_code("Ваш код подтверждения: 654321"), "654321")
        self.assertIsNone(extract_verification_code("123456 and 654321"))

    def test_newer_news_does_not_hide_code(self):
        letters = [
            MailLetter("old", "service@test.invalid", "Login", "Code: 654321", 10),
            MailLetter("new", "news@test.invalid", "News", "No code", 20),
        ]
        self.assertEqual(get_verification_code(letters), "654321")

    def test_old_and_unrelated_codes_are_excluded(self):
        letters = [
            MailLetter("old", "service@test.invalid", "Login", "Code: 111111", 10),
            MailLetter("used", "service@test.invalid", "Login", "Code: 222222", 20),
            MailLetter("right", "service@test.invalid", "Login", "Code: 654321", 30),
            MailLetter("other", "shop@test.invalid", "Login", "Code: 333333", 40),
        ]
        result = get_verification_code(
            letters, sender="service@test.invalid", subject="Login",
            after_date=10, ignored_ids={"used"},
        )
        self.assertEqual(result, "654321")
        self.assertIsNone(get_verification_code(letters, after_date=40))

    def test_empty_inbox(self):
        self.assertIsNone(get_verification_code([]))

    def test_numeric_and_iso_dates_share_unix_seconds(self):
        self.assertEqual(parse_letter_date(1_791_115_200), parse_letter_date(1_791_115_200_000))
        self.assertEqual(parse_letter_date("2026-10-04T12:00:00Z"), 1_791_115_200)
        self.assertEqual(parse_letter_date("1791115200000"), 1_791_115_200)
        for date in (True, float("nan"), float("inf"), "2026-10-04T12:00:00"):
            with self.assertRaises(ValueError):
                parse_letter_date(date)


class ProxyTests(unittest.TestCase):
    def test_invalid_address_and_scheme(self):
        for line in (":8080:user:pass", "garbage://host:8080:user:pass", "bad host:8080:user:pass"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    parse_proxy(line)

    def test_valid_proxy_and_password_with_colons(self):
        proxy = parse_proxy("HTTPS://host.test:8080:user:pass:with:colon")
        self.assertEqual(proxy.scheme, "https")
        self.assertEqual(proxy.password, "pass:with:colon")

    def test_invalid_ports(self):
        for port in ("0", "65536", "abc"):
            with self.assertRaises(ValueError):
                parse_proxy(f"host.test:{port}:user:pass")

    def test_socks5_authentication_is_rejected_before_browser_launch(self):
        with self.assertRaises(ValueError):
            parse_proxy("socks5://host.test:1080:user:pass")
        proxy = parse_proxy("socks5://host.test:1080::")
        self.assertEqual(proxy.username, "")

    def test_empty_or_broken_file_does_not_allow_direct_connection(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.txt"
            for contents in ("", "# comment\n", "invalid\n"):
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_proxies(path)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.api = NotLettersClient("test-token")
        await self.api.close()
        self.api.client = httpx.AsyncClient(
            base_url=self.api.base_url,
            transport=httpx.MockTransport(self.respond),
        )

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"data": {"balance": 1.25}})
        if request.url.path == "/v1/change-password":
            return httpx.Response(200, json={"code": 200, "data": "ok"})
        return httpx.Response(200, json={"data": ["test@example.invalid:pass:with:colon"]})

    async def asyncTearDown(self):
        await self.api.close()

    async def test_fractional_or_boolean_count_never_sends_purchase(self):
        for count in (1.5, True, 0, -1):
            with self.assertRaises(ValueError):
                await self.api.buy_emails(count)
        self.assertEqual(self.requests, [])

    async def test_removed_email_type_stays_removed(self):
        with self.assertRaises(ValueError):
            await self.api.buy_emails(1, type_email=2)
        self.assertEqual(self.requests, [])

    async def test_balance_and_purchase(self):
        self.assertEqual(await self.api.get_balance(), 1.25)
        accounts = await self.api.buy_emails(1)
        self.assertEqual(accounts[0].password, "pass:with:colon")

    async def test_iso_dates_are_compared_in_the_same_timezone(self):
        await self.api.close()
        letters = [
            {"id": "old", "sender": "service@test.invalid", "subject": "Login",
             "letter": {"text": "Code: 111111"}, "date": "2026-10-04T15:00:00+04:00"},
            {"id": "new", "sender": "service@test.invalid", "subject": "Login",
             "letter": {"text": "Code: 654321"}, "date": "2026-10-04T12:00:00Z"},
        ]
        self.api.client = httpx.AsyncClient(
            base_url=self.api.base_url,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {"letters": letters}})),
        )
        result = await self.api.get_letters(MailAccount("test@example.invalid", "pass"))
        self.assertEqual(get_verification_code(result), "654321")

    async def test_password_changes_only_on_success(self):
        account = MailAccount("test@example.invalid", "old")
        self.assertEqual(await self.api.change_password(account, "new"), "ok")
        self.assertEqual(account.password, "new")
        await self.api.close()
        self.api.client = httpx.AsyncClient(
            base_url=self.api.base_url,
            transport=httpx.MockTransport(lambda request: httpx.Response(403)),
        )
        with self.assertRaises(httpx.HTTPStatusError):
            await self.api.change_password(account, "wrong")
        self.assertEqual(account.password, "new")

    async def test_password_is_not_in_model_repr(self):
        self.assertNotIn("secret-password", repr(MailAccount("test@example.invalid", "secret-password")))
        self.assertNotIn("secret-password", repr(Proxy("http", "host.test", 8080, "user", "secret-password")))

    async def test_context_manager_closes_client_after_error(self):
        api = NotLettersClient("test-token")
        with self.assertRaises(RuntimeError):
            async with api:
                raise RuntimeError("test failure")
        self.assertTrue(api.client.is_closed)
        with self.assertRaises(RuntimeError):
            async with api:
                pass

    async def test_network_settings_do_not_inherit_environment(self):
        from unittest.mock import patch
        with patch("mail.notletters.httpx.AsyncClient") as factory:
            NotLettersClient("test-token", proxy="http://proxy.test:8080", max_connections=3)
        options = factory.call_args.kwargs
        self.assertFalse(options["trust_env"])
        self.assertFalse(options["follow_redirects"])
        self.assertEqual(options["proxy"], "http://proxy.test:8080")
        self.assertEqual(options["limits"].max_connections, 3)

    async def test_invalid_resource_limits_are_rejected(self):
        for options in ({"timeout": float("nan")}, {"timeout": 0}, {"timeout": True},
                        {"max_connections": 1.5}, {"max_connections": True},
                        {"max_response_bytes": 0}):
            with self.assertRaises(ValueError):
                NotLettersClient("test-token", **options)

    async def test_oversized_stream_is_stopped_and_closed(self):
        class Body(httpx.AsyncByteStream):
            chunks = 0
            closed = False

            async def __aiter__(self):
                for index in range(5):
                    self.chunks += 1
                    yield b"x" * 65536

            async def aclose(self):
                self.closed = True

        body = Body()
        await self.api.close()
        self.api.max_response_bytes = 65536
        self.api.client = httpx.AsyncClient(
            base_url=self.api.base_url,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)),
        )
        with self.assertRaises(ValueError):
            await self.api.get_balance()
        self.assertEqual(body.chunks, 2)
        self.assertTrue(body.closed)

    async def test_total_timeout_stops_slow_stream_and_closes_response(self):
        class Body(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                await asyncio.sleep(1)
                yield b'{}'

            async def aclose(self):
                self.closed = True

        body = Body()
        await self.api.close()
        self.api.timeout = 0.02
        self.api.client = httpx.AsyncClient(
            base_url=self.api.base_url,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)),
        )
        with self.assertRaises(TimeoutError):
            await self.api.get_balance()
        self.assertTrue(body.closed)

    async def test_ambiguous_purchase_response_is_not_retried(self):
        await self.api.close()
        requests = []

        def respond(request):
            requests.append(request)
            raise httpx.ReadTimeout("test timeout", request=request)

        self.api.client = httpx.AsyncClient(
            base_url=self.api.base_url, transport=httpx.MockTransport(respond),
        )
        with self.assertRaises(httpx.ReadTimeout):
            await self.api.buy_emails(1)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
