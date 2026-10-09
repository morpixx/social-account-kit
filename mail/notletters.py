import asyncio
import json
from math import isfinite

import httpx

from core.models import MailAccount
from core.models import MailLetter
from core.mail import parse_letter_date


class NotLettersClient:
    def __init__(
        self, token: str, *, timeout: float = 15,
        max_connections: int = 10, max_response_bytes: int = 10 * 1024 * 1024,
        proxy: str | None = None,
    ):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")

        for name, value in (("max_connections", max_connections), ("max_response_bytes", max_response_bytes)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

        self.token = token
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.base_url = "https://api.notletters.com"
        self.headers = {"Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json"}
        self.client = httpx.AsyncClient(
            base_url = self.base_url, headers = self.headers,
            timeout = timeout, trust_env = False, proxy = proxy,
            follow_redirects = False,
            limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )


    async def __aenter__(self):
        if self.client.is_closed:
            raise RuntimeError("NotLetters client is closed")
        return self


    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()


    async def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        async with asyncio.timeout(self.timeout):
            async with self.client.stream(method, path, json=payload) as response:
                response.raise_for_status()
                body = bytearray()

                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise ValueError("NotLetters response exceeds size limit")
                    body.extend(chunk)

        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Invalid NotLetters response")
        return data


    async def get_balance(self) -> float:
        data = await self._request_json("GET", "/v1/me")

        data = data["data"]
        balance = float(data["balance"])

        return balance


    async def get_letters(self, account: MailAccount, search: str = "") -> list[MailLetter]:
        payload = {"email": account.email,
                   "password": account.password}
        if search:
            payload["filters"] = {"search": search}

        data = await self._request_json("POST", "/v1/letters", payload)

        try:
            letters_data = data["data"]["letters"]
            if not isinstance(letters_data, list):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid NotLetters letters response") from None

        letters = []

        self.invalid_letter_count = 0
        for mail_data in letters_data:
            try:
                mail_id = mail_data["id"]
                if isinstance(mail_id, bool) or not isinstance(mail_id, (str, int)):
                    raise ValueError()
                mail_sender = mail_data["sender"]
                mail_subject = mail_data["subject"]
                mail_text = mail_data["letter"]["text"]
                if any(not isinstance(value, str) for value in (mail_sender, mail_subject, mail_text)):
                    raise ValueError()
                mail_date = parse_letter_date(mail_data["date"])
            except (KeyError, TypeError, ValueError, OverflowError):
                self.invalid_letter_count += 1
                continue

            letter = MailLetter(
            id = mail_id,
            sender = mail_sender,
            subject = mail_subject,
            text = mail_text,
            date = mail_date)

            letters.append(letter)
        if letters_data and not letters:
            raise ValueError("NotLetters returned only malformed letters")
        return letters


    async def buy_emails(self, count: int, type_email: int = 1) -> list[MailAccount]:
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("count must be an integer")

        if count < 1:
            raise ValueError("count must be at least 1")

        if not isinstance(type_email, int) or isinstance(type_email, bool):
            raise ValueError("type_email must be an integer")

        if type_email not in (0, 1, 3):
            raise ValueError("type_email must be one of: 0, 1, 3")

        payload = {"count": count,
                   "type_email": type_email}

        data = await self._request_json("POST", "/v1/buy-emails", payload)

        accounts = []

        for line in data["data"]:
            email, password = line.split(":", 1)
            accounts.append(MailAccount(email, password))

        return accounts


    async def change_password(self, account: MailAccount, new_password: str) -> str:
        payload = {"email": account.email,
                   "new_password": new_password,
                   "old_password": account.password}

        data = await self._request_json("POST", "/v1/change-password", payload)
        code = data["code"]

        if code != 200:
            raise ValueError("NotLetters failed to change password")

        data = data["data"]
        account.password = new_password

        return data

    async def close(self) -> None:
        await self.client.aclose()
