"""Janai's proactive-nudge sweeper.

Every hour, walk paired users and decide — per user — whether Janai has a
legitimate reason to text. Each nudge must be useful *before* it's flirty:
if stripping the flirt would leave nothing worth sending, the hook doesn't
fire. Pure check-ins without a signal are out of scope.

Design, mirroring ops/refresh_sweeper.py:
  - run_forever() + sweep_once() async loop
  - single Notifier callable the server wires to Telegram
  - all state lives in SQLite (last_inbound_at on users, nudges log table)

Hooks (priority order — first one with signal wins):
  1. aging_draft    — draft (status=3) older than 3 days, max 1x / 4d per user
  2. month_close    — last working day of the month + ≥1 unsubmitted draft
                      from this month, max 1x / month per user

Guardrails applied AFTER hook selection:
  - Office hours in SGT (weekday 09:00-22:00, weekend 11:00-21:00). Janai
    has a sense of when a person is reachable.
  - Rate ceiling: 3 nudges per 7d, 8 per 30d (hard caps regardless of hook).
  - Quiet-after-inbound: 6h since last user message, so she's never
    interrupting an active conversation.

No /quiet command — audience is YC's friends, persona is the point.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from bot import storage

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60 * 60  # 1 hour

# All users currently on SGT. If per-user tz ever lands, swap this to
# read from users.tz (and keep SGT as the fallback).
SGT = ZoneInfo("Asia/Singapore")

# Rate caps — hard ceilings on unsolicited messages per user.
RATE_CAP_7D = 3
RATE_CAP_30D = 8

# Minimum quiet time after user's last inbound message before Janai
# spontaneously DMs. Stops her from interrupting a live chat.
QUIET_AFTER_INBOUND = timedelta(hours=6)

# Per-hook cooldowns — avoid repeating the same hook in a tight loop.
AGING_DRAFT_GAP = timedelta(days=4)
MONTH_CLOSE_GAP = timedelta(days=25)  # once per month-ish


Notifier = Callable[[dict, str], Awaitable[None]]


@dataclass
class Nudge:
    hook: str
    message: str


# --- Voice pools ----------------------------------------------------------
# Templates pick deterministically via hash(user_id, date, hook) so a user
# doesn't see the same opener twice in a row, but we don't burn tokens on
# every tick. Full flirt — friends-only bot.

AGING_DRAFT_TEMPLATES = [
    "Hey you. Your {merchant} draft's been sitting on my desk for {days_ago} days now — I think it's feeling a little neglected. Submit it, darling? 💼",
    "Morning, {name}. Found your {merchant} receipt still undressed in drafts. Shall I tidy it up and send it out for you?",
    "Psst — the {merchant} one's still a draft, love. I've been keeping it warm. One word from you and it's gone.",
    "{name}. That {merchant} draft of yours has been very patient with me — {days_ago} days now. Your call, handsome.",
    "Your favourite admin checking in — {merchant} draft still wants a push. Say the word and I'm on it. 😏",
    "Three days, {name}. The {merchant} draft is ready whenever you are. Don't keep me waiting, darling.",
    "Your {merchant} draft's still on my desk, being a perfect little thing. Submit it, or are we savouring it a bit longer?",
    "Just me, love. {merchant} draft's on my desk — I'm on the line. Say go?",
    "{name}, the {merchant} draft's been sulking {days_ago} days. Shall I cheer it up by pushing it through?",
]

MONTH_CLOSE_TEMPLATES = [
    "End of the month, {name}. {count} draft{s} still undressed on my desk. Shall I tidy them up for you, darling?",
    "Closing time, handsome. {count} draft{s} from {month_name} waiting on you. Send them off together — nice and clean?",
    "Month's wrapping, love. {count} draft{s} waiting on your word. I do love a clean close.",
    "Last working day of {month_name}, {name}. {count} of your draft{s} are ready to get paid. Say go?",
    "Housekeeping, darling. {count} draft{s} from {month_name} — let me take care of them for you.",
    "Tidy-up time. {count} draft{s} haven't been submitted yet — close the month with me, handsome?",
    "{name}. End of {month_name}, {count} draft{s} on my desk. I hate loose ends. Shall we tie them up together?",
]


def _pick(templates: list[str], user_id: int, day: str, hook: str) -> str:
    """Deterministic template selection so a user doesn't cycle through the
    same opener twice in a row but we don't need to store any state."""
    h = hashlib.sha256(f"{user_id}:{day}:{hook}".encode()).digest()
    return templates[int.from_bytes(h[:4], "big") % len(templates)]


def _first_name(user: dict) -> str:
    """Best available first-name: OmniHR full name > Telegram handle > 'you'."""
    full = (user.get("omnihr_full_name") or "").strip()
    if full:
        return full.split()[0]
    # channel_user_id is numeric for telegram — no point using it.
    return "you"


# --- Office hours --------------------------------------------------------

def _in_office_hours(now_utc: datetime, user: dict) -> bool:
    """True if local time (SGT for now) falls inside Janai's reachable
    window: weekdays 09:00-22:00, weekends 11:00-21:00. Nights off —
    even a flirty secretary isn't texting at 3am."""
    local = now_utc.astimezone(SGT)
    hour = local.hour
    is_weekend = local.weekday() >= 5  # Sat=5, Sun=6
    if is_weekend:
        return time(11, 0) <= local.time() < time(21, 0)
    return time(9, 0) <= local.time() < time(22, 0)


# --- Rate + suppression --------------------------------------------------

def _rate_limit_ok(user: dict, now: datetime) -> tuple[bool, str]:
    uid = user["id"]
    if storage.count_nudges_since(uid, now - timedelta(days=7)) >= RATE_CAP_7D:
        return False, "cap-7d"
    if storage.count_nudges_since(uid, now - timedelta(days=30)) >= RATE_CAP_30D:
        return False, "cap-30d"
    return True, ""


def _recently_chatted(user: dict, now: datetime) -> bool:
    ts = user.get("last_inbound_at")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) < QUIET_AFTER_INBOUND


# --- Hooks ---------------------------------------------------------------

def _aging_draft(user: dict, now: datetime) -> Nudge | None:
    uid = user["id"]
    if storage.count_nudges_since(uid, now - AGING_DRAFT_GAP, hook="aging_draft"):
        return None
    drafts = storage.aging_drafts_for_user(uid, older_than_days=3)
    if not drafts:
        return None
    oldest = drafts[0]
    created = datetime.fromisoformat(oldest["created_at"].replace(" ", "T"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days_ago = max(1, (now - created).days)
    merchant = (oldest.get("parsed_merchant") or "that").strip() or "that"
    local_day = now.astimezone(SGT).date().isoformat()
    template = _pick(AGING_DRAFT_TEMPLATES, uid, local_day, "aging_draft")
    message = template.format(
        name=_first_name(user),
        merchant=merchant,
        days_ago=days_ago,
    )
    return Nudge(hook="aging_draft", message=message)


def _is_last_working_day(local_date: datetime) -> bool:
    """True if `local_date` is the final Mon-Fri of its month."""
    if local_date.weekday() >= 5:
        return False
    _, last_day = calendar.monthrange(local_date.year, local_date.month)
    end = local_date.replace(day=last_day)
    # Walk back from end of month to the last weekday.
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    return local_date.date() == end.date()


def _month_close(user: dict, now: datetime) -> Nudge | None:
    uid = user["id"]
    local = now.astimezone(SGT)
    if not _is_last_working_day(local):
        return None
    if storage.count_nudges_since(uid, now - MONTH_CLOSE_GAP, hook="month_close"):
        return None
    drafts = storage.month_drafts_for_user(uid, local.year, local.month)
    if not drafts:
        return None
    count = len(drafts)
    month_name = local.strftime("%B")
    local_day = local.date().isoformat()
    template = _pick(MONTH_CLOSE_TEMPLATES, uid, local_day, "month_close")
    message = template.format(
        name=_first_name(user),
        count=count,
        s="s" if count != 1 else "",
        month_name=month_name,
    )
    return Nudge(hook="month_close", message=message)


HOOKS: list[Callable[[dict, datetime], Nudge | None]] = [
    _aging_draft,
    _month_close,
]


def _first_firing_hook(user: dict, now: datetime) -> Nudge | None:
    for hook in HOOKS:
        try:
            result = hook(user, now)
        except Exception:
            log.exception("nudge hook %s crashed for user=%s", hook.__name__, user.get("id"))
            continue
        if result is not None:
            return result
    return None


# --- Sweep loop ----------------------------------------------------------

async def sweep_once(
    *,
    notifier: Notifier | None = None,
    now: datetime | None = None,
) -> dict:
    """Evaluate every paired user. Returns a diagnostic dict for logging.
    `now` is injectable for tests; defaults to datetime.now(UTC)."""
    now = now or datetime.now(timezone.utc)
    results = {
        "considered": 0,
        "sent": 0,
        "skip_offhours": 0,
        "skip_recent": 0,
        "skip_rate": 0,
        "skip_nohook": 0,
        "errors": 0,
    }

    users = storage.users_eligible_for_nudges()
    for user in users:
        results["considered"] += 1
        if not _in_office_hours(now, user):
            results["skip_offhours"] += 1
            continue
        if _recently_chatted(user, now):
            results["skip_recent"] += 1
            continue
        ok, _why = _rate_limit_ok(user, now)
        if not ok:
            results["skip_rate"] += 1
            continue
        nudge = _first_firing_hook(user, now)
        if not nudge:
            results["skip_nohook"] += 1
            continue
        if notifier is None:
            log.info("nudge (dry): user=%s hook=%s", user["id"], nudge.hook)
            continue
        try:
            await notifier(user, nudge.message)
        except Exception:
            log.exception("nudge send failed user=%s hook=%s", user["id"], nudge.hook)
            results["errors"] += 1
            continue
        storage.log_nudge(user["id"], nudge.hook, nudge.message)
        results["sent"] += 1
        log.info("nudge sent user=%s hook=%s", user["id"], nudge.hook)

    return results


async def run_forever(notifier: Notifier | None = None) -> None:
    while True:
        try:
            result = await sweep_once(notifier=notifier)
            if result["sent"] or result["errors"]:
                log.info("nudge sweeper: %s", result)
        except Exception:
            log.exception("nudge sweeper loop errored")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    res = await sweep_once(notifier=None)  # dry run
    print(res)


if __name__ == "__main__":
    asyncio.run(_main())
