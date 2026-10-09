from contextlib import contextmanager
from copy import deepcopy
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import RLock
import time

from core.models import AccountProfile, MailAccount, Proxy, SavedAccount
from core.proxy import validate_proxy


def _protect_data(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Account storage requires Windows DPAPI")

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(wintypes.BYTE))]

    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)))
    result = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def _proxy_key(proxy: Proxy) -> tuple:
    validate_proxy(proxy)
    return (proxy.scheme, proxy.host.lower(), proxy.port, proxy.username, proxy.password)


def _invalid_json_number(value: str):
    raise ValueError("Account JSON contains a nonfinite number")


def validate_profile(profile: AccountProfile) -> None:
    if not isinstance(profile, AccountProfile) or profile.platform not in ("tiktok", "instagram"):
        raise ValueError("Invalid account profile")
    if any(not isinstance(value, str) or not value for value in
           (profile.name, profile.username, profile.birth_date, profile.password)):
        raise ValueError("Invalid account profile fields")
    limit = 24 if profile.platform == "tiktok" else 30
    if not re.fullmatch(r"[A-Za-z0-9_.]{1," + str(limit) + "}", profile.username):
        raise ValueError("Invalid account username")
    if profile.username.endswith(".") or ".." in profile.username:
        raise ValueError("Invalid account username")
    if not isinstance(profile.platform_user_id, str):
        raise ValueError("Invalid platform user id")
    if profile.platform_user_id and (not profile.platform_user_id.isascii() or not profile.platform_user_id.isdigit()
                                    or len(profile.platform_user_id) > 32 or int(profile.platform_user_id) < 1):
        raise ValueError("Invalid platform user id")
    born = date.fromisoformat(profile.birth_date)
    if born.isoformat() != profile.birth_date or born >= date.today():
        raise ValueError("Invalid account birth date")
    if profile.registration_state not in ("pending", "in_progress", "needs_manual", "unknown", "complete"):
        raise ValueError("Invalid registration state")
    if not isinstance(profile.created_at, str):
        raise ValueError("Invalid account creation date")
    if profile.created_at:
        if datetime.fromisoformat(profile.created_at).utcoffset() is None:
            raise ValueError("Account creation date must include a timezone")
    if bool(profile.created_at) != (profile.registration_state == "complete"):
        raise ValueError("Account creation date requires completed registration")


class AccountStorage:
    def __init__(self, directory: Path | str = "data/accounts"):
        self.directory = Path(directory)
        self._lock = RLock()
        self._index_cache = None
        self._index_stamp = None

    def _account_path(self, account_id: str) -> Path:
        if not isinstance(account_id, str) or not account_id or len(account_id) > 256:
            raise ValueError("account_id must be a nonempty string of at most 256 characters")
        filename = sha256(account_id.encode("utf-8")).hexdigest() + ".json"
        return self.directory / filename

    @contextmanager
    def _write_lock(self):
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl

        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / ".lock").open("a+b") as file:
                file.seek(0, os.SEEK_END)
                if file.tell() == 0:
                    file.write(b"0")
                    file.flush()

                deadline = time.monotonic() + 15
                while True:
                    file.seek(0)
                    try:
                        if os.name == "nt":
                            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Account storage is busy") from None
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    file.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def _account_files(self):
        return [*self.directory.glob("*.json"), *self.directory.glob("*.bin")]

    def _write_json(self, path: Path, content: bytes) -> None:
        with NamedTemporaryFile(dir=self.directory, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                file.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _claim(index: dict, kind: str, value, owner: str) -> None:
        key = sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()
        existing = index[kind].get(key)
        if existing is not None and existing != owner:
            raise ValueError(f"{kind} is already assigned to another account")
        index[kind][key] = owner

    def _index_account(self, index: dict, account: SavedAccount) -> None:
        owner = self._account_path(account.account_id).stem
        proxy = _proxy_key(account.proxy)
        proxy_hash = sha256(json.dumps(proxy).encode("utf-8")).hexdigest()
        if owner in index["accounts"] and index["accounts"][owner] != proxy_hash:
            raise ValueError("Account proxy is already fixed")
        self._claim(index, "proxy", proxy, owner)
        if account.profile is not None:
            self._claim(index, "username", [account.profile.platform, account.profile.username.casefold()], owner)
            if account.mail_account is not None:
                self._claim(index, "email", [account.profile.platform, account.mail_account.email.casefold()], owner)
        if account.marketplace.get("item_id") is not None:
            self._claim(index, "listing", account.marketplace["item_id"], owner)
        index["accounts"][owner] = proxy_hash

    def _reservation_index(self) -> dict:
        path = self.directory / ".reservations"
        stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        if stamp is not None and stamp == self._index_stamp:
            index = deepcopy(self._index_cache)
        elif stamp is not None:
            try:
                if path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError()
                index = json.loads(path.read_bytes(), parse_constant=_invalid_json_number)
                if type(index.get("version")) is not int or index["version"] != 1 or any(not isinstance(index.get(key), dict)
                        for key in ("accounts", "proxy", "username", "email", "listing")):
                    raise ValueError()
                if any(not isinstance(key, str) or not isinstance(value, str)
                       for kind in ("accounts", "proxy", "username", "email", "listing")
                       for key, value in index[kind].items()):
                    raise ValueError()
            except (OSError, ValueError, TypeError, AttributeError):
                raise ValueError("Reservation index is damaged; restore it before assigning resources") from None
        else:
            index = {"version": 1, "accounts": {}, "proxy": {}, "username": {}, "email": {}, "listing": {}}
        # Known reservations survive damaged account snapshots. Unknown files must be reconciled.
        for file in self._account_files():
            if file.stem not in index["accounts"]:
                self._index_account(index, self._load_path(file))
        return index

    def _save_index(self, index: dict) -> None:
        path = self.directory / ".reservations"
        if index == self._index_cache and path.exists():
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            if stamp == self._index_stamp:
                return
        self._write_json(path, json.dumps(index, allow_nan=False).encode("utf-8"))
        self._index_stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        self._index_cache = deepcopy(index)

    def _load_path(self, path: Path) -> SavedAccount:
        with path.open("rb") as file:
            content = file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Account file exceeds size limit")
        try:
            if path.suffix == ".bin":
                content = _protect_data(content, decrypt=True)
            data = json.loads(content, parse_constant=_invalid_json_number)
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data["cookies"], list):
                raise ValueError("Invalid account file")
            if any(not isinstance(cookie, dict) for cookie in data["cookies"]):
                raise ValueError("Invalid account cookies")
            proxy = Proxy(**data["proxy"])
            _proxy_key(proxy)
            if not isinstance(proxy.username, str) or not isinstance(proxy.password, str):
                raise ValueError("Invalid account proxy")
            account = SavedAccount(data["account_id"], proxy, data["cookies"],
                                   data.get("fingerprint", {}), data.get("browser_state", {}),
                                   data.get("session_storage", []))
            if data.get("mail_account") is not None:
                account.mail_account = MailAccount(**data["mail_account"])
                if not isinstance(account.mail_account.email, str) or not isinstance(account.mail_account.password, str):
                    raise ValueError("Invalid account email")
            account.marketplace = data.get("marketplace", {})
            if data.get("profile") is not None:
                account.profile = AccountProfile(**data["profile"])
                validate_profile(account.profile)
            account.snapshot = data.get("snapshot", {})
            if not isinstance(account.snapshot, dict):
                raise ValueError("Invalid snapshot information")
            if not isinstance(account.marketplace, dict):
                raise ValueError("Invalid marketplace data")
            if account.marketplace:
                sale = account.marketplace
                item_id = sale.get("item_id")
                if sale.get("state") not in ("creating", "unknown", "failed", "awaiting", "checking", "check_unknown", "published") or not isinstance(sale.get("settings"), dict):
                    raise ValueError("Invalid listing state")
                if item_id is not None and (type(item_id) is not int or item_id < 1):
                    raise ValueError("Invalid listing id")
                if sale["state"] in ("awaiting", "checking", "check_unknown", "published") and item_id is None:
                    raise ValueError("Listing id is required")
            if not isinstance(account.fingerprint, dict) or not isinstance(account.browser_state, dict) or not isinstance(account.session_storage, list):
                raise ValueError("Invalid account browser data")
            if self._account_path(account.account_id).stem != path.stem:
                raise ValueError("Account file identity mismatch")
            if account.browser_state and account.browser_state.get("cookies") != account.cookies:
                raise ValueError("Account cookies disagree with browser state")
            return account
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError("Cannot read account file") from None

    def load_account(self, account_id: str) -> SavedAccount | None:
        path = self._account_path(account_id)
        if not path.exists():
            path = path.with_suffix(".bin")
            if not path.exists():
                return None
        return self._load_path(path)

    def _save_account(self, account: SavedAccount) -> None:
        path = self._account_path(account.account_id)
        data = {"version": 1, **asdict(account)}
        reason = ""
        try:
            content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(content) > 8 * 1024 * 1024:
                reason = "size_limit"
        except (TypeError, ValueError):
            reason = "invalid_browser_state"
        if reason:
            data["browser_state"] = {"cookies": data["cookies"], "origins": []}
            data["session_storage"] = []
            data["snapshot"] = {"complete": False, "reason": reason, "captured_at": datetime.now(timezone.utc).isoformat()}
            content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("Essential account data exceeds size limit")
        index = self._reservation_index()
        if path.stem in index["accounts"] and not path.exists() and not path.with_suffix(".bin").exists():
            raise ValueError("Account snapshot is missing; restore it before continuing")
        self._index_account(index, account)
        # Reserve before writing the snapshot; failed writes never release a proxy or nickname.
        self._save_index(index)
        self._write_json(path, content)
        path.with_suffix(".bin").unlink(missing_ok=True)

    def reserve_proxy(self, account_id: str, proxies: list[Proxy]) -> Proxy:
        self._account_path(account_id)
        with self._write_lock():
            saved = self.load_account(account_id)
            if saved is not None:
                index = self._reservation_index()
                self._index_account(index, saved)
                self._save_index(index)
                if not self._account_path(account_id).exists():
                    self._save_account(saved)
                return saved.proxy

            index = self._reservation_index()
            owner = self._account_path(account_id).stem
            for proxy in proxies:
                key = sha256(json.dumps(_proxy_key(proxy)).encode("utf-8")).hexdigest()
                if index["proxy"].get(key) in (None, owner) and (owner not in index["accounts"] or index["accounts"][owner] == key):
                    self._save_account(SavedAccount(account_id, proxy, []))
                    return proxy
            raise ValueError("No unused proxies remain for a new account")

    def save_fingerprint(self, account_id: str, fingerprint: dict) -> None:
        if not isinstance(fingerprint, dict) or not fingerprint:
            raise ValueError("Fingerprint must be a nonempty object")
        with self._write_lock():
            saved = self.load_account(account_id)
            if saved is None:
                raise ValueError("Account proxy must be reserved first")
            if saved.fingerprint and saved.fingerprint != fingerprint:
                raise ValueError("Account fingerprint is already fixed")
            saved.fingerprint = fingerprint
            self._save_account(saved)

    def save_account(
        self, account_id: str, cookies: list[dict], proxy: Proxy, *,
        browser_state: dict | None = None, session_storage: list[dict] | None = None,
        mail_account: MailAccount | None = None,
        registration_complete: bool = False,
        snapshot: dict | None = None,
        platform_user_id: str | None = None,
    ) -> None:
        self._account_path(account_id)
        proxy_key = _proxy_key(proxy)
        if not isinstance(cookies, list) or any(not isinstance(cookie, dict) for cookie in cookies):
            raise ValueError("cookies must be a list of cookie objects")
        if type(registration_complete) is not bool:
            raise ValueError("registration_complete must be boolean")

        with self._write_lock():
            account = self.load_account(account_id) or SavedAccount(account_id, proxy, [])
            if _proxy_key(account.proxy) != proxy_key:
                raise ValueError("Account proxy is already fixed")
            account.cookies = cookies
            if browser_state is not None:
                if not isinstance(browser_state, dict):
                    raise ValueError("browser_state must be an object")
                account.browser_state = browser_state
            account.browser_state = {**account.browser_state, "cookies": cookies}
            account.browser_state.setdefault("origins", [])
            if snapshot is not None:
                if not isinstance(snapshot, dict):
                    raise ValueError("Invalid snapshot information")
                account.snapshot = snapshot
            if session_storage is not None:
                if not isinstance(session_storage, list):
                    raise ValueError("session_storage must be a list")
                if any(not isinstance(item, dict) or not isinstance(item.get("origin"), str)
                       or not isinstance(item.get("values"), dict)
                       or any(not isinstance(key, str) or not isinstance(value, str)
                              for key, value in item["values"].items()) for item in session_storage):
                    raise ValueError("Invalid session_storage entries")
                account.session_storage = session_storage
            if mail_account is not None:
                if not isinstance(mail_account, MailAccount) or not isinstance(mail_account.email, str) or not isinstance(mail_account.password, str):
                    raise ValueError("mail_account must contain email and password")
                if account.profile is not None and account.mail_account is not None and account.mail_account.email.casefold() != mail_account.email.casefold():
                    raise ValueError("Account registration email is already fixed")
                account.mail_account = mail_account
            if registration_complete:
                if account.profile is None or not cookies:
                    raise ValueError("Registration requires a profile and an authenticated browser session")
                if platform_user_id is not None:
                    if not isinstance(platform_user_id, str) or not platform_user_id:
                        raise ValueError("Verified platform user id is required")
                    if account.profile.platform_user_id and account.profile.platform_user_id != platform_user_id:
                        raise ValueError("Authenticated account identity changed")
                    account.profile.platform_user_id = platform_user_id
                if not account.profile.platform_user_id:
                    raise ValueError("Verified platform user id is required")
                account.profile.registration_state = "complete"
                if not account.profile.created_at:
                    account.profile.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                validate_profile(account.profile)
            self._save_account(account)

    def reserve_profile(self, account_id: str, profile: AccountProfile, mail_account: MailAccount) -> AccountProfile:
        validate_profile(profile)
        if profile.registration_state != "pending" or profile.created_at:
            raise ValueError("New account profile must be pending")
        if not isinstance(mail_account, MailAccount) or not isinstance(mail_account.email, str) or not mail_account.email:
            raise ValueError("Account email is required")
        if not isinstance(mail_account.password, str) or not mail_account.password:
            raise ValueError("Account email password is required")
        born = date.fromisoformat(profile.birth_date)
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        if not 18 <= age <= 75:
            raise ValueError("Registration age must be between 18 and 75")
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None:
                raise ValueError("Account proxy must be reserved first")
            if account.profile is not None:
                if account.profile != profile or account.mail_account != mail_account:
                    raise ValueError("Account registration profile is already fixed")
                return account.profile
            if account.cookies:
                raise ValueError("Existing browser account cannot be registered again")
            if account.mail_account is not None and account.mail_account != mail_account:
                raise ValueError("Account email is already fixed")
            account.profile = profile
            account.mail_account = mail_account
            self._save_account(account)
            return profile

    def replace_username(self, account_id: str, username: str, *, expected: str) -> AccountProfile:
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None or account.profile is None:
                raise ValueError("Account profile is missing")
            if account.profile.registration_state not in ("pending", "in_progress") or account.profile.username != expected:
                raise ValueError("Username cannot be changed in this registration state")
            account.profile.username = username
            validate_profile(account.profile)
            self._save_account(account)
            return account.profile

    def set_registration_state(self, account_id: str, state: str) -> None:
        if state not in ("in_progress", "needs_manual", "unknown", "complete"):
            raise ValueError("Invalid registration state")
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None or account.profile is None:
                raise ValueError("Account registration profile is not saved")
            if account.profile.registration_state == "complete":
                if state != "complete":
                    raise ValueError("Completed registration cannot be reset")
                return
            if account.profile.registration_state == "unknown" and state != "complete":
                raise ValueError("Unknown registration must be reconciled before a new attempt")
            if state == "in_progress" and account.profile.registration_state not in ("pending", "needs_manual"):
                raise ValueError("Registration is already in progress")
            if state == "complete" and (not account.cookies or not account.profile.platform_user_id):
                raise ValueError("Completed registration requires a browser session")
            account.profile.registration_state = state
            if state == "complete":
                account.profile.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save_account(account)

    def reset_registration(self, account_id: str, *, confirmed_not_created: bool = False) -> None:
        if confirmed_not_created is not True:
            raise ValueError("Confirm the account does not exist on the platform before resetting")
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None or account.profile is None or account.profile.registration_state not in ("unknown", "in_progress", "needs_manual"):
                raise ValueError("Registration cannot be reset")
            if account.profile.platform_user_id or any(cookie.get("name") == "sessionid" and cookie.get("value")
                                                     for cookie in account.cookies):
                raise ValueError("Authenticated session must be reconciled, not registered again")
            account.profile.registration_state = "pending"
            account.cookies = []
            account.browser_state = {"cookies": [], "origins": []}
            account.session_storage = []
            account.snapshot = {"complete": False, "reason": "operator_confirmed_not_created",
                                "captured_at": datetime.now(timezone.utc).isoformat()}
            self._save_account(account)

    def begin_sale(self, account_id: str, settings: dict) -> dict:
        if not isinstance(settings, dict) or not settings:
            raise ValueError("Listing settings must be a nonempty object")
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None:
                raise ValueError("Account is not saved")
            sale = account.marketplace
            if sale:
                if sale.get("settings") != settings:
                    raise ValueError("Account already has a listing with different settings")
                if sale.get("item_id"):
                    return sale
                if sale.get("state") != "failed":
                    raise RuntimeError("Previous listing creation must be reconciled with the Market")
            account.marketplace = {"state": "creating", "item_id": None, "settings": settings}
            self._save_account(account)
            return account.marketplace

    def save_sale(self, account_id: str, item_id: int | None, state: str) -> None:
        if state not in ("unknown", "failed", "awaiting", "checking", "check_unknown", "published"):
            raise ValueError("Invalid listing state")
        if item_id is not None and (type(item_id) is not int or item_id < 1):
            raise ValueError("Invalid listing id")
        if state in ("awaiting", "checking", "check_unknown", "published") and item_id is None:
            raise ValueError("Listing id is required")
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None or not account.marketplace:
                raise ValueError("Account listing is not reserved")
            old_id = account.marketplace.get("item_id")
            if old_id is not None and old_id != item_id:
                raise ValueError("Account listing id is already fixed")
            if account.marketplace.get("state") == "published" and state != "published":
                raise ValueError("Published listing cannot be downgraded by a stale response")
            account.marketplace.update(item_id=item_id, state=state)
            self._save_account(account)

    def begin_check(self, account_id: str) -> None:
        with self._write_lock():
            account = self.load_account(account_id)
            if account is None or account.marketplace.get("state") != "awaiting":
                raise RuntimeError("Market check must be reconciled before another attempt")
            account.marketplace["state"] = "checking"
            self._save_account(account)
