import asyncio
from decimal import Decimal, InvalidOperation
import json
from math import isfinite
import time

import httpx

from core.models import MailAccount, Proxy
from core.proxy import validate_proxy
from core.storage import AccountStorage


class LztMarketError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 retry_after: float | None = None, data: dict | None = None,
                 result_unknown: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.data = data or {}
        self.result_unknown = result_unknown


class LztMarketClient:
    def __init__(self, token: str, *, timeout: float = 300,
                 max_response_bytes: int = 10 * 1024 * 1024, proxy: str | None = None):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("LZT token is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")

        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.client = httpx.AsyncClient(
            base_url = "https://api.lzt.market",
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout = timeout, trust_env = False, follow_redirects = False, proxy = proxy,
            limits = httpx.Limits(max_connections=3, max_keepalive_connections=3),
        )

    async def __aenter__(self):
        if self.client.is_closed:
            raise RuntimeError("LZT client is closed")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(method, path, json=payload) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise LztMarketError("LZT response exceeds size limit", result_unknown=method != "GET")
                        body.extend(chunk)
                    try:
                        data = json.loads(body, parse_float=Decimal)
                    except (ValueError, UnicodeError):
                        raise LztMarketError("Invalid LZT response", status_code=response.status_code,
                                             result_unknown=method != "GET") from None
                    if not isinstance(data, dict):
                        raise LztMarketError("Invalid LZT response", result_unknown=method != "GET")
                    if not response.is_success or data.get("errors"):
                        retry_after = None
                        try:
                            if response.headers.get("Retry-After"):
                                retry_after = float(response.headers["Retry-After"])
                            elif response.headers.get("X-RateLimit-Reset"):
                                retry_after = float(response.headers["X-RateLimit-Reset"]) - time.time()
                            if retry_after is not None:
                                retry_after = max(0, retry_after) if isfinite(retry_after) else None
                        except ValueError:
                            retry_after = None
                        raise LztMarketError(f"LZT request failed (HTTP {response.status_code})",
                                             status_code=response.status_code, retry_after=retry_after, data=data,
                                             result_unknown=method != "GET" and (response.status_code >= 500 or response.status_code in (200, 408)))
                    return data
        except (httpx.RequestError, TimeoutError):
            raise LztMarketError("LZT request did not return a result", result_unknown=method != "GET") from None

    @staticmethod
    def _item(data: dict) -> dict:
        item = data.get("item")
        if not isinstance(item, dict) or type(item.get("item_id")) is not int or item["item_id"] < 1:
            raise LztMarketError("LZT response has no valid item", result_unknown=True)
        return item

    @staticmethod
    def _item_id(item_id: int) -> None:
        if type(item_id) is not int or item_id < 1:
            raise ValueError("item_id must be a positive integer")

    async def get_profile(self) -> dict:
        data = await self._request_json("GET", "/me")
        profile = data.get("user")
        if not isinstance(profile, dict):
            raise LztMarketError("LZT response has no user")
        return profile

    async def get_balance(self) -> Decimal:
        profile = await self.get_profile()
        try:
            balance = Decimal(str(profile["balance"]))
            if not balance.is_finite():
                raise ValueError("Invalid balance")
            return balance
        except (KeyError, InvalidOperation, ValueError):
            raise LztMarketError("LZT response has no valid balance") from None

    async def get_account(self, item_id: int) -> dict:
        self._item_id(item_id)
        item = self._item(await self._request_json("GET", f"/{item_id}"))
        if item["item_id"] != item_id:
            raise LztMarketError("LZT returned a different account")
        return item

    @staticmethod
    def _listing_payload(title: str, price: int | float, category_id: int, currency: str,
                         description: str, information: str, guarantee_duration: int) -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Listing title is required")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not isfinite(price) or price < 1:
            raise ValueError("price must be a finite number of at least 1")
        if type(category_id) is not int or category_id not in (10, 20):
            raise ValueError("Supported categories: Instagram=10, TikTok=20")
        if currency not in ("rub", "uah", "kzt", "byn", "usd", "eur", "gbp", "cny", "try", "jpy", "brl"):
            raise ValueError("Invalid listing currency")
        if type(guarantee_duration) is not int or guarantee_duration not in (0, 43200, 86400, 259200):
            raise ValueError("Invalid guarantee duration")
        if not isinstance(description, str) or not isinstance(information, str):
            raise ValueError("Listing descriptions must be strings")
        return {"title": title, "price": price, "category_id": category_id, "currency": currency,
                "item_origin": "autoreg", "guarantee_duration": guarantee_duration,
                "description": description, "information": information}

    @staticmethod
    def _email_payload(mail_account: MailAccount | None, email_type: str) -> dict:
        if email_type not in ("native", "autoreg"):
            raise ValueError("email_type must be native or autoreg")
        if mail_account is None:
            return {}
        if not isinstance(mail_account, MailAccount) or not isinstance(mail_account.email, str) or not isinstance(mail_account.password, str):
            raise ValueError("mail_account must contain email and password")
        if not mail_account.email or ":" in mail_account.email or not mail_account.password:
            raise ValueError("Email login and password are required")
        return {"has_email_login_data": True, "email_login_data": f"{mail_account.email}:{mail_account.password}",
                "email_type": email_type}

    async def add_account(self, *, title: str, price: int | float, category_id: int = 20,
                          currency: str = "rub", description: str = "", information: str = "",
                          guarantee_duration: int = 86400, mail_account: MailAccount | None = None,
                          email_type: str = "autoreg") -> dict:
        payload = self._listing_payload(title, price, category_id, currency, description, information, guarantee_duration)
        payload.update(self._email_payload(mail_account, email_type))
        return self._item(await self._request_json("POST", "/item/add", payload))

    @staticmethod
    def _check_payload(cookies: list[dict], proxy: Proxy, category_id: int,
                       login: str, password: str, mail_account: MailAccount | None, email_type: str) -> dict:
        if not isinstance(cookies, list) or not cookies or any(not isinstance(cookie, dict)
                or not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str)
                or not isinstance(cookie.get("domain"), str) or not isinstance(cookie.get("path"), str) for cookie in cookies):
            raise ValueError("Nonempty browser cookies are required")
        validate_proxy(proxy)
        if proxy.scheme != "http":
            raise ValueError("Market checker requires an HTTP proxy in host:port:user:password format")
        if not isinstance(proxy.username, str) or not isinstance(proxy.password, str) or ":" in proxy.username:
            raise ValueError("Invalid Market proxy credentials")
        if category_id not in (10, 20) or type(category_id) is not int:
            raise ValueError("Supported categories: Instagram=10, TikTok=20")
        if not isinstance(login, str) or not isinstance(password, str) or bool(login) != bool(password):
            raise ValueError("Pass both account login and password, or neither")
        extra = {"cookies": json.dumps(cookies, ensure_ascii=False, allow_nan=False),
                 "proxy": f"{proxy.host}:{proxy.port}:{proxy.username}:{proxy.password}"}
        if category_id == 20:
            extra["cookie_login"] = True
        payload = {"extra": extra}
        if login:
            payload.update(login=login, password=password)
        payload.update(LztMarketClient._email_payload(mail_account, email_type))
        return payload

    async def check_account(self, item_id: int, *, cookies: list[dict], proxy: Proxy,
                            category_id: int = 20, login: str = "", password: str = "",
                            mail_account: MailAccount | None = None, email_type: str = "autoreg") -> dict:
        self._item_id(item_id)
        payload = self._check_payload(cookies, proxy, category_id, login, password, mail_account, email_type)
        item = self._item(await self._request_json("POST", f"/{item_id}/goods/check", payload))
        if item["item_id"] != item_id:
            raise LztMarketError("LZT returned a different account", result_unknown=True)
        return item

    async def publish_saved_account(self, storage: AccountStorage, account_id: str, *,
                                    title: str, price: int | float, category_id: int = 20,
                                    currency: str = "rub", description: str = "", information: str = "",
                                    guarantee_duration: int = 86400, login: str = "", password: str = "",
                                    mail_account: MailAccount | None = None, email_type: str = "autoreg") -> dict:
        account = await asyncio.to_thread(storage.load_account, account_id)
        if account is None or not account.fingerprint:
            raise ValueError("A saved account with its registration fingerprint is required")
        if account.profile is not None:
            if account.profile.registration_state != "complete" or not account.profile.platform_user_id:
                raise ValueError("Account registration is not confirmed")
            expected_category = 20 if account.profile.platform == "tiktok" else 10
            if category_id != expected_category:
                raise ValueError("Market category does not match the saved account platform")
            if not login and not password:
                login, password = account.profile.username, account.profile.password
        mail_account = mail_account if mail_account is not None else account.mail_account
        check_payload = self._check_payload(account.cookies, account.proxy, category_id, login, password, mail_account, email_type)
        settings = self._listing_payload(title, price, category_id, currency, description, information, guarantee_duration)
        # Keep existing listing settings so older sales can be reconciled without creating a duplicate.
        if account.marketplace.get("settings") != settings:
            proxy_info = json.dumps({"scheme": account.proxy.scheme, "host": account.proxy.host,
                                     "port": account.proxy.port, "username": account.proxy.username,
                                     "password": account.proxy.password}, ensure_ascii=False)
            settings["information"] += ("\n\n" if information else "") + "Account proxy (JSON):\n" + proxy_info
        sale = await asyncio.to_thread(storage.begin_sale, account_id, settings)
        item_id = sale.get("item_id")
        if item_id is None:
            try:
                payload = {**settings, **self._email_payload(mail_account, email_type)}
                item = self._item(await self._request_json("POST", "/item/add", payload))
                item_id = item["item_id"]
            except LztMarketError as error:
                state = "unknown" if error.result_unknown else "failed"
                await asyncio.to_thread(storage.save_sale, account_id, None, state)
                raise
            await asyncio.to_thread(storage.save_sale, account_id, item_id, "awaiting")
        if sale.get("state") == "published":
            return await self.get_account(item_id)
        if sale.get("state") in ("checking", "check_unknown"):
            item = await self.get_account(item_id)
            if item.get("item_state") == "active":
                await asyncio.to_thread(storage.save_sale, account_id, item_id, "published")
                return item
            # A non-active listing does not prove that an asynchronous check has stopped.
            raise LztMarketError("Previous Market check is unresolved; no duplicate check was sent", result_unknown=True)
        await asyncio.to_thread(storage.begin_check, account_id)
        try:
            item = self._item(await self._request_json("POST", f"/{item_id}/goods/check", check_payload))
            if item["item_id"] != item_id or item.get("item_state") != "active":
                raise LztMarketError("Market did not confirm an active listing", result_unknown=True)
        except LztMarketError as error:
            await asyncio.to_thread(storage.save_sale, account_id, item_id, "check_unknown" if error.result_unknown else "awaiting")
            raise
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(storage.save_sale, account_id, item_id, "check_unknown"))
            raise
        await asyncio.to_thread(storage.save_sale, account_id, item_id, "published")
        return item
