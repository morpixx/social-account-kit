import asyncio
from dataclasses import replace
from datetime import date
import json
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest

from playwright.async_api import async_playwright

from core.browser import prepare_browser_executable
from core.models import MailAccount, MailLetter, Proxy
from core.registration import ManualCheckRequired, generate_profile
from core.storage import AccountStorage
from platforms.instagram import InstagramClient, SIGNUP_URL as INSTAGRAM_SIGNUP
from platforms.tiktok import TikTokClient, SIGNUP_URL as TIKTOK_SIGNUP


TIKTOK_HTML = '''<!doctype html><html lang="en"><body>
<div role="combobox" aria-label="Year. Double-tap for more options" onclick="options('Year')">Year</div>
<div role="combobox" aria-label="Month. Double-tap for more options" onclick="options('Month')">Month</div>
<div role="combobox" aria-label="Day. Double-tap for more options" onclick="options('Day')">Day</div>
<div id="options" role="listbox" style="height:160px;overflow:auto"></div>
<input name="email" placeholder="Email address"><input type="password" placeholder="Password">
<input placeholder="Enter 6-digit code"><button onclick="window.sent=true;document.body.dataset.sent='true'">Send code</button>
<button onclick="next()">Next</button>
<script>
window.birthday={}; window.sent=false;document.body.dataset.sent='false';
const months=['January','February','March','April','May','June','July','August','September','October','November','December'];
function options(kind) {
 const values=kind==='Year'?Array.from({length:127},(_,i)=>2026-i):kind==='Month'?months:Array.from({length:31},(_,i)=>String(i+1));
 const box=document.getElementById('options'); box.innerHTML='';
 for(const value of values){const row=document.createElement('div'); row.role='option'; row.textContent=value; row.style.height='28px';
 row.onclick=()=>{window.birthday[kind]=String(value); box.innerHTML='';document.querySelector('[aria-label^="'+kind+'"]').textContent=value;};box.appendChild(row);}
}
function next(){window.submitted={email:document.querySelector('[name=email]').value,password:document.querySelector('[type=password]').value,
 code:document.querySelector('[placeholder="Enter 6-digit code"]').value,birthday:window.birthday};
 document.body.innerHTML='<input name="username" placeholder="Username"><button onclick="finish()">Sign up</button>';}
async function finish(){window.submitted.username=document.querySelector('[name=username]').value; await fetch('/fixture-session');
 document.body.dataset.submitted=JSON.stringify(window.submitted);
 document.body.innerHTML='<a href="/@'+window.submitted.username+'">Profile</a>';
 const state=document.createElement('script');state.id='__UNIVERSAL_DATA_FOR_REHYDRATION__';state.type='application/json';
 state.textContent=JSON.stringify({__DEFAULT_SCOPE__:{'webapp.app-context':{isLogin:true,user:{id:'123',uniqueId:window.submitted.username}}}});
 document.body.appendChild(state);}
</script></body></html>'''


def instagram_html(*, delay=False):
    years = ''.join(f'<option value="{year}">{year}</option>' for year in range(2026, 1899, -1))
    months = ''.join(f'<option value="{month}">{month}</option>' for month in range(1, 13))
    days = ''.join(f'<option value="{day}">{day}</option>' for day in range(1, 32))
    birthday = f'<select name="year">{years}</select><select name="month">{months}</select><select name="day">{days}</select><button onclick="code()">Next</button>'
    html = '''<!doctype html><html lang="en"><body>
<input name="emailOrPhone"><input name="fullName"><input name="username"><input name="password" type="password">
<button onclick="signup()">Sign up</button><script>
function signup(){window.submitted={};for(const input of document.querySelectorAll('input')) window.submitted[input.name]=input.value;
 document.body.innerHTML=BIRTHDAY;}
function code(){window.submitted.birthday={};for(const input of document.querySelectorAll('select')) window.submitted.birthday[input.name]=input.value;
 document.body.innerHTML='<input name="email_confirmation_code"><button onclick="finish()">Confirm</button>';}
async function finish(){window.submitted.code=document.querySelector('input').value;await fetch('/fixture-session');
 document.body.dataset.submitted=JSON.stringify(window.submitted);
 document.body.innerHTML='<a href="/'+window.submitted.username+'/">Profile</a>';}
</script></body></html>'''
    if delay:
        html = html.replace('document.body.innerHTML=BIRTHDAY;', "document.body.innerHTML='<p>Loading</p>';setTimeout(()=>{document.body.innerHTML=BIRTHDAY;},400);")
    return html.replace('BIRTHDAY', json.dumps(birthday))


@unittest.skipUnless(os.getenv("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 for local browser fixtures")
class RegistrationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = AccountStorage(self.directory.name)
        self.proxy = Proxy("http", "proxy.test", 8080, "test-user", "test-password")
        self.storage.reserve_proxy("fixture-account", [self.proxy])
        self.storage.save_fingerprint("fixture-account", {"config": {"test": True}})
        self.mail = MailAccount("test@example.com", "test-mail-password")
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        executable = await asyncio.to_thread(prepare_browser_executable)
        self.browser = await self.playwright.firefox.launch(headless=True, executable_path=executable)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(locale="en-US")
        self.addAsyncCleanup(self.context.close)
        self.page = await self.context.new_page()
        self.mail_calls = 0
        self.profile = None

    async def open_fixture(self, platform, *, challenge=False, taken=False, suggestion=False, wrong_code=False, delay=False):
        self.profile = replace(generate_profile(platform), birth_date=date(2000, 2, 29).isoformat())
        self.storage.reserve_profile("fixture-account", self.profile, self.mail)
        html = TIKTOK_HTML if platform == "tiktok" else instagram_html(delay=delay)
        if challenge:
            html = html.replace('<body>', '<body><div id="captcha-fixture">Manual test checkpoint</div>')
        if taken:
            function = 'finish' if platform == 'tiktok' else 'signup'
            prefix = 'async ' if platform == 'tiktok' else ''
            offered = '<button data-testid="username-suggestion">fixture_suggested</button>' if suggestion else ''
            rejection = f'''if(document.querySelector('[name=username]').value==={json.dumps(self.profile.username)}){{
                if(!document.querySelector('[role=alert]'))document.body.insertAdjacentHTML('beforeend', '<div role="alert">Username already taken</div>{offered}');return;}}'''
            html = html.replace(prefix + 'function ' + function + '(){', prefix + 'function ' + function + '(){' + rejection)
        if wrong_code:
            html = html.replace('function next(){', '''function next(){
                if(document.querySelector('[placeholder="Enter 6-digit code"]').value==='654321'){
                    document.body.insertAdjacentHTML('beforeend','<div role="alert">Incorrect code</div>');return;}''')

        async def route(request):
            if request.request.url.endswith('/fixture-session'):
                await request.fulfill(status=200, body='ok', headers={'set-cookie':'sessionid=test-session; Path=/; Secure; HttpOnly'})
            elif '/api/v1/accounts/current_user/' in request.request.url:
                submitted = await self.page.locator('body').get_attribute('data-submitted')
                user = json.loads(submitted) if submitted else {}
                await request.fulfill(status=200, json={'user': {'pk': '123', 'username': user.get('username')}})
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)
        # All browser traffic is local fixture data; no provider receives credentials.
        await self.context.route("**/*", route)
        await self.page.goto(TIKTOK_SIGNUP if platform == "tiktok" else INSTAGRAM_SIGNUP)
        return SimpleNamespace(page=self.page, request=SimpleNamespace(unique_key="fixture-account"))

    async def get_letters(self, account):
        self.mail_calls += 1
        if self.mail_calls == 1:
            return []
        return [MailLetter("fixture-code", "login@" + self.profile.platform + ".com", "Code", "Code: 654321", time.time())]

    async def test_tiktok_form_and_generated_metadata_are_saved_after_identity_confirmation(self):
        context = await self.open_fixture("tiktok")
        result = await TikTokClient(self, self.storage).register(context, self.mail)
        submitted = json.loads(await self.page.locator('body').get_attribute('data-submitted'))
        self.assertEqual(submitted["username"], result.username)
        self.assertEqual(submitted["email"], self.mail.email)
        self.assertEqual(submitted["password"], result.password)
        self.assertEqual(submitted["code"], "654321")
        self.assertEqual(submitted["birthday"], {"Year": "2000", "Month": "February", "Day": "29"})
        account = self.storage.load_account("fixture-account")
        self.assertEqual(account.profile.registration_state, "complete")
        self.assertTrue(account.profile.created_at)
        self.assertTrue(any(cookie["name"] == "sessionid" for cookie in account.cookies))
        self.assertEqual(account.mail_account, self.mail)
        self.assertEqual(account.profile.platform_user_id, "123")

    async def test_instagram_form_uses_name_username_and_birthday(self):
        context = await self.open_fixture("instagram")
        result = await InstagramClient(self, self.storage).register(context, self.mail)
        submitted = json.loads(await self.page.locator('body').get_attribute('data-submitted'))
        self.assertEqual(submitted["fullName"], result.name)
        self.assertEqual(submitted["username"], result.username)
        self.assertEqual(submitted["password"], result.password)
        self.assertEqual(submitted["birthday"], {"year": "2000", "month": "2", "day": "29"})
        self.assertEqual(self.storage.load_account("fixture-account").profile.registration_state, "complete")

    async def test_security_check_requires_manual_handler_and_does_not_submit_first(self):
        context = await self.open_fixture("tiktok", challenge=True)
        client = TikTokClient(self, self.storage)
        with self.assertRaises(ManualCheckRequired):
            await client.register(context, self.mail)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), 'false')
        self.assertEqual(self.storage.load_account("fixture-account").profile.registration_state, "pending")
        calls = []
        async def manual(page):
            calls.append(True)
            await page.locator('#captcha-fixture').evaluate('(node) => node.remove()')
        result = await client.register(context, self.mail, manual_handler=manual)
        self.assertEqual(calls, [True])
        self.assertEqual(result.registration_state, "complete")

    async def test_tiktok_accepts_platform_suggestion_after_explicit_rejection(self):
        context = await self.open_fixture("tiktok", taken=True, suggestion=True)
        original = self.profile.username
        result = await TikTokClient(self, self.storage).register(context, self.mail)
        self.assertEqual(result.username, "fixture_suggested")
        self.assertNotEqual(result.username, original)
        self.assertEqual(self.storage.load_account("fixture-account").profile.username, result.username)

    async def test_instagram_generates_replacement_when_no_suggestion_exists(self):
        context = await self.open_fixture("instagram", taken=True)
        original = self.profile.username
        result = await InstagramClient(self, self.storage).register(context, self.mail)
        self.assertNotEqual(result.username, original)
        submitted = json.loads(await self.page.locator('body').get_attribute('data-submitted'))
        self.assertEqual(submitted['username'], result.username)

    async def test_invalid_code_is_replaced_by_a_new_letter_without_resend(self):
        context = await self.open_fixture("tiktok", wrong_code=True)
        async def letters(account):
            self.mail_calls += 1
            if self.mail_calls == 1:
                return []
            code = '654321' if self.mail_calls == 2 else '123456'
            return [MailLetter(code, 'login@tiktok.com', 'Code', 'Code: ' + code, time.time())]
        client = SimpleNamespace(get_letters=letters)
        await TikTokClient(client, self.storage).register(context, self.mail)
        submitted = json.loads(await self.page.locator('body').get_attribute('data-submitted'))
        self.assertEqual(submitted['code'], '123456')

    async def test_loading_gap_does_not_skip_birthday_stage(self):
        context = await self.open_fixture("instagram", delay=True)
        result = await InstagramClient(self, self.storage).register(context, self.mail)
        submitted = json.loads(await self.page.locator('body').get_attribute('data-submitted'))
        self.assertEqual(submitted['birthday'], {'year': '2000', 'month': '2', 'day': '29'})
        self.assertEqual(result.registration_state, 'complete')
