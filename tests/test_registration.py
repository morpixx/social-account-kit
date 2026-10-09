import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.models import MailAccount, MailLetter, Proxy
from core.registration import (RegistrationError, check_origin, generate_profile,
                               prepare_registration, register_account, wait_email_code, wait_registered)
from core.storage import AccountStorage


class ProfileTests(unittest.TestCase):
    def test_age_boundaries_include_18_and_75_on_leap_days(self):
        for today in (date(2024, 2, 29), date(2025, 2, 28), date(2026, 10, 8)):
            for boundary in (0, -1):
                with patch("core.registration.secrets.randbelow", side_effect=lambda count: 0 if boundary == 0 else count - 1):
                    born = date.fromisoformat(generate_profile("tiktok", today=today).birth_date)
                age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
                self.assertEqual(age, 75 if boundary == 0 else 18)

    def test_generated_profiles_have_valid_distinct_usernames_and_hidden_passwords(self):
        for platform in ("tiktok", "instagram"):
            profiles = [generate_profile(platform) for _ in range(100)]
            self.assertEqual(len({profile.username for profile in profiles}), 100)
            for profile in profiles:
                self.assertRegex(profile.username, r"^[a-z0-9]{1,24}$")
                self.assertNotIn(profile.password, repr(profile))
                self.assertEqual(profile.created_at, "")
                self.assertEqual(profile.registration_state, "pending")

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            generate_profile("unknown")

    def test_unexpected_websites_are_rejected(self):
        for url in ("http://www.tiktok.com", "https://tiktok.com.evil.test", "https://www.instagram.com/",
                    "https://user@www.tiktok.com", "https://www.tiktok.com:8443"):
            with self.assertRaises(RegistrationError):
                check_origin(SimpleNamespace(url=url), "tiktok")
        check_origin(SimpleNamespace(url="https://www.tiktok.com/signup"), "tiktok")


class RegistrationStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = AccountStorage(self.directory.name)
        self.proxy = Proxy("http", "proxy.test", 8080, "test-user", "test-password")
        self.mail = MailAccount("test@example.com", "test-mail-password")
        self.storage.reserve_proxy("one", [self.proxy])
        self.storage.save_fingerprint("one", {"config": {"seed": 1}})
        self.profile = generate_profile("tiktok")
        self.storage.reserve_profile("one", self.profile, self.mail)

    async def test_profile_is_reused_without_regeneration(self):
        with patch("core.registration.generate_profile", side_effect=AssertionError("must reuse")):
            restored = await prepare_registration(self.storage, "one", "tiktok", self.mail)
        self.assertEqual(restored, self.profile)

    async def test_created_date_and_profile_are_saved_with_cookies_and_kept_on_updates(self):
        self.storage.set_registration_state("one", "in_progress")
        self.storage.save_account("one", [{"name": "sessionid", "value": "test-session"}], self.proxy,
                                  registration_complete=True, platform_user_id="123")
        account = self.storage.load_account("one")
        stamp = account.profile.created_at
        self.assertEqual(datetime.fromisoformat(stamp).utcoffset().total_seconds(), 0)
        self.storage.save_account("one", [{"name": "sessionid", "value": "updated"}], self.proxy)
        self.storage.set_registration_state("one", "complete")
        content = json.loads(next(Path(self.directory.name).glob("*.json")).read_text())
        self.assertEqual(content["profile"]["created_at"], stamp)
        self.assertEqual(content["profile"]["username"], self.profile.username)
        self.assertEqual(content["profile"]["birth_date"], self.profile.birth_date)
        self.assertEqual(content["profile"]["name"], self.profile.name)
        self.assertEqual(content["profile"]["password"], self.profile.password)
        self.assertEqual(content["mail_account"]["password"], self.mail.password)
        self.assertEqual(content["fingerprint"], {"config": {"seed": 1}})

    async def test_profile_and_email_cannot_be_replaced(self):
        with self.assertRaises(ValueError):
            self.storage.reserve_profile("one", replace(self.profile, username="another"), self.mail)
        with self.assertRaises(ValueError):
            self.storage.reserve_profile("one", self.profile, MailAccount("other@example.com", "test-password"))
        with self.assertRaises(ValueError):
            self.storage.save_account("one", [], self.proxy, mail_account=MailAccount("other@example.com", "test-password"))

    async def test_duplicate_username_or_email_is_not_assigned_on_the_same_platform(self):
        self.storage.reserve_proxy("two", [replace(self.proxy, port=8081)])
        with self.assertRaises(ValueError):
            self.storage.reserve_profile("two", self.profile, MailAccount("other@example.com", "test-password"))
        with self.assertRaises(ValueError):
            self.storage.reserve_profile("two", replace(self.profile, username="another"), self.mail)

    async def test_exactly_one_parallel_registration_can_claim_the_account(self):
        def claim(_):
            try:
                AccountStorage(self.directory.name).set_registration_state("one", "in_progress")
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sum(workers.map(claim, range(2))), 1)

    async def test_unknown_registration_cannot_automatically_submit_again(self):
        self.storage.set_registration_state("one", "in_progress")
        self.storage.set_registration_state("one", "unknown")
        with self.assertRaises(RegistrationError):
            await prepare_registration(self.storage, "one", "tiktok", self.mail)
        self.assertEqual(self.storage.load_account("one").profile.created_at, "")

    async def test_failed_attempt_keeps_session_but_does_not_record_creation(self):
        page = SimpleNamespace(url="https://www.tiktok.com/signup", context=SimpleNamespace(cookies=AsyncMock(return_value=[{"value": "partial"}]), storage_state=AsyncMock(return_value={"cookies": [{"value": "partial"}], "origins": []})),
                               evaluate=AsyncMock(return_value={"origin": "https://www.tiktok.com", "values": {}}))
        context = SimpleNamespace(page=page, request=SimpleNamespace(unique_key="one"))
        async def action(profile, mark_started):
            await mark_started()
            raise RegistrationError("Unconfirmed registration")
        with self.assertRaisesRegex(RegistrationError, "Unconfirmed"):
            await register_account(context, self.storage, "tiktok", self.mail, action)
        account = self.storage.load_account("one")
        self.assertEqual(account.profile.registration_state, "unknown")
        self.assertEqual(account.profile.created_at, "")
        self.assertEqual(account.cookies, [{"value": "partial"}])

    async def test_completed_registration_cannot_be_reset_or_recorded_without_cookies(self):
        with self.assertRaises(ValueError):
            self.storage.set_registration_state("one", "complete")
        with self.assertRaises(ValueError):
            self.storage.save_account("one", [], self.proxy, registration_complete=True)
        self.assertEqual(self.storage.load_account("one").profile.created_at, "")


class VerificationMailTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_link_without_authentication_cannot_confirm_registration(self):
        link = SimpleNamespace(count=AsyncMock(return_value=1), first=SimpleNamespace(is_visible=AsyncMock(return_value=True)))
        page = SimpleNamespace(url="https://www.tiktok.com", locator=lambda selector: link,
                               context=SimpleNamespace(cookies=AsyncMock(return_value=[])))
        with self.assertRaises(RegistrationError):
            await wait_registered(page, generate_profile("tiktok"), timeout=0.01)

    async def test_old_letters_and_lookalike_sender_domains_are_rejected(self):
        letters = [MailLetter("old", "login@tiktok.com", "Code", "Code: 111111", 101),
                   MailLetter("fake", "login@tiktok.com.evil.test", "Code", "Code: 222222", 105),
                   MailLetter("dated", "login@tiktok.com", "Code", "Code: 333333", 99),
                   MailLetter("new", "TikTok <login@account.tiktok.com>", "Code", "Code: 654321", 102)]
        client = SimpleNamespace(get_letters=AsyncMock(return_value=letters))
        check = AsyncMock()
        result = await wait_email_code(client, MailAccount("test@example.com", "test-password"),
                                       domains=("tiktok.com",), after_date=100, ignored_ids={"old"}, check=check)
        self.assertEqual(result, "654321")
        check.assert_awaited_once()

    async def test_missing_verification_email_has_a_total_timeout(self):
        client = SimpleNamespace(get_letters=AsyncMock(return_value=[]))
        with self.assertRaisesRegex(RegistrationError, "did not arrive"):
            await wait_email_code(client, MailAccount("test@example.com", "test-password"),
                                  domains=("instagram.com",), after_date=0, ignored_ids=set(), timeout=0.02, interval=0.01)

    async def test_slow_mail_request_is_cancelled_by_the_total_timeout(self):
        async def slow(account):
            await asyncio.sleep(1)
        client = SimpleNamespace(get_letters=slow)
        with self.assertRaises(RegistrationError):
            await wait_email_code(client, MailAccount("test@example.com", "test-password"),
                                  domains=("tiktok.com",), after_date=0, ignored_ids=set(), timeout=0.02)
