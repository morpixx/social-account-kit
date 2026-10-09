import time

from core.models import MailAccount
from core.registration import (check_manual, click, dismiss_cookies, fill, register_account,
                               set_birthday, submit_email_code, submit_username)
from core.session import save_browser_session
from core.storage import AccountStorage


SIGNUP_URL = "https://www.tiktok.com/signup/phone-or-email/email?lang=en"


class TikTokClient:
    def __init__(self, mail_client, storage: AccountStorage):
        self.mail_client = mail_client
        self.storage = storage

    async def register(self, context, mail_account: MailAccount, *, manual_handler=None):
        page = context.page
        account_id = context.request.unique_key

        async def check():
            await check_manual(page, self.storage, account_id, manual_handler, platform="tiktok")

        async def action(profile, mark_started):
            await dismiss_cookies(page)
            await check_manual(page, self.storage, account_id, manual_handler, platform="tiktok")
            await set_birthday(page, profile.birth_date, custom=True)
            await fill(page, ('input[name="email"]', 'input[placeholder="Email address"]'), mail_account.email)
            await fill(page, ('input[type="password"]',), profile.password)
            previous = await self.mail_client.get_letters(mail_account)
            requested_at = time.time() - 5
            await mark_started()
            await click(page, "^Send code$")
            await save_browser_session(page, self.storage, account_id)
            await check_manual(page, self.storage, account_id, manual_handler, platform="tiktok")
            await submit_email_code(page, profile, self.storage, account_id, self.mail_client, mail_account,
                                    domains=("tiktok.com",), after_date=requested_at,
                                    ignored_ids={letter.id for letter in previous},
                                    fields=('input[placeholder="Enter 6-digit code"]', 'input[name="code"]'),
                                    label="^Next$", stages=('input[name="username"], input[placeholder="Username"]',), check=check)
            await submit_username(page, profile, self.storage, account_id,
                                  ('input[name="username"]', 'input[placeholder="Username"]'),
                                  "^Sign up$", (), check)
            await check_manual(page, self.storage, account_id, manual_handler, platform="tiktok")

        return await register_account(context, self.storage, "tiktok", mail_account, action, check=check)
