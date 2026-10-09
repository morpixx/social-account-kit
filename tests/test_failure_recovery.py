import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.models import MailAccount, Proxy
from core.registration import (ManualCheckRequired, RegistrationError, generate_profile,
                               confirm_saved_account, register_account, wait_registered)
from core.session import save_browser_session
from core.storage import AccountStorage
from mail.notletters import NotLettersClient
from marketplaces.lzt import LztMarketClient, LztMarketError


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = AccountStorage(self.directory.name)
        self.proxy = Proxy("http", "proxy.test", 8080, "test-user", "test-password")
        self.mail = MailAccount("test@example.com", "test-password")
        self.profile = generate_profile("tiktok")
        self.storage.reserve_proxy("one", [self.proxy])
        self.storage.save_fingerprint("one", {"config": {"seed": 123}})
        self.storage.reserve_profile("one", self.profile, self.mail)
        self.cookies = [{"name": "sessionid", "value": "test-session", "domain": ".tiktok.com", "path": "/",
                         "expires": 1900000000, "httpOnly": True, "secure": True, "sameSite": "Lax"}]

    def page(self, *, state=None):
        state = state if state is not None else {"cookies": self.cookies, "origins": []}
        return SimpleNamespace(url="https://www.tiktok.com/signup", context=SimpleNamespace(
            cookies=AsyncMock(return_value=self.cookies), storage_state=AsyncMock(return_value=state)),
            evaluate=AsyncMock(return_value={"origin": "https://www.tiktok.com", "values": {}}))

    async def test_large_browser_state_does_not_lose_cookies_or_expiry(self):
        state = {"cookies": self.cookies, "origins": [{"origin": "https://www.tiktok.com",
                 "localStorage": [{"name": "large", "value": "x" * (8 * 1024 * 1024)}]}]}
        await save_browser_session(self.page(state=state), self.storage, "one",
                                   registration_complete=True, platform_user_id="123")
        saved = self.storage.load_account("one")
        self.assertEqual(saved.cookies, self.cookies)
        self.assertEqual(saved.browser_state, {"cookies": self.cookies, "origins": []})
        self.assertFalse(saved.snapshot["complete"])
        self.assertEqual(saved.snapshot["reason"], "size_limit")
        self.assertEqual(saved.profile.registration_state, "complete")
        self.assertTrue(saved.profile.created_at)
        self.assertEqual(saved.fingerprint, {"config": {"seed": 123}})

    async def test_failed_full_export_still_saves_fresh_cookies(self):
        page = self.page()
        page.context.storage_state.side_effect = RuntimeError("Fixture IndexedDB error")
        await save_browser_session(page, self.storage, "one")
        saved = self.storage.load_account("one")
        self.assertEqual(saved.cookies, self.cookies)
        self.assertFalse(saved.snapshot["complete"])

    async def test_invalid_optional_state_is_recorded_as_incomplete(self):
        self.storage.save_account("one", self.cookies, self.proxy,
                                  browser_state={"cookies": self.cookies, "origins": [{"value": float("nan")}]})
        self.assertEqual(self.storage.load_account("one").cookies, self.cookies)
        self.assertEqual(self.storage.load_account("one").snapshot["reason"], "invalid_browser_state")

    async def test_identity_is_required_before_recording_creation(self):
        with self.assertRaisesRegex(ValueError, "user id"):
            self.storage.save_account("one", self.cookies, self.proxy, registration_complete=True)
        self.assertEqual(self.storage.load_account("one").profile.registration_state, "pending")

    async def test_wrong_session_plus_profile_link_is_rejected(self):
        page = self.page()
        page.evaluate.return_value = {"username": "another_user", "id": "456"}
        with self.assertRaisesRegex(RegistrationError, "different account"):
            await wait_registered(page, self.profile, timeout=0.05)

    async def test_user_cookie_must_match_authenticated_self_identity(self):
        profile = generate_profile("instagram")
        page = self.page()
        page.url = "https://www.instagram.com/"
        page.context.cookies.return_value = [dict(self.cookies[0], domain=".instagram.com"),
                                            {"name": "ds_user_id", "value": "456", "domain": ".instagram.com"}]
        page.evaluate.return_value = {"username": profile.username, "id": "123"}
        with self.assertRaisesRegex(RegistrationError, "user cookie"):
            await wait_registered(page, profile, timeout=0.05)

    async def test_valid_self_identity_returns_immutable_id(self):
        page = self.page()
        page.evaluate.return_value = {"username": self.profile.username, "id": "123"}
        self.assertEqual(await wait_registered(page, self.profile, timeout=0.05), "123")

    async def test_final_confirmation_checks_late_challenge(self):
        check = AsyncMock(side_effect=ManualCheckRequired("Fixture late challenge"))
        with self.assertRaises(ManualCheckRequired):
            await wait_registered(self.page(), self.profile, timeout=0.05, check=check)
        check.assert_awaited_once()

    async def test_cancellation_marks_unknown_and_keeps_session(self):
        ready = asyncio.Event()
        async def action(profile, mark_started):
            await mark_started()
            ready.set()
            await asyncio.Event().wait()
        context = SimpleNamespace(page=self.page(), request=SimpleNamespace(unique_key="one"))
        task = asyncio.create_task(register_account(context, self.storage, "tiktok", self.mail, action))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = self.storage.load_account("one")
        self.assertEqual(saved.profile.registration_state, "unknown")
        self.assertEqual(saved.cookies, self.cookies)

    async def test_cancellation_waits_for_pending_disk_claim(self):
        ready = Event()
        original = self.storage.set_registration_state
        def slow_claim(account_id, state):
            if state == "in_progress":
                ready.set()
                time.sleep(0.05)
            original(account_id, state)
        async def action(profile, mark_started):
            await mark_started()
            self.fail("Cancelled registration must not submit")
        context = SimpleNamespace(page=self.page(), request=SimpleNamespace(unique_key="one"))
        with patch.object(self.storage, "set_registration_state", side_effect=slow_claim):
            task = asyncio.create_task(register_account(context, self.storage, "tiktok", self.mail, action))
            await asyncio.to_thread(ready.wait, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.storage.load_account("one").profile.registration_state, "unknown")
        self.assertEqual(self.storage.load_account("one").cookies, self.cookies)

    async def test_cancelled_closed_browser_keeps_previous_checkpoint_and_cancellation(self):
        self.storage.save_account("one", self.cookies, self.proxy)
        ready = asyncio.Event()
        async def action(profile, mark_started):
            await mark_started()
            ready.set()
            await asyncio.Event().wait()
        page = self.page()
        page.context.cookies.side_effect = RuntimeError("Fixture browser closed")
        context = SimpleNamespace(page=page, request=SimpleNamespace(unique_key="one"))
        task = asyncio.create_task(register_account(context, self.storage, "tiktok", self.mail, action))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.storage.load_account("one").profile.registration_state, "unknown")
        self.assertEqual(self.storage.load_account("one").cookies, self.cookies)

    async def test_late_manual_check_keeps_manual_state_and_session(self):
        async def action(profile, mark_started):
            await mark_started()
            self.storage.set_registration_state("one", "needs_manual")
            raise ManualCheckRequired("Fixture challenge")
        context = SimpleNamespace(page=self.page(), request=SimpleNamespace(unique_key="one"))
        with self.assertRaises(ManualCheckRequired):
            await register_account(context, self.storage, "tiktok", self.mail, action)
        self.assertEqual(self.storage.load_account("one").profile.registration_state, "needs_manual")
        self.assertEqual(self.storage.load_account("one").cookies, self.cookies)

    async def test_username_replacement_keeps_other_fields_and_reservations(self):
        self.storage.set_registration_state("one", "in_progress")
        revised = self.storage.replace_username("one", "offered_username", expected=self.profile.username)
        self.assertEqual(revised, replace(self.profile, username="offered_username", registration_state="in_progress"))
        self.storage.set_registration_state("one", "unknown")
        with self.assertRaises(ValueError):
            self.storage.replace_username("one", "another", expected="offered_username")
        self.storage.reserve_proxy("two", [replace(self.proxy, port=8081)])
        with self.assertRaises(ValueError):
            self.storage.reserve_profile("two", self.profile, MailAccount("other@example.com", "test-password"))

    async def test_reset_requires_explicit_reconciliation_and_rejects_authenticated_session(self):
        self.storage.set_registration_state("one", "in_progress")
        self.storage.set_registration_state("one", "unknown")
        with self.assertRaises(ValueError):
            self.storage.reset_registration("one")
        self.storage.reset_registration("one", confirmed_not_created=True)
        self.assertEqual(self.storage.load_account("one").profile.registration_state, "pending")
        self.storage.set_registration_state("one", "in_progress")
        self.storage.set_registration_state("one", "unknown")
        self.storage.save_account("one", self.cookies, self.proxy)
        with self.assertRaises(ValueError):
            self.storage.reset_registration("one", confirmed_not_created=True)

    async def test_corrupt_account_does_not_block_healthy_updates_or_release_proxy(self):
        self.storage.reserve_proxy("two", [replace(self.proxy, port=8081)])
        self.storage._account_path("one").write_text("broken", encoding="utf-8")
        reopened = AccountStorage(self.directory.name)
        reopened.save_account("two", [], replace(self.proxy, port=8081))
        with self.assertRaises(ValueError):
            reopened.reserve_proxy("three", [self.proxy])
        self.assertEqual(reopened.reserve_proxy("three", [replace(self.proxy, port=8082)]).port, 8082)
        with self.assertRaises(ValueError):
            reopened.reserve_proxy("one", [replace(self.proxy, port=8083)])

    async def test_missing_index_migrates_existing_reservations(self):
        (Path(self.directory.name) / ".reservations").unlink()
        reopened = AccountStorage(self.directory.name)
        with self.assertRaises(ValueError):
            reopened.reserve_proxy("two", [self.proxy])
        self.assertEqual(reopened.reserve_proxy("two", [replace(self.proxy, port=8081)]).port, 8081)

    async def test_missing_snapshot_requires_restore_and_keeps_resources_reserved(self):
        path = self.storage._account_path("one")
        backup = path.read_bytes()
        path.unlink()
        reopened = AccountStorage(self.directory.name)
        with self.assertRaisesRegex(ValueError, "snapshot is missing"):
            reopened.reserve_proxy("one", [self.proxy])
        with self.assertRaisesRegex(ValueError, "snapshot is missing"):
            reopened.save_account("one", self.cookies, self.proxy)
        self.assertIsNone(reopened.load_account("one"))
        with self.assertRaises(ValueError):
            reopened.reserve_proxy("two", [self.proxy])
        reopened.reserve_proxy("two", [replace(self.proxy, port=8081)])
        path.write_bytes(backup)
        reopened.reserve_proxy("one", [self.proxy])
        saved = reopened.load_account("one")
        self.assertEqual(saved.profile, self.profile)
        self.assertEqual(saved.fingerprint, {"config": {"seed": 123}})

    async def test_failed_first_snapshot_write_keeps_reservation_and_blocks_recreation(self):
        original = self.storage._write_json
        def fail_snapshot(path, content):
            if path.suffix == ".json":
                raise OSError("Fixture disk failure")
            original(path, content)
        proxy = replace(self.proxy, port=8081)
        with patch.object(self.storage, "_write_json", side_effect=fail_snapshot):
            with self.assertRaises(OSError):
                self.storage.reserve_proxy("two", [proxy])
        reopened = AccountStorage(self.directory.name)
        with self.assertRaisesRegex(ValueError, "snapshot is missing"):
            reopened.reserve_proxy("two", [proxy])
        with self.assertRaises(ValueError):
            reopened.reserve_proxy("three", [proxy])
        self.assertIsNone(reopened.load_account("two"))

    async def test_cookie_updates_do_not_rewrite_unchanged_resource_index(self):
        with patch.object(self.storage, "_write_json", wraps=self.storage._write_json) as write:
            self.storage.save_account("one", self.cookies, self.proxy)
        self.assertEqual([call.args[0] for call in write.call_args_list], [self.storage._account_path("one")])
        self.storage.set_registration_state("one", "in_progress")
        self.storage.replace_username("one", "new_username", expected=self.profile.username)
        reopened = AccountStorage(self.directory.name)
        reopened.reserve_proxy("two", [replace(self.proxy, port=8081)])
        with self.assertRaises(ValueError):
            reopened.reserve_profile("two", replace(self.profile, username="new_username"),
                                     MailAccount("other@example.com", "test-password"))

    async def test_unindexed_corrupt_file_fails_closed(self):
        (Path(self.directory.name) / ".reservations").unlink()
        self.storage._account_path("one").write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            AccountStorage(self.directory.name).reserve_proxy("two", [replace(self.proxy, port=8081)])

    async def test_healthy_update_does_not_parse_every_account_snapshot(self):
        for number in range(2, 15):
            self.storage.reserve_proxy(str(number), [replace(self.proxy, port=8080 + number)])
        original = self.storage._load_path
        with patch.object(self.storage, "_load_path", wraps=original) as load:
            self.storage.save_account("one", self.cookies, self.proxy)
        self.assertEqual(load.call_count, 1)

    async def test_malformed_unrelated_letter_does_not_hide_valid_code(self):
        client = NotLettersClient("test-token")
        self.addAsyncCleanup(client.close)
        client._request_json = AsyncMock(return_value={"data": {"letters": [
            {"id": "bad", "sender": "other@example.com", "subject": "News", "letter": {"text": "News"}, "date": "bad-date"},
            {"id": "good", "sender": "login@tiktok.com", "subject": "Code", "letter": {"text": "Code: 123456"}, "date": time.time()}
        ]}})
        letters = await client.get_letters(self.mail)
        self.assertEqual([letter.id for letter in letters], ["good"])
        self.assertEqual(client.invalid_letter_count, 1)

    async def test_market_unknown_check_is_read_before_any_retry(self):
        self.storage.save_account("one", self.cookies, self.proxy, registration_complete=True, platform_user_id="123")
        market = LztMarketClient("test-token", timeout=0.1)
        self.addAsyncCleanup(market.close)
        calls = []
        async def request(method, path, payload=None):
            calls.append((method, path))
            if path == "/item/add":
                return {"item": {"item_id": 123, "item_state": "awaiting"}}
            if method == "POST":
                raise LztMarketError("Fixture timeout", result_unknown=True)
            return {"item": {"item_id": 123, "item_state": "active"}}
        market._request_json = request
        with self.assertRaises(LztMarketError):
            await market.publish_saved_account(self.storage, "one", title="Fixture", price=10)
        self.assertEqual(self.storage.load_account("one").marketplace["state"], "check_unknown")
        await market.publish_saved_account(self.storage, "one", title="Fixture", price=10)
        self.assertEqual(calls, [("POST", "/item/add"), ("POST", "/123/goods/check"), ("GET", "/123")])
        self.assertEqual(self.storage.load_account("one").marketplace["state"], "published")

    async def test_nonactive_market_result_does_not_prove_check_finished(self):
        self.storage.save_account("one", self.cookies, self.proxy, registration_complete=True, platform_user_id="123")
        settings = LztMarketClient._listing_payload("Fixture", 10, 20, "rub", "", "", 86400)
        self.storage.begin_sale("one", settings)
        self.storage.save_sale("one", 123, "check_unknown")
        market = LztMarketClient("test-token", timeout=0.1)
        self.addAsyncCleanup(market.close)
        market._request_json = AsyncMock(return_value={"item": {"item_id": 123, "item_state": "awaiting"}})
        with self.assertRaises(LztMarketError):
            await market.publish_saved_account(self.storage, "one", title="Fixture", price=10)
        market._request_json.assert_awaited_once_with("GET", "/123")

    async def test_parallel_market_checks_claim_only_once(self):
        self.storage.begin_sale("one", {"title": "Fixture"})
        self.storage.save_sale("one", 123, "awaiting")
        self.storage.begin_check("one")
        with self.assertRaises(RuntimeError):
            AccountStorage(self.directory.name).begin_check("one")

    async def test_stale_market_failure_cannot_downgrade_published_listing(self):
        self.storage.begin_sale("one", {"title": "Fixture"})
        self.storage.save_sale("one", 123, "published")
        with self.assertRaises(ValueError):
            self.storage.save_sale("one", 123, "awaiting")
        self.assertEqual(self.storage.load_account("one").marketplace["state"], "published")

    async def test_unknown_registration_can_be_confirmed_without_submitting_again(self):
        self.storage.set_registration_state("one", "in_progress")
        self.storage.set_registration_state("one", "unknown")
        page = self.page()
        page.evaluate.side_effect = [{"username": self.profile.username, "id": "123"},
                                    {"origin": "https://www.tiktok.com", "values": {}}]
        context = SimpleNamespace(page=page, request=SimpleNamespace(unique_key="one"))
        with patch("core.registration.check_manual", new=AsyncMock()):
            result = await confirm_saved_account(context, self.storage)
        self.assertEqual(result.registration_state, "complete")
        self.assertEqual(result.platform_user_id, "123")
        self.assertTrue(result.created_at)


if __name__ == "__main__":
    unittest.main()
