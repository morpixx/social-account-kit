from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from datetime import timedelta
from urllib.parse import quote

from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import (
    PlaywrightCrawler,
    PlaywrightCrawlingContext,
)
from crawlee.proxy_configuration import ProxyConfiguration
from crawlee.sessions import SessionPool
from crawlee.storage_clients import MemoryStorageClient

from core.browser import CamoufoxOS, HeadlessMode, create_browser_pool
from core.models import Proxy
from core.proxy import validate_proxy
from core.storage import AccountStorage


PageHandler = Callable[
    [PlaywrightCrawlingContext],
    Awaitable[None],
]


class AccountMemoryStorageClient(MemoryStorageClient):
    def get_storage_client_cache_key(self, configuration):
        # Crawlee otherwise shares completed requests between separate crawlers.
        return self


class AccountProxyConfiguration(ProxyConfiguration):
    async def new_proxy_info(self, session_id, request, proxy_tier):
        info = await super().new_proxy_info(session_id, request, proxy_tier)
        if info is not None and request is not None:
            info.account_id = request.unique_key
        return info


def proxy_to_url(proxy: Proxy) -> str:
    validate_proxy(proxy)

    username = quote(proxy.username, safe="")
    password = quote(proxy.password, safe="")

    auth = ""

    if username or password:
        auth = f"{username}:{password}@"

    return (
        f"{proxy.scheme}://"
        f"{auth}{proxy.host.lower()}:{proxy.port}"
    )


def create_proxy_configuration(
    proxies: list[Proxy] | None,
    account_storage: AccountStorage | None = None,
) -> ProxyConfiguration:
    if not proxies and account_storage is None:
        raise ValueError("Proxy list must contain at least one proxy")

    proxy_urls = [
        proxy_to_url(proxy)
        for proxy in proxies or []
    ]

    if len(set(proxy_urls)) != len(proxy_urls):
        raise ValueError("Proxy list must not contain duplicates")

    assigned_proxies = {}
    assignment_lock = asyncio.Lock()

    async def new_proxy_url(session_id: str | None, request: Request | None) -> str:
        if request is not None:
            key = ("request", request.unique_key)
        elif session_id is not None:
            key = ("session", session_id)
        else:
            key = ("anonymous", len(assigned_proxies))

        async with assignment_lock:
            if key in assigned_proxies:
                return assigned_proxies[key]

            if account_storage is not None:
                if request is None:
                    raise ValueError("A stable account request key is required for saved accounts")
                proxy = await asyncio.to_thread(account_storage.reserve_proxy, request.unique_key, proxies or [])
                proxy_url = proxy_to_url(proxy)
            else:
                if len(assigned_proxies) >= len(proxy_urls):
                    raise ValueError("No unused proxies remain for a new account")
                proxy_url = proxy_urls[len(assigned_proxies)]

            assigned_proxies[key] = proxy_url
            return proxy_url

    return AccountProxyConfiguration(new_url_function=new_proxy_url)


def create_crawler(
    *,
    handler: PageHandler | None = None,
    proxies: list[Proxy] | None = None,
    headless: HeadlessMode = False,
    humanize: bool | float = True,
    os: CamoufoxOS | list[CamoufoxOS] | None = None,
    locale: str | list[str] | None = None,
    geoip: bool | str = False,
    block_webrtc: bool = True,
    max_concurrency: int = 3,
    max_request_retries: int = 2,
    navigation_timeout: int = 45,
    request_handler_timeout: int = 120,
    session_pool_size: int = 100,
    session_max_usage_count: int = 1,
    max_requests_per_crawl: int | None = None,
    account_storage: AccountStorage | None = None,
) -> PlaywrightCrawler:
    if max_concurrency < 1:
        raise ValueError(
            "max_concurrency must be at least 1"
        )

    if max_request_retries < 0:
        raise ValueError(
            "max_request_retries cannot be negative"
        )

    if navigation_timeout < 1:
        raise ValueError(
            "navigation_timeout must be at least 1 second"
        )

    if request_handler_timeout < 1:
        raise ValueError(
            "request_handler_timeout must be at least 1 second"
        )

    if session_pool_size < 1:
        raise ValueError(
            "session_pool_size must be at least 1"
        )

    if type(session_max_usage_count) is not int or session_max_usage_count != 1:
        raise ValueError(
            "Each account must use a separate one-use session"
        )

    proxy_configuration = create_proxy_configuration(proxies, account_storage)

    browser_pool = create_browser_pool(
        headless=headless,
        humanize=humanize,
        os=os,
        locale=locale,
        geoip=geoip,
        block_webrtc=block_webrtc,
        enable_cache=False,
        max_open_pages_per_browser=1,
        account_storage=account_storage,
    )

    session_pool = SessionPool(
        max_pool_size=session_pool_size,
        create_session_settings={
            "max_usage_count": session_max_usage_count,
        },
    )

    concurrency_settings = ConcurrencySettings(
        min_concurrency=1,
        desired_concurrency=max_concurrency,
        max_concurrency=max_concurrency,
    )

    crawler = PlaywrightCrawler(
        storage_client=AccountMemoryStorageClient(),
        browser_pool=browser_pool,
        proxy_configuration=proxy_configuration,
        use_session_pool=True,
        session_pool=session_pool,
        concurrency_settings=concurrency_settings,
        max_request_retries=max_request_retries,
        max_session_rotations=0,
        max_requests_per_crawl=max_requests_per_crawl,
        navigation_timeout=timedelta(
            seconds=navigation_timeout
        ),
        request_handler_timeout=timedelta(
            seconds=request_handler_timeout
        ),
        retry_on_blocked=False,
    )

    @crawler.router.default_handler
    async def default_handler(
        context: PlaywrightCrawlingContext,
    ) -> None:
        if handler is not None:
            await handler(context)
        else:
            context.log.info("Account page opened")

        if account_storage is not None:
            proxy_info = context.proxy_info
            if proxy_info is None:
                raise RuntimeError("Account proxy is missing")
            from core.session import save_browser_session
            await save_browser_session(context.page, account_storage, context.request.unique_key)

    return crawler
