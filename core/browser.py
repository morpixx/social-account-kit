from typing import Any, Literal
import asyncio
import builtins
from hashlib import sha256
from importlib.metadata import version
import json
from datetime import timedelta
from pathlib import Path
import os
import re
import sys
from tempfile import NamedTemporaryFile
from contextvars import ContextVar

from camoufox import AsyncNewBrowser
from camoufox.pkgman import launch_path
from camoufox import utils as camoufox_utils
from crawlee.browsers import (
    BrowserPool,
    CrawleePage,
    PlaywrightBrowserController,
    PlaywrightBrowserPlugin,
)
from crawlee.proxy_configuration import ProxyInfo
from typing_extensions import override

from core.models import Proxy
from core.proxy import validate_proxy
from core.storage import AccountStorage


CamoufoxOS = Literal["windows", "macos", "linux"]
HeadlessMode = bool | Literal["virtual"]
_account_proxy = ContextVar[dict[str, str] | None]("account_proxy", default=None)
_saved_account = ContextVar("saved_account", default=None)


def _print_camoufox_message(*args, **kwargs) -> None:
    if args and isinstance(args[0], str) and args[0].startswith("Skipping unknown patch "):
        return
    builtins.print(*args, **kwargs)


# Camoufox 0.5.6 prints unsupported fingerprint values even with debug disabled.
camoufox_utils.print = _print_camoufox_message


def prepare_browser_executable(executable_path: str | None = None) -> str | None:
    if sys.platform != "win32":
        return executable_path

    original = Path(executable_path or launch_path())

    if original.name != "camoufox.exe":
        return str(original)

    data = original.read_bytes()
    pattern = rb'<dependency>\s*<dependentAssembly>\s*<assemblyIdentity(?=[^>]*name="mozglue")[^>]+/>\s*</dependentAssembly>\s*</dependency>'
    matches = list(re.finditer(pattern, data))

    if not matches:
        return str(original)

    if len(matches) != 1 or not original.with_name("mozglue.dll").is_file():
        raise RuntimeError("Unexpected Camoufox Windows manifest")

    match = matches[0]
    patched = data[:match.start()] + b" " * (match.end() - match.start()) + data[match.end():]
    target = original.with_name("camoufox-windows.exe")

    if target.exists() and target.read_bytes() == patched:
        return str(target)

    # Camoufox #650: fix the Windows manifest in a copy, preserving the official binary.
    with NamedTemporaryFile(dir=original.parent, suffix=".tmp", delete=False) as file:
        file.write(patched)
        temporary = Path(file.name)

    try:
        os.replace(temporary, target)
    except PermissionError:
        if not target.exists() or target.read_bytes() != patched:
            raise
    finally:
        temporary.unlink(missing_ok=True)

    return str(target)


def build_proxy(proxy: Proxy) -> dict[str, str]:
    validate_proxy(proxy)

    proxy_options = {
        "server": f"{proxy.scheme}://{proxy.host.lower()}:{proxy.port}",
    }

    if proxy.username:
        proxy_options["username"] = proxy.username

    if proxy.password:
        proxy_options["password"] = proxy.password

    return proxy_options


class CamoufoxPlugin(PlaywrightBrowserPlugin):
    @override
    async def new_browser(self) -> PlaywrightBrowserController:
        if not self._playwright:
            raise RuntimeError(
                "Playwright browser plugin is not initialized."
            )

        launch_options = dict(self.browser_launch_options)
        account_proxy = _account_proxy.get()
        if account_proxy is not None:
            launch_options["proxy"] = account_proxy
        executable_path = await asyncio.to_thread(
            prepare_browser_executable, launch_options.get("executable_path"),
        )

        if executable_path:
            launch_options["executable_path"] = executable_path

        launch_options["env"] = {
            key: value for key, value in os.environ.items()
            if key not in ("NOTLETTERS_TOKEN", "LZT_TOKEN", "DEBUG", "PWDEBUG")
            and not key.startswith("CAMOU_CONFIG_")
        }
        saved_context = _saved_account.get()
        if saved_context is not None:
            storage, account = saved_context
            # Firefox's per-session Canvas randomization prevents account restoration.
            launch_options["firefox_user_prefs"] = {
                **launch_options["firefox_user_prefs"],
                "privacy.baselineFingerprintingProtection": False,
            }
            if not isinstance(launch_options["headless"], bool):
                raise ValueError("Saved Windows accounts require boolean headless mode")
            browser_hash = await asyncio.to_thread(lambda: sha256(Path(executable_path).read_bytes()).hexdigest())
            library_version = version("camoufox")
            if account.fingerprint:
                fingerprint = account.fingerprint
                if fingerprint.get("browser_sha256") != browser_hash or fingerprint.get("camoufox_version") != library_version:
                    raise RuntimeError("Saved account requires its original Camoufox version")
                launch_options["config"] = dict(fingerprint["config"])
                launch_options["os"] = fingerprint["os"]
                launch_options["i_know_what_im_doing"] = True
                launch_options.pop("geoip", None)
                launch_options.pop("locale", None)
            elif account.cookies:
                raise ValueError("Existing account cookies have no saved browser fingerprint")

            options = await asyncio.to_thread(camoufox_utils.launch_options, **launch_options)
            chunks = sorted((int(key.rsplit("_", 1)[1]), value) for key, value in options["env"].items()
                            if key.startswith("CAMOU_CONFIG_"))
            config = json.loads("".join(value for index, value in chunks))

            if account.fingerprint:
                config = dict(account.fingerprint["config"])
                target_os = {"windows": "win", "macos": "mac", "linux": "lin"}[account.fingerprint["os"]]
                options["env"] = {key: value for key, value in options["env"].items() if not key.startswith("CAMOU_CONFIG_")}
                options["env"].update(camoufox_utils.get_env_vars(config, target_os, path=Path(executable_path)))
                options["firefox_user_prefs"] = dict(account.fingerprint["firefox_user_prefs"])
            else:
                platform = config.get("navigator.platform", "")
                target_os = "windows" if platform == "Win32" else "macos" if platform == "MacIntel" else "linux"
                fingerprint = {"config": config, "os": target_os, "browser_sha256": browser_hash,
                               "camoufox_version": library_version,
                               "firefox_user_prefs": options.get("firefox_user_prefs", {})}
                await asyncio.to_thread(storage.save_fingerprint, account.account_id, fingerprint)

            browser = await AsyncNewBrowser(self._playwright, from_options=options)
        else:
            browser = await AsyncNewBrowser(self._playwright, **launch_options)

        return PlaywrightBrowserController(
            browser=browser,
            max_open_pages_per_browser=self.max_open_pages_per_browser,
            use_incognito_pages=True,
            header_generator=None,
        )


class AccountBrowserPool(BrowserPool):
    def __init__(self, *, account_storage: AccountStorage | None = None, **options):
        super().__init__(**options)
        self.account_storage = account_storage

    @override
    async def new_page(
        self, *, page_id: str | None = None,
        browser_plugin: PlaywrightBrowserPlugin | None = None,
        proxy_info: ProxyInfo | None = None,
    ) -> CrawleePage:
        plugin = browser_plugin or self.plugins[0]
        proxy_options = plugin.browser_launch_options.get("proxy")

        if proxy_info is not None:
            selected_proxy = build_proxy(Proxy(
                proxy_info.scheme, proxy_info.hostname, proxy_info.port,
                proxy_info.username, proxy_info.password,
            ))
            if proxy_options is not None and proxy_options != selected_proxy:
                raise ValueError("Account proxy differs from the fixed browser proxy")
            proxy_options = selected_proxy

        if not proxy_options:
            raise ValueError("Each account requires a proxy")

        token = _account_proxy.set(proxy_options)
        account_token = None
        try:
            if self.account_storage is not None:
                account_id = getattr(proxy_info, "account_id", None)
                if not account_id:
                    raise ValueError("A stable account request key is required")
                account = await asyncio.to_thread(self.account_storage.load_account, account_id)
                if account is None:
                    raise ValueError("Account proxy must be reserved first")
                if build_proxy(account.proxy) != proxy_options:
                    raise ValueError("Account must use its saved proxy")
                account_token = _saved_account.set((self.account_storage, account))
            return await super().new_page(
                page_id=page_id, browser_plugin=browser_plugin, proxy_info=proxy_info,
            )
        finally:
            if account_token is not None:
                _saved_account.reset(account_token)
            _account_proxy.reset(token)


def create_browser_pool(
    *,
    headless: HeadlessMode = False,
    humanize: bool | float = True,
    os: CamoufoxOS | list[CamoufoxOS] | None = None,
    locale: str | list[str] | None = None,
    geoip: bool | str = False,
    proxy: Proxy | None = None,
    block_images: bool = False,
    block_webrtc: bool = True,
    enable_cache: bool = False,
    max_open_pages_per_browser: int = 1,
    retire_browser_after_page_count: int = 1,
    extra_options: dict[str, Any] | None = None,
    account_storage: AccountStorage | None = None,
) -> BrowserPool:
    if type(max_open_pages_per_browser) is not int or max_open_pages_per_browser != 1:
        raise ValueError("Each account must use a separate browser")

    if type(retire_browser_after_page_count) is not int or retire_browser_after_page_count != 1:
        raise ValueError("Browser fingerprints must not be reused between accounts")

    if enable_cache:
        raise ValueError("Browser cache must remain disabled")

    launch_options: dict[str, Any] = {
        "headless": headless,
        "humanize": humanize,
        "block_images": block_images,
        "block_webrtc": block_webrtc,
        "enable_cache": enable_cache,
        "firefox_user_prefs": {
            "network.proxy.allow_hijacking_localhost": True,
            "browser.cache.disk.enable": False,
            "browser.cache.memory.enable": False,
            "browser.cache.offline.enable": False,
            "network.http.use-cache": False,
        },
    }

    if os is not None:
        launch_options["os"] = os

    if locale is not None:
        launch_options["locale"] = locale

    if geoip:
        launch_options["geoip"] = geoip

    if proxy is not None:
        launch_options["proxy"] = build_proxy(proxy)

    if extra_options:
        if extra_options.get("enable_cache") or extra_options.get("persistent_context") or "user_data_dir" in extra_options:
            raise ValueError("Persistent browser state is not allowed")
        if any(key in extra_options for key in ("from_options", "fingerprint", "config", "env", "args", "debug", "launch_options")):
            raise ValueError("Account fingerprints and launch state must be generated privately")
        extra_prefs = extra_options.get("firefox_user_prefs", {})
        prefs = {**extra_prefs, **launch_options["firefox_user_prefs"]}
        launch_options.update(extra_options)
        launch_options["firefox_user_prefs"] = prefs

    plugin = CamoufoxPlugin(
        browser_type="firefox",
        browser_launch_options=launch_options,
        max_open_pages_per_browser=max_open_pages_per_browser,
        use_incognito_pages=True,
    )

    pool = AccountBrowserPool(
        account_storage=account_storage,
        plugins=[plugin],
        retire_browser_after_page_count=retire_browser_after_page_count,
        close_inactive_browsers_interval=timedelta(seconds=5),
    )

    @pool.pre_page_create_hook
    async def restore_browser_state(page_id, browser_controller, options, proxy_info) -> None:
        saved_context = _saved_account.get()
        if saved_context is not None:
            storage, account = saved_context
            options["storage_state"] = account.browser_state or {"cookies": account.cookies, "origins": []}

    @pool.post_page_create_hook
    async def restore_session_storage(crawlee_page, browser_controller) -> None:
        saved_context = _saved_account.get()
        if saved_context is not None and saved_context[1].session_storage:
            entries = [[item["origin"], item["values"]] for item in saved_context[1].session_storage]
            script = "(() => { const saved = new Map(" + json.dumps(entries) + "); const values = saved.get(location.origin); if (values) { for (const [key, value] of Object.entries(values)) { if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, value); } } })();"
            await crawlee_page.page.context.add_init_script(script)

    @pool.post_page_close_hook
    async def close_account_browser(page_id, browser_controller) -> None:
        # A one-use browser is no longer needed after account cleanup.
        await browser_controller.close(force=True)

    return pool
