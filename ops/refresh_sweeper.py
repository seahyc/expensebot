"""Proactive JWT refresh sweeper.

Every 6h, find users whose access token expires within the next hour and
refresh + persist new tokens. Avoids users hitting expiry mid-action and —
more importantly — limits the blast radius if OmniHR rotates refresh tokens
on refresh: the sweeper keeps the DB copy current even for users who haven't
interacted recently.

Users whose refresh token is already expired are skipped (nothing we can do —
they must /pair again on their next interaction). AuthError during refresh
(refresh rejected server-side) is logged; we leave the stale row alone so the
next user action surfaces the "Session expired" reply.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Awaitable, Callable

import httpx

from bot import storage
from omnihr_client.auth import refresh_access_token
from omnihr_client.exceptions import AuthError

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
REFRESH_WINDOW = timedelta(hours=1)
# Once a user's session is expired, remind them at most this often. Low enough
# that they won't forget; high enough that they won't feel spammed if they
# ignore it for a while.
EXPIRY_RENOTIFY_AFTER = timedelta(days=7)

EXPIRY_MESSAGE = (
    "🔒 Your OmniHR session expired — I can't file claims until you reconnect.\n\n"
    "*To reconnect (~30s):*\n"
    "1. Sign in to your company's OmniHR in Chrome (it's at "
    "`<your-company>.omnihr.co`, e.g. `glints.omnihr.co`). Google SSO is fine. "
    "Your browser session probably expired too.\n"
    "2. Send /pair here. I'll reply with a 6-digit code.\n"
    "3. On that OmniHR tab, click the 💰 *ExpenseBot* icon in Chrome's toolbar "
    "(puzzle-piece menu if unpinned) → paste the code → tap *Pair*.\n\n"
    "Don't have the extension anymore? "
    "Reinstall: https://expensebot.seahyingcong.com/extension"
)

Notifier = Callable[[dict, str], Awaitable[None]]


async def sweep_once(
    *,
    base_url: str = "https://api.omnihr.co/api/v1",
    notifier: Notifier | None = None,
) -> dict:
    """Refresh tokens for every near-expiry user, then DM users whose refresh
    token is already expired so they know to /pair. Returns counts for logging.
    """
    candidates = storage.users_needing_refresh(within=REFRESH_WINDOW)
    refreshed = failed = 0

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:
        for row in candidates:
            user_id = row["id"]
            _, refresh_jwt = storage.get_omnihr_tokens(user_id)
            if not refresh_jwt:
                continue
            try:
                new = await refresh_access_token(http, refresh_jwt)
            except AuthError as e:
                # Server rejected the refresh — treat as a dead session so the
                # expiry-notify pass below picks them up on its query (their
                # refresh_expires_at may still be in the future per the JWT,
                # so forcibly mark the refresh as expired).
                log.warning("sweeper: refresh rejected for user=%s: %s", user_id, e)
                storage.mark_refresh_dead(user_id)
                failed += 1
                continue
            except Exception:
                log.exception("sweeper: refresh crashed for user=%s", user_id)
                failed += 1
                continue
            storage.set_omnihr_tokens(
                user_id,
                access_jwt=new.access_token,
                refresh_jwt=new.refresh_token,
                access_expires_at=new.access_expires_at,
                refresh_expires_at=new.refresh_expires_at,
            )
            refreshed += 1

    notified = await _notify_expired(notifier)
    return {
        "candidates": len(candidates),
        "refreshed": refreshed,
        "failed": failed,
        "notified": notified,
    }


async def _notify_expired(notifier: Notifier | None) -> int:
    expired = storage.users_with_expired_session(renotify_after=EXPIRY_RENOTIFY_AFTER)
    if not expired:
        return 0
    if notifier is None:
        log.info("sweeper: %d user(s) have expired sessions; no notifier wired", len(expired))
        return 0
    sent = 0
    for user in expired:
        try:
            await notifier(user, EXPIRY_MESSAGE)
        except Exception:
            log.exception("sweeper: failed to notify user=%s", user["id"])
            continue
        storage.mark_session_expired_notified(user["id"])
        sent += 1
    return sent


async def run_forever(notifier: Notifier | None = None) -> None:
    while True:
        try:
            result = await sweep_once(notifier=notifier)
            if result["candidates"] or result["notified"]:
                log.info("refresh sweeper: %s", result)
        except Exception:
            log.exception("refresh sweeper loop errored")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await run_forever()


if __name__ == "__main__":
    asyncio.run(main())
