from concurrent.futures import ThreadPoolExecutor
import os
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core.models import MailAccount, Proxy
from core.storage import AccountStorage, _protect_data


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = AccountStorage(self.directory.name)
        self.proxies = [Proxy("http", "localhost", 8000 + index, "test-user", "secret-proxy")
                        for index in range(8)]

    def test_json_roundtrip_with_all_browser_data(self):
        proxy = self.storage.reserve_proxy("account-one", self.proxies)
        fingerprint = {"config": {"navigator.userAgent": "test-browser"}, "os": "windows"}
        cookies = [{"name": "session", "value": "secret-session"}]
        browser_state = {"cookies": cookies, "origins": [{"origin": "https://example.com", "localStorage": []}]}
        sessions = [{"origin": "https://example.com", "values": {"token": "secret-token"}}]
        self.storage.save_fingerprint("account-one", fingerprint)
        self.storage.save_account("account-one", cookies, proxy,
                                  browser_state=browser_state, session_storage=sessions,
                                  mail_account=MailAccount("test@example.com", "test-email-password"))
        reopened = AccountStorage(self.directory.name).load_account("account-one")
        self.assertEqual(reopened.proxy, proxy)
        self.assertEqual(reopened.cookies, cookies)
        self.assertEqual(reopened.fingerprint, fingerprint)
        self.assertEqual(reopened.browser_state, browser_state)
        self.assertEqual(reopened.session_storage, sessions)
        self.assertEqual(reopened.mail_account.password, "test-email-password")
        files = list(Path(self.directory.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        content = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(content["cookies"], cookies)
        self.assertEqual(content["fingerprint"], fingerprint)
        self.assertEqual(content["proxy"]["password"], "secret-proxy")
        self.assertEqual(content["mail_account"]["password"], "test-email-password")
        self.assertEqual(list(Path(self.directory.name).glob("*.bin")), [])

    @unittest.skipUnless(os.name == "nt", "Legacy files use Windows DPAPI")
    def test_legacy_file_is_migrated_without_losing_data_or_proxy_reservation(self):
        proxy = self.storage.reserve_proxy("account-one", self.proxies)
        self.storage.save_fingerprint("account-one", {"config": {"seed": 1}})
        self.storage.save_account("account-one", [{"value": "test-session"}], proxy)
        account = self.storage.load_account("account-one")
        path = next(Path(self.directory.name).glob("*.json"))
        old_path = path.with_suffix(".bin")
        old_path.write_bytes(_protect_data(json.dumps({"version": 1, **asdict(account)}).encode()))
        path.unlink()
        second = self.storage.reserve_proxy("account-two", self.proxies)
        self.assertNotEqual(proxy, second)
        self.assertEqual(self.storage.reserve_proxy("account-one", []), proxy)
        self.assertEqual(self.storage.load_account("account-one"), account)
        self.assertTrue(path.exists())
        self.assertFalse(old_path.exists())

    def test_parallel_instances_cannot_assign_the_same_proxy(self):
        def reserve(index):
            return AccountStorage(self.directory.name).reserve_proxy(f"account-{index}", self.proxies)
        with ThreadPoolExecutor(max_workers=8) as executor:
            assigned = list(executor.map(reserve, range(8)))
        self.assertEqual(len({proxy.port for proxy in assigned}), 8)
        with self.assertRaises(ValueError):
            self.storage.reserve_proxy("account-extra", self.proxies)
        self.assertEqual(self.storage.reserve_proxy("account-0", []), assigned[0])

    def test_proxy_and_fingerprint_cannot_be_replaced(self):
        proxy = self.storage.reserve_proxy("account-one", self.proxies)
        self.storage.save_fingerprint("account-one", {"config": {"seed": 1}})
        with self.assertRaises(ValueError):
            self.storage.save_fingerprint("account-one", {"config": {"seed": 2}})
        with self.assertRaises(ValueError):
            self.storage.save_account("account-one", [], self.proxies[1])
        with self.assertRaises(ValueError):
            self.storage.save_account("account-two", [], proxy)

    def test_updating_cookies_keeps_browser_state_consistent(self):
        proxy = self.storage.reserve_proxy("account-one", self.proxies)
        self.storage.save_account("account-one", [{"value": "old"}], proxy,
                                  browser_state={"cookies": [{"value": "old"}], "origins": []})
        self.storage.save_account("account-one", [{"value": "new"}], proxy)
        saved = self.storage.load_account("account-one")
        self.assertEqual(saved.browser_state["cookies"], saved.cookies)
        self.assertEqual(saved.cookies, [{"value": "new"}])

    def test_failed_atomic_replace_preserves_previous_file(self):
        proxy = self.storage.reserve_proxy("account-one", self.proxies)
        self.storage.save_account("account-one", [{"value": "old"}], proxy)
        with patch("core.storage.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.storage.save_account("account-one", [{"value": "new"}], proxy)
        self.assertEqual(self.storage.load_account("account-one").cookies, [{"value": "old"}])
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_tampered_file_fails_closed(self):
        self.storage.reserve_proxy("account-one", self.proxies)
        path = next(Path(self.directory.name).glob("*.json"))
        path.write_bytes(b"broken-account-file")
        with self.assertRaisesRegex(ValueError, "Cannot read account file"):
            self.storage.load_account("account-one")
        second = self.storage.reserve_proxy("account-two", self.proxies)
        self.assertNotEqual(second, self.proxies[0])
        with self.assertRaises(ValueError):
            self.storage.reserve_proxy("account-one", self.proxies)

    def test_account_identifier_cannot_escape_storage_directory(self):
        self.storage.reserve_proxy("../../account", self.proxies)
        path = next(Path(self.directory.name).glob("*.json"))
        self.assertEqual(path.parent, Path(self.directory.name))
        self.assertEqual(len(path.stem), 64)

    def test_file_identity_and_duplicated_cookies_are_checked(self):
        proxy = self.storage.reserve_proxy("account-one", self.proxies)
        self.storage.save_account("account-one", [{"value": "test-session"}], proxy)
        path = next(Path(self.directory.name).glob("*.json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["account_id"] = "account-two"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.storage.load_account("account-one")
        data["account_id"] = "account-one"
        data["browser_state"]["cookies"] = []
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.storage.load_account("account-one")

    def test_parallel_sales_and_listing_identity_are_checked(self):
        self.storage.reserve_proxy("account-one", self.proxies)
        self.storage.reserve_proxy("account-two", self.proxies)
        self.storage.begin_sale("account-one", {"price": 10})
        with self.assertRaises(RuntimeError):
            self.storage.begin_sale("account-one", {"price": 10})
        self.storage.save_sale("account-one", 123, "awaiting")
        self.assertEqual(self.storage.begin_sale("account-one", {"price": 10})["item_id"], 123)
        self.storage.begin_sale("account-two", {"price": 10})
        with self.assertRaises(ValueError):
            self.storage.save_sale("account-two", 123, "awaiting")
        with self.assertRaises(ValueError):
            self.storage.save_sale("account-one", 124, "published")

    def test_json_nonfinite_numbers_and_boolean_listing_id_are_rejected(self):
        self.storage.reserve_proxy("account-one", self.proxies)
        path = next(Path(self.directory.name).glob("*.json"))
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        data["fingerprint"] = {"value": float("nan")}
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.storage.load_account("account-one")
        data = json.loads(original)
        data["marketplace"] = {"state": "published", "item_id": True, "settings": {"price": 10}}
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.storage.load_account("account-one")


if __name__ == "__main__":
    unittest.main()
