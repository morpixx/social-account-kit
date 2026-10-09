import argparse
import asyncio
from getpass import getpass
from pathlib import Path
import re

from crawlee import Request

from core.config import load_config
from core.crawler import create_crawler
from core.models import MailAccount
from core.proxy import load_proxies
from core.registration import CHALLENGE_SELECTOR, RegistrationError, visible
from core.storage import AccountStorage
from mail.notletters import NotLettersClient
from platforms.instagram import InstagramClient, SIGNUP_URL as INSTAGRAM_SIGNUP
from platforms.tiktok import TikTokClient, SIGNUP_URL as TIKTOK_SIGNUP


async def main():
    parser = argparse.ArgumentParser(description="Register one account and save its browser session")
    parser.add_argument("platform", choices=("tiktok", "instagram"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--email")
    parser.add_argument("--proxies", type=Path, default=Path("data/proxies.txt"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--manual-checks", action="store_true")
    args = parser.parse_args()
    if args.headless and args.manual_checks:
        parser.error("Manual checks require a visible browser")
    config = load_config()
    if not config.notletters_token or re.search(r"[^\x21-\x7e]", config.notletters_token):
        parser.error("Set a valid NOTLETTERS_TOKEN in .env")
    storage = AccountStorage(Path(config.data_dir) / "accounts")
    saved = storage.load_account(args.account_id)
    if saved is not None and saved.profile is not None and saved.profile.platform != args.platform:
        parser.error("Account belongs to another platform; use a different account-id")
    if saved is not None and saved.profile is not None and saved.profile.registration_state == "complete":
        if not saved.profile.platform_user_id:
            parser.error("Saved registration has no verified user id; confirm the restored session before selling")
        print(f"Account already registered: {saved.profile.username} ({saved.profile.created_at})")
        return
    if saved is not None and saved.mail_account is not None:
        if args.email is not None and args.email.casefold() != saved.mail_account.email.casefold():
            parser.error("Account email is already fixed")
        account = saved.mail_account
    else:
        email = args.email or input("Email: ").strip()
        account = MailAccount(email, getpass("Email password: "))
    proxies = [] if saved is not None else load_proxies(args.proxies)
    result = None

    async def manual_handler(page):
        print("Complete the website security check in the opened browser (up to 120 seconds).")
        async with asyncio.timeout(120):
            while await visible(page, CHALLENGE_SELECTOR) or "/challenge/" in page.url or "/checkpoint/" in page.url:
                await asyncio.sleep(0.5)

    async with NotLettersClient(config.notletters_token) as mail_client:
        client = TikTokClient(mail_client, storage) if args.platform == "tiktok" else InstagramClient(mail_client, storage)

        async def handler(context):
            nonlocal result
            result = await client.register(context, account, manual_handler=manual_handler if args.manual_checks else None)

        crawler = create_crawler(handler=handler, proxies=proxies, account_storage=storage,
                                 headless=args.headless, locale="en-US", geoip=True,
                                 max_concurrency=1, max_request_retries=0, request_handler_timeout=400)
        url = TIKTOK_SIGNUP if args.platform == "tiktok" else INSTAGRAM_SIGNUP
        await crawler.run([Request.from_url(url, unique_key=args.account_id)])
    if result is None:
        raise RegistrationError("Registration failed; inspect the account state before another attempt")
    print(f"Account registered: {result.username} ({result.created_at})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RegistrationError as error:
        print(str(error))
        raise SystemExit(1) from None
