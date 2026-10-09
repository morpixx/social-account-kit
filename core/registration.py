import asyncio
from calendar import month_name
from datetime import date, timedelta
from email.utils import parseaddr
import re
import secrets
import string
import time
from urllib.parse import urlsplit

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from core.mail import extract_verification_code
from core.models import AccountProfile, MailAccount
from core.storage import AccountStorage
from core.session import save_browser_session


FIRST_NAMES = ("Alex", "Daniel", "Oliver", "James", "Noah", "Henry", "Leo", "David",
               "Emma", "Olivia", "Sophia", "Mia", "Amelia", "Emily", "Chloe", "Grace")
LAST_NAMES = ("Miller", "Wilson", "Taylor", "Brown", "Clark", "Hall", "Young", "Green",
              "Walker", "Baker", "Hill", "Scott", "Adams", "Carter", "Turner", "Parker")
CHALLENGE_SELECTOR = 'iframe[src*="captcha"], iframe[src*="recaptcha"], [id*="captcha"], [class*="captcha-verify"]'


class RegistrationError(RuntimeError):
    pass


class ManualCheckRequired(RegistrationError):
    pass


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def generate_profile(platform: str, *, today: date | None = None) -> AccountProfile:
    if platform not in ("tiktok", "instagram"):
        raise ValueError("Supported platforms: tiktok, instagram")
    today = today if today is not None else date.today()
    oldest = _years_ago(today, 76) + timedelta(days=1)
    youngest = _years_ago(today, 18)
    birthday = oldest + timedelta(days=secrets.randbelow((youngest - oldest).days + 1))
    first = secrets.choice(FIRST_NAMES)
    last = secrets.choice(LAST_NAMES)
    username = (first + last).lower()[:14] + secrets.token_hex(5)
    alphabet = string.ascii_letters + string.digits + "!@#_"
    password = "Aa7!" + "".join(secrets.choice(alphabet) for _ in range(12))
    return AccountProfile(platform, f"{first} {last}", username, birthday.isoformat(), password)


async def wait_email_code(mail_client, mail_account: MailAccount, *, domains: tuple[str, ...],
                          after_date: float, ignored_ids: set, timeout: float = 120,
                          interval: float = 3, check=None, consume: bool = False) -> str:
    if timeout <= 0 or interval <= 0:
        raise ValueError("Mail polling intervals must be positive")
    try:
        async with asyncio.timeout(timeout):
            while True:
                if check is not None:
                    await check()
                letters = await mail_client.get_letters(mail_account)
                matching = []
                for letter in letters:
                    sender = parseaddr(letter.sender)[1].rpartition("@")[2].casefold()
                    if any(sender == domain or sender.endswith("." + domain) for domain in domains):
                        matching.append(letter)
                for letter in sorted(matching, key=lambda item: item.date, reverse=True):
                    if letter.date <= after_date or letter.id in ignored_ids:
                        continue
                    code = extract_verification_code(letter.text)
                    if code:
                        if consume:
                            ignored_ids.add(letter.id)
                        return code
                await asyncio.sleep(interval)
    except TimeoutError:
        raise RegistrationError("Verification email did not arrive in time") from None


async def visible(page: Page, selector: str) -> bool:
    for item in await page.locator(selector).all():
        if await item.is_visible():
            return True
    return False


def check_origin(page: Page, platform: str | None = None) -> None:
    url = urlsplit(page.url)
    domains = (platform + ".com",) if platform is not None else ("tiktok.com", "instagram.com")
    if url.scheme != "https" or url.hostname not in {host for domain in domains for host in (domain, "www." + domain)}:
        raise RegistrationError("Registration left the expected website")
    if url.username is not None or url.password is not None or url.port not in (None, 443):
        raise RegistrationError("Invalid registration website")


async def fill(page: Page, selectors: tuple[str, ...], value: str, *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_origin(page)
        for selector in selectors:
            for field in await page.locator(selector).all():
                if await field.is_visible() and await field.is_editable():
                    await field.fill(value)
                    return
        await asyncio.sleep(0.2)
    raise RegistrationError("Registration field is missing or unavailable")


async def click(page: Page, label: str, *, timeout: float = 20) -> None:
    check_origin(page)
    button = page.get_by_role("button", name=re.compile(label, re.IGNORECASE))
    try:
        await button.first.click(timeout=timeout * 1000)
    except PlaywrightTimeoutError:
        raise RegistrationError("Registration button is missing or unavailable") from None


async def dismiss_cookies(page: Page) -> None:
    for label in ("Decline optional cookies", "Only allow essential cookies", "Reject optional cookies"):
        button = page.get_by_role("button", name=label, exact=True)
        if await button.count() and await button.first.is_visible():
            await button.first.click()
            return


async def set_birthday(page: Page, birth_date: str, *, custom: bool = False) -> None:
    check_origin(page)
    born = date.fromisoformat(birth_date)
    for label, value in (("Year", born.year), ("Month", born.month), ("Day", born.day)):
        if not custom:
            field = page.locator(f'select[title^="{label}"], select[aria-label^="{label}"], select[name="{label.lower()}"]').first
            try:
                await field.select_option(str(value), timeout=20000)
            except PlaywrightTimeoutError:
                raise RegistrationError("Birthday selector is missing or unavailable") from None
        else:
            field = page.get_by_role("combobox", name=re.compile("^" + label))
            await field.click(timeout=20000)
            text = month_name[value] if label == "Month" else str(value)
            option = page.get_by_role("option", name=text, exact=True)
            try:
                await option.scroll_into_view_if_needed(timeout=20000)
                await option.click()
            except PlaywrightTimeoutError:
                raise RegistrationError("Requested birth date is unavailable in the form") from None


async def check_manual(page: Page, storage: AccountStorage, account_id: str, manual_handler,
                       *, platform: str | None = None) -> None:
    check_origin(page, platform)
    if not await visible(page, CHALLENGE_SELECTOR) and "/challenge/" not in page.url and "/checkpoint/" not in page.url:
        return
    account = await asyncio.to_thread(storage.load_account, account_id)
    check_origin(page, account.profile.platform)
    started = account.profile.registration_state in ("in_progress", "needs_manual")
    if started:
        await asyncio.to_thread(storage.set_registration_state, account_id, "needs_manual")
    if manual_handler is None:
        raise ManualCheckRequired("Website requires a manual security check")
    await manual_handler(page)
    check_origin(page, account.profile.platform)
    if await visible(page, CHALLENGE_SELECTOR) or "/challenge/" in page.url or "/checkpoint/" in page.url:
        raise ManualCheckRequired("Manual security check has not completed")
    if started:
        await asyncio.to_thread(storage.set_registration_state, account_id, "in_progress")


async def read_identity(page: Page, platform: str) -> dict | None:
    check_origin(page, platform)
    # These are website adapters, not public OAuth APIs. Missing or changed data fails closed.
    if platform == "tiktok":
        identity = await page.evaluate("""() => {
            const node = document.querySelector('script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
            if (!node || node.textContent.length > 1048576) return null;
            try {
                const app = JSON.parse(node.textContent).__DEFAULT_SCOPE__?.['webapp.app-context'];
                if (app?.isLogin !== true || !app.user) return null;
                return {username: app.user.uniqueId, id: app.user.id};
            } catch { return null; }
        }""")
    else:
        identity = await page.evaluate("""async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 3000);
            try {
                const response = await fetch('/api/v1/accounts/current_user/?edit=true', {
                    credentials: 'same-origin', redirect: 'error', signal: controller.signal,
                    headers: {'X-IG-App-ID': '936619743392459'}
                });
                if (!response.ok) return null;
                const reader = response.body.getReader();
                const chunks = []; let size = 0;
                while (true) {
                    const chunk = await reader.read(); if (chunk.done) break;
                    size += chunk.value.length;
                    if (size > 1048576) { await reader.cancel(); return null; }
                    chunks.push(chunk.value);
                }
                const bytes = new Uint8Array(size); let offset = 0;
                for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
                const data = JSON.parse(new TextDecoder().decode(bytes));
                if (!data.user) return null;
                return {username: data.user.username, id: String(data.user.pk ?? '')};
            } catch { return null; } finally { clearTimeout(timer); }
        }""")
    check_origin(page, platform)
    if not isinstance(identity, dict) or not isinstance(identity.get("username"), str):
        return None
    user_id = identity.get("id")
    if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit() or len(user_id) > 32 or int(user_id) < 1:
        return None
    return identity


async def wait_registered(page: Page, profile: AccountProfile, *, timeout: float = 45, check=None) -> str:
    domain = "tiktok.com" if profile.platform == "tiktok" else "instagram.com"
    cookie_name = "sessionid"
    deadline = time.monotonic() + timeout
    refreshed = False
    while time.monotonic() < deadline:
        check_origin(page, profile.platform)
        if check is not None:
            await check()
        cookies = await page.context.cookies()
        authenticated = any(cookie.get("name") == cookie_name and cookie.get("value")
                            and cookie.get("domain", "").lstrip(".") in (domain, "www." + domain) for cookie in cookies)
        if authenticated:
            identity = await read_identity(page, profile.platform)
            if identity is not None:
                if identity["username"].casefold() != profile.username.casefold() or (profile.platform_user_id and identity["id"] != profile.platform_user_id):
                    raise RegistrationError("Authenticated session belongs to a different account")
                user_cookie = next((cookie["value"] for cookie in cookies if cookie.get("name") == "ds_user_id"
                                    and cookie.get("domain", "").lstrip(".") in (domain, "www." + domain)), None)
                if user_cookie is not None and user_cookie != identity["id"]:
                    raise RegistrationError("Session identity does not match the user cookie")
                return identity["id"]
            if profile.platform == "tiktok" and not refreshed:
                # The signup page can still contain guest bootstrap data after login.
                refreshed = True
                await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded",
                                timeout=max(1, deadline - time.monotonic()) * 1000)
                continue
        await asyncio.sleep(0.3)
    raise RegistrationError("Website did not confirm the requested account identity")


async def prepare_registration(storage: AccountStorage, account_id: str, platform: str,
                               mail_account: MailAccount) -> AccountProfile:
    account = await asyncio.to_thread(storage.load_account, account_id)
    if account is None:
        raise ValueError("Account proxy must be reserved first")
    profile = account.profile
    if profile is not None:
        if profile.platform != platform or account.mail_account != mail_account:
            raise ValueError("Saved account belongs to another registration profile")
        if profile.registration_state not in ("pending", "complete"):
            raise RegistrationError("Previous registration must be reconciled before another attempt")
        if profile.registration_state == "complete" and not profile.platform_user_id:
            raise RegistrationError("Saved registration has no verified user id; confirm the restored session first")
        if profile.registration_state == "pending":
            today = date.today()
            born = date.fromisoformat(profile.birth_date)
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            if not 18 <= age <= 75:
                raise RegistrationError("Saved birth date is outside the supported age range")
        return profile
    profile = generate_profile(platform)
    return await asyncio.to_thread(storage.reserve_profile, account_id, profile, mail_account)


async def wait_stage(page: Page, selectors: tuple[str, ...], storage: AccountStorage,
                     account_id: str, manual_handler, *, timeout: float = 45) -> int:
    account = await asyncio.to_thread(storage.load_account, account_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await check_manual(page, storage, account_id, manual_handler, platform=account.profile.platform)
        for index, selector in enumerate(selectors):
            if await visible(page, selector):
                return index
        await asyncio.sleep(0.3)
    raise RegistrationError("Website did not show the next registration step")


async def form_error(page: Page, fields: tuple[str, ...], pattern: str) -> bool:
    selectors = ['[role="alert"]', '#signup_error_message', '[data-e2e*="error"]']
    for field in fields:
        for item in await page.locator(field).all():
            described = (await item.get_attribute("aria-describedby") or "").split()
            for name in described:
                if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    selectors.append("#" + name)
    for selector in selectors:
        for item in await page.locator(selector).all():
            if await item.is_visible() and re.search(pattern, await item.inner_text(), re.IGNORECASE):
                return True
    return False


async def replace_rejected_username(page: Page, profile: AccountProfile, storage: AccountStorage,
                                    account_id: str) -> None:
    check_origin(page, profile.platform)
    candidates = []
    selector = '[data-e2e*="suggest"], [data-testid*="suggest"], [role="option"]'
    for item in await page.locator(selector).all():
        if await item.is_visible():
            candidates.append((await item.inner_text()).strip().removeprefix("@"))
    prefix = re.sub(r"[^a-z0-9]", "", profile.name.lower())[:14] or "user"
    candidates.extend(prefix + secrets.token_hex(5) for _ in range(5))
    limit = 24 if profile.platform == "tiktok" else 30
    for username in candidates:
        if username.casefold() == profile.username.casefold() or not re.fullmatch(r"[a-zA-Z0-9_.]{1," + str(limit) + "}", username):
            continue
        if username.endswith(".") or ".." in username:
            continue
        try:
            updated = await asyncio.to_thread(storage.replace_username, account_id, username, expected=profile.username)
        except ValueError:
            continue
        profile.username = updated.username
        return
    raise RegistrationError("No valid unused username could be reserved")


async def submit_username(page: Page, profile: AccountProfile, storage: AccountStorage,
                          account_id: str, fields: tuple[str, ...], label: str,
                          stages: tuple[str, ...], check, *, timeout: float = 45) -> int:
    taken = r"(?:username|user name).{0,60}(?:taken|already|unavailable|not available|isn't available)|(?:taken|unavailable).{0,40}(?:username|user name)"
    for attempt in range(4):
        await check()
        await fill(page, fields, profile.username)
        seen_clear = not await form_error(page, fields, taken)
        await click(page, label)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await check()
            for index, selector in enumerate(stages):
                if await visible(page, selector):
                    await save_browser_session(page, storage, account_id)
                    return index
            if not stages and not any([await visible(page, field) for field in fields]):
                await save_browser_session(page, storage, account_id)
                return -1
            error = await form_error(page, fields, taken)
            if not error:
                seen_clear = True
            if error and seen_clear:
                if attempt == 3:
                    raise RegistrationError("Website rejected all username attempts")
                # A logged-in session may mean creation already happened. Never submit it again blindly.
                cookies = await page.context.cookies()
                if any(cookie.get("name") == "sessionid" and cookie.get("value")
                       and cookie.get("domain", "").lstrip(".") in (profile.platform + ".com", "www." + profile.platform + ".com")
                       for cookie in cookies):
                    raise RegistrationError("Username rejection occurred after login; reconcile the account")
                await replace_rejected_username(page, profile, storage, account_id)
                break
            await asyncio.sleep(0.3)
        else:
            raise RegistrationError("Website did not confirm or reject the username submission")
    raise RegistrationError("Username attempts exhausted")


async def submit_email_code(page: Page, profile: AccountProfile, storage: AccountStorage,
                            account_id: str, mail_client, mail_account: MailAccount, *,
                            domains: tuple[str, ...], after_date: float, ignored_ids: set,
                            fields: tuple[str, ...], label: str, stages: tuple[str, ...], check,
                            timeout: float = 120) -> int:
    deadline = time.monotonic() + timeout
    rejected = r"(?:code).{0,50}(?:incorrect|invalid|expired|wrong)|(?:incorrect|invalid|expired|wrong).{0,50}code"
    for _ in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        code = await wait_email_code(mail_client, mail_account, domains=domains, after_date=after_date,
                                     ignored_ids=ignored_ids, check=check, consume=True, timeout=remaining)
        await fill(page, fields, code)
        seen_clear = not await form_error(page, fields, rejected)
        await click(page, label)
        while time.monotonic() < deadline:
            await check()
            for index, selector in enumerate(stages):
                if await visible(page, selector):
                    await save_browser_session(page, storage, account_id)
                    return index
            if not stages and not any([await visible(page, field) for field in fields]):
                await save_browser_session(page, storage, account_id)
                return -1
            error = await form_error(page, fields, rejected)
            if not error:
                seen_clear = True
            if error and seen_clear:
                break
            await asyncio.sleep(0.3)
        else:
            raise RegistrationError("Website did not confirm or reject the verification code")
    raise RegistrationError("Verification codes were rejected or expired")


async def confirm_saved_account(context, storage: AccountStorage, *, manual_handler=None) -> AccountProfile:
    account_id = context.request.unique_key
    account = await asyncio.to_thread(storage.load_account, account_id)
    if account is None or account.profile is None:
        raise RegistrationError("Saved registration profile is missing")
    async def check():
        await check_manual(context.page, storage, account_id, manual_handler, platform=account.profile.platform)
    user_id = await wait_registered(context.page, account.profile, check=check)
    await save_browser_session(context.page, storage, account_id, registration_complete=True, platform_user_id=user_id)
    return (await asyncio.to_thread(storage.load_account, account_id)).profile


async def register_account(context, storage: AccountStorage, platform: str,
                           mail_account: MailAccount, action, *, check=None) -> AccountProfile:
    account_id = context.request.unique_key
    profile = await prepare_registration(storage, account_id, platform, mail_account)
    if profile.registration_state == "complete":
        return profile
    started = False
    completed = False
    claim = None
    try:
        async with asyncio.timeout(300):
            check_origin(context.page, platform)
            async def mark_started():
                nonlocal started, claim
                claim = asyncio.create_task(asyncio.to_thread(storage.set_registration_state, account_id, "in_progress"))
                try:
                    await asyncio.shield(claim)
                finally:
                    if claim.done() and not claim.cancelled() and claim.exception() is None:
                        started = True
            await action(profile, mark_started)
            profile = (await asyncio.to_thread(storage.load_account, account_id)).profile
            user_id = await wait_registered(context.page, profile, check=check)
            await save_browser_session(context.page, storage, account_id, registration_complete=True, platform_user_id=user_id)
            completed = True
            return (await asyncio.to_thread(storage.load_account, account_id)).profile
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if isinstance(error, RegistrationError):
            raise
        raise RegistrationError("Registration did not complete; inspect the saved account state") from None
    finally:
        async def preserve():
            nonlocal started
            if claim is not None:
                try:
                    await claim
                    started = True
                except Exception:
                    return
            if started and not completed:
                account = await asyncio.to_thread(storage.load_account, account_id)
                if account.profile.registration_state not in ("complete", "needs_manual", "unknown"):
                    await asyncio.to_thread(storage.set_registration_state, account_id, "unknown")
                await save_browser_session(context.page, storage, account_id)
        # A cancellation cannot skip the completed disk claim or partial-session checkpoint.
        cleanup = asyncio.create_task(preserve())
        cancelled = asyncio.current_task().cancelling() > 0
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            cleanup.result()
        except Exception:
            if not cancelled:
                raise RegistrationError("Partial session could not be saved; keep the last checkpoint for reconciliation") from None
            if getattr(context, "log", None) is not None:
                context.log.error("Partial session could not be saved during cancellation")
        if cancelled:
            raise asyncio.CancelledError()
