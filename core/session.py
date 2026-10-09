import asyncio
from datetime import datetime, timezone


async def save_browser_session(page, storage, account_id: str, *, registration_complete=False,
                               platform_user_id=None) -> None:
    account = await asyncio.to_thread(storage.load_account, account_id)
    if account is None:
        raise ValueError("Account proxy is missing")
    complete = True
    reason = ""
    async with asyncio.timeout(25):
        # Read cookies separately: a failed IndexedDB export must not lose the session.
        cookies = await page.context.cookies()
        try:
            async with asyncio.timeout(10):
                state = await page.context.storage_state(indexed_db=True)
            cookies = state["cookies"]
        except Exception:
            state = {"cookies": cookies, "origins": []}
            complete, reason = False, "browser_state_unavailable"
        try:
            async with asyncio.timeout(5):
                session = await page.evaluate("() => ({ origin: location.origin, values: Object.fromEntries(Object.entries(sessionStorage)) })")
            sessions = [session]
        except Exception:
            sessions = []
            complete, reason = False, "session_storage_unavailable"
        snapshot = {"complete": complete, "reason": reason,
                    "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        await asyncio.to_thread(storage.save_account, account_id, cookies, account.proxy,
                                browser_state=state, session_storage=sessions, snapshot=snapshot,
                                registration_complete=registration_complete, platform_user_id=platform_user_id)
