import time

from core.models import MailAccount
from core.registration import (check_manual, click, dismiss_cookies, fill, register_account,
                               set_birthday, submit_email_code, submit_username, wait_stage)
from core.storage import AccountStorage


SIGNUP_URL = "https://www.instagram.com/accounts/emailsignup/"
BIRTHDAY = 'select[title^="Month"], select[aria-label^="Month"], select[name="month"]'
CODE = 'input[name="email_confirmation_code"], input[name="confirmationCode"], input[aria-label="Confirmation Code"]'


class InstagramClient:
    def __init__(self, mail_client, storage: AccountStorage):
        self.mail_client = mail_client
        self.storage = storage

    async def register(self, context, mail_account: MailAccount, *, manual_handler=None):
        page = context.page
        account_id = context.request.unique_key

        async def check():
            await check_manual(page, self.storage, account_id, manual_handler, platform="instagram")

        async def action(profile, mark_started):
            await dismiss_cookies(page)
            await check_manual(page, self.storage, account_id, manual_handler, platform="instagram")
            await fill(page, ('input[name="emailOrPhone"]', 'input[aria-label="Mobile Number or Email"]',
                             'input[aria-label="Email"]'), mail_account.email)
            await fill(page, ('input[name="fullName"]', 'input[aria-label="Full Name"]'), profile.name)
            await fill(page, ('input[name="username"]', 'input[aria-label="Username"]'), profile.username)
            await fill(page, ('input[name="password"]', 'input[type="password"]'), profile.password)
            previous = await self.mail_client.get_letters(mail_account)
            requested_at = time.time() - 5
            await mark_started()
            stage = await submit_username(page, profile, self.storage, account_id,
                                          ('input[name="username"]', 'input[aria-label="Username"]'),
                                          "^Sign up$", (BIRTHDAY, CODE), check)
            if stage == 0:
                await set_birthday(page, profile.birth_date)
                await click(page, "^Next$")
                await wait_stage(page, (CODE,), self.storage, account_id, manual_handler)
            await submit_email_code(page, profile, self.storage, account_id, self.mail_client, mail_account,
                                    domains=("instagram.com", "mail.instagram.com"), after_date=requested_at,
                                    ignored_ids={letter.id for letter in previous},
                                    fields=('input[name="email_confirmation_code"]', 'input[name="confirmationCode"]',
                                            'input[aria-label="Confirmation Code"]'),
                                    label="^(Next|Confirm)$", stages=(), check=check)
            await check_manual(page, self.storage, account_id, manual_handler, platform="instagram")

        return await register_account(context, self.storage, "instagram", mail_account, action, check=check)
