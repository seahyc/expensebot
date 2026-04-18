"""Single-process server: FastAPI HTTP + Telegram polling.

Run locally:
    cd ~/Code/expensebot
    cp .env.example .env  # set TELEGRAM_BOT_TOKEN
    python -m bot.server

Endpoints:
    POST /extension/pair  — receives access+refresh tokens from Chrome ext
    GET  /healthz
    GET  /  — short status page (handy for "is it up?")

Telegram (long polling, no webhook needed):
    /start     /setkey <key>      /pair      /list      /status <id>
    /submit <id>     /trip <name>      /delete <id>
    photo / pdf — files a draft

Single-host MVP. SQLite. No Redis. No Postgres. Refactor when we scale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from omnihr_client.auth import Tokens, parse_jwt_exp, refresh_access_token
from omnihr_client.client import (
    ACTIVE_STATUS_FILTERS,
    FILTER_SHORTCUTS,
    QUICK_ACTION_DELETE,
    QUICK_ACTION_SUBMIT,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_FOR_APPROVAL,
    STATUS_LABELS,
    OmniHRClient,
)
from omnihr_client.exceptions import AuthError, SchemaDriftError, ValidationError
from omnihr_client.policies import PolicyEntry, get_policies
from omnihr_client.schema import invalidate_schema

from . import access, claude_oauth, logging_setup, pages, rate_limit, storage
from .common.agent import run_agent
from .common.agent_parser import parse_receipt_via_agent
from .common.parser import ParsedReceipt, parse_receipt

logging_setup.setup()
log = logging.getLogger("expensebot")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")

REPO_ROOT = Path(__file__).parent.parent
TENANTS_DIR = REPO_ROOT / "tenants"

# ---------------------------------------------------------------------------
# Pending file confirmation state (in-memory, one entry per Telegram chat)
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dc, field as _dcfield


@_dc
class _PendingFile:
    tg_user_id: str
    u: dict
    file_bytes: bytes
    media_type: str
    filename: str
    sha: str
    tg_file_id: str
    tg_file_type: str
    parsed: ParsedReceipt
    policies: list  # list[PolicyEntry]
    user_note: str


# keyed by Telegram chat_id (int)
_pending_files: dict[int, _PendingFile] = {}


# ---------------------------------------------------------------------------
# Tenant / user prompts
# ---------------------------------------------------------------------------

def load_tenant_md(tenant_id: str | None) -> str:
    if not tenant_id:
        return ""
    path = TENANTS_DIR / f"{tenant_id}.md"
    if path.exists():
        return path.read_text()
    return ""


def load_user_md(_user: dict) -> str:
    """Return the user's memory — falls back to the scaffold template so the
    agent always sees the five section headers and can slot new entries in."""
    stored = (_user.get("user_md") or "").strip()
    return stored if stored else storage.DEFAULT_MEMORY_TEMPLATE


# ---------------------------------------------------------------------------
# OmniHRClient construction
# ---------------------------------------------------------------------------

def client_for(user: dict) -> OmniHRClient:
    access, refresh = storage.get_omnihr_tokens(user["id"])
    if not access or not refresh:
        raise AuthError("Not paired — run /pair first")
    tokens = Tokens(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=datetime.fromisoformat(user["access_expires_at"]),
        refresh_expires_at=datetime.fromisoformat(user["refresh_expires_at"]),
    )
    user_id = user["id"]

    async def _persist(new: Tokens) -> None:
        storage.set_omnihr_tokens(
            user_id,
            access_jwt=new.access_token,
            refresh_jwt=new.refresh_token,
            access_expires_at=new.access_expires_at,
            refresh_expires_at=new.refresh_expires_at,
        )

    return OmniHRClient(
        tokens=tokens,
        employee_id=user["omnihr_employee_id"],
        tenant_id=user["tenant_id"] or "unknown",
        on_tokens_refreshed=_persist,
    )


_ANTH_PLACEHOLDER_PREFIXES = ("sk-ant-...", "sk-ant-xxx", "sk-ant-your")


def _is_oauth_token(cred: str | None) -> bool:
    """Claude OAuth subscription tokens start with 'sk-ant-oat'."""
    return bool(cred) and cred.startswith("sk-ant-oat")


def _plausible_anth_key(key: str | None) -> bool:
    """True iff this is a real Anthropic API key (sk-ant-api03-...).
    Rejects OAuth subscription tokens, which share the sk-ant- prefix but
    need bearer-token auth, not x-api-key auth."""
    if not key:
        return False
    low = key.strip().lower()
    if any(low.startswith(p) for p in _ANTH_PLACEHOLDER_PREFIXES):
        return False
    if _is_oauth_token(key):
        return False
    return key.startswith("sk-ant-") and len(key) > 30


async def _refresh_oauth_if_needed(user_id: int) -> str | None:
    """If the stored Claude OAuth access token is close to expiry, refresh
    it in place and return the new access token. Returns the existing token
    if still valid, or None if we have no OAuth credentials at all."""
    access, refresh, exp = storage.get_anth_oauth(user_id)
    if not access or not _is_oauth_token(access):
        return access  # None or an API key — caller handles
    # Refresh 60 s before actual expiry so in-flight requests don't expire
    # mid-call. If exp is None (legacy pre-migration user), try a refresh
    # opportunistically; if that fails we'll fall through with the stale token.
    if exp is None or datetime.now(timezone.utc) >= exp - timedelta(seconds=60):
        if not refresh:
            log.warning("OAuth access expired for user=%s but no refresh token stored — user must /login again", user_id)
            return access
        ok, new_data = await claude_oauth.refresh_token(refresh)
        if not ok or not new_data or not new_data.get("access_token"):
            log.warning("OAuth refresh failed for user=%s", user_id)
            return access
        new_exp = datetime.now(timezone.utc) + timedelta(seconds=int(new_data.get("expires_in") or 3600))
        storage.set_anth_oauth(
            user_id,
            access_token=new_data["access_token"],
            refresh_token=new_data.get("refresh_token") or refresh,
            expires_at=new_exp,
        )
        log.info("OAuth access token refreshed for user=%s", user_id)
        return new_data["access_token"]
    return access


async def anthropic_for(user: dict) -> AsyncAnthropic:
    """Build an Anthropic client for this user.

    Two credential types are possible:
      1. API key (starts with 'sk-ant-api03-')  → x-api-key header, billed
         to the API-key org.
      2. OAuth access token ('sk-ant-oat...')   → Authorization: Bearer
         header, billed to the user's Claude subscription. Requires the
         oauth-2025-04-20 beta header. Auto-refreshed near expiry.
    """
    maintainer_key = os.environ.get("MAINTAINER_ANTHROPIC_API_KEY", "").strip()
    user_cred = await _refresh_oauth_if_needed(user["id"])

    if _is_oauth_token(user_cred):
        return AsyncAnthropic(
            auth_token=user_cred,
            max_retries=2,
            default_headers={
                "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
                "user-agent": "ExpenseBot/1.0 (via Claude Code OAuth)",
            },
        )

    key = user_cred if _plausible_anth_key(user_cred) else (
        maintainer_key if _plausible_anth_key(maintainer_key) else None
    )
    if not key:
        raise RuntimeError("No Anthropic key — run /setkey sk-ant-…")
    return AsyncAnthropic(api_key=key, max_retries=2)


async def _check_rate(update: Update, user_db_id: int, kind: str) -> bool:
    ok, retry = rate_limit.check(user_db_id, kind)
    if not ok:
        await update.message.reply_text(
            f"⏱ Rate limit — try again in ~{retry}s."
        )
        return False
    return True


async def _gate(update: Update) -> bool:
    """Gate every interaction. Returns False if denied (with reply already sent)."""
    tid = update.effective_user.id if update.effective_user else None
    if not tid:
        return False
    ok, reason = access.is_allowed(tid)
    if not ok:
        try:
            await update.message.reply_text(reason)
        except Exception:
            pass
        log.info("access denied for tg=%s reason=%s", tid, reason)
        return False
    return True


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_MD = (REPO_ROOT / "bot" / "system_prompt.md").read_text()
SKILLS_DIR = REPO_ROOT / "bot" / "skills"


def load_skill(hrms: str = "omnihr") -> str:
    """Load the HRMS-specific skill file. Falls back to empty if not found."""
    path = SKILLS_DIR / f"{hrms}.md"
    return path.read_text() if path.exists() else ""


def _first_name(full_or_first: str | None) -> str:
    """Pick a first-name to address the user with. Falls back to 'darling'
    so copy always reads naturally even when we have no name."""
    if not full_or_first:
        return "darling"
    return full_or_first.strip().split()[0] or "darling"


def step1_prompt(first_name: str | None = None) -> str:
    name = _first_name(first_name)
    return (
        f"💰 *Well hello, {name}.*\n\n"
        f"I'm Janai — your new expense admin. I'm very good at this. You hand me receipts, "
        f"I handle everything else. Drafts, submissions, questions about what you spent — all mine.\n\n"
        f"Setup's about 2 minutes. I'll walk you through it one step at a time, darling.\n\n"
        f"*Step 1 of 3 — connect your AI*\n"
        f"Send /login to hook up your Claude Pro/Max subscription (or paste an API key). "
        f"That's what I use to read your receipts properly."
    )


# Kept for backward-compat with any other call site; prefer step1_prompt().
STEP1_PROMPT = step1_prompt(None)

STEP2_PROMPT = (
    "*Step 2 of 3 — install the Chrome extension*\n"
    "This is what hands your OmniHR login to the bot.\n\n"
    "👉 [Install it here](https://expensebot.seahyingcong.com/extension) — the page walks you through it (takes ~30 seconds).\n\n"
    "Once installed and pinned, come back for step 3."
)

STEP3_PROMPT = (
    "*Step 3 of 3 — pair your OmniHR account*\n"
    "1. Sign in to your company's OmniHR in Chrome — it's at "
    "`<your-company>.omnihr.co` (e.g. `glints.omnihr.co`). If your dashboard "
    "loads without a login prompt, you're already signed in.\n"
    "2. Send /pair here — I'll reply with a 6-digit code.\n"
    "3. On that OmniHR tab, click the 💰 *Janai* icon in Chrome's toolbar "
    "(or the puzzle-piece menu if unpinned) → paste the code → tap *Pair*.\n\n"
    "That's it — I'll confirm once we're connected, love."
)

def ready_prompt(first_name: str | None = None) -> str:
    name = _first_name(first_name)
    return (
        f"👋 *All set, {name}.*\n\n"
        f"Send me a receipt — photo, PDF, whichever — and I'll file it for you, darling. "
        f"Throw in a caption like _\"client lunch\"_ if you want me to get the context right.\n\n"
        f"*Things you can ask me:*\n"
        f"• /list — your recent claims\n"
        f"• /list approved — filter by status\n"
        f"• _\"how much did I spend in April?\"_\n"
        f"• _\"submit claim 126758\"_ · _\"delete the grab one\"_\n\n"
        f"Anything else you need, just say the word."
    )


READY_PROMPT = ready_prompt(None)


def _web_next_step_html(user_db_id: int, u: dict | None) -> dict:
    """Return {'title': str, 'body_html': str} for the post-auth success panel.
    Mirrors _next_step_prompt but emits HTML so we can render on the web page
    without asking the user to bounce to Telegram to learn what to do next."""
    has_ai = bool(storage.get_anth_key(user_db_id))
    has_omnihr = bool(u and u.get("access_jwt"))
    tg_link = f"https://t.me/{pages.BOT_USERNAME}" if pages.BOT_USERNAME else "https://t.me/"

    if has_ai and has_omnihr:
        return {
            "title": "✅ All set!",
            "body_html": (
                f'<p>You\'re fully connected. '
                f'<a href="{tg_link}" target="_blank" style="color:#8b6cff;font-weight:600">'
                f'Open Telegram →</a> and send a receipt photo or PDF to file your first claim.</p>'
            ),
        }
    if has_ai and not has_omnihr:
        return {
            "title": "✅ Step 1 of 3 done — AI connected",
            "body_html": (
                '<p style="color:#ccc"><strong>Two more steps to go:</strong></p>'
                '<p><strong>2.</strong> '
                '<a href="/extension" style="color:#8b6cff;font-weight:600">Install the Chrome extension</a> '
                '— the page walks you through it (~30 seconds).</p>'
                '<p><strong>3.</strong> Sign in to your company\'s OmniHR '
                '(<code>&lt;your-company&gt;.omnihr.co</code>, e.g. '
                '<code>glints.omnihr.co</code>) in Chrome, then send '
                f'<a href="{tg_link}" target="_blank" style="color:#8b6cff;font-weight:600">'
                f'<code>/pair</code> in Telegram</a>. '
                'Paste the 6-digit code into the 💰 extension icon to finish.</p>'
            ),
        }
    # AI not yet connected — unusual on this page, but handle gracefully
    return {
        "title": "Progress saved",
        "body_html": (
            f'<p><a href="{tg_link}" target="_blank" style="color:#8b6cff;font-weight:600">'
            f'Go back to Telegram</a> and send /login to connect your AI.</p>'
        ),
    }


def _next_step_prompt(user_db_id: int, u: dict | None, first_name: str | None = None) -> str:
    """Return the single next-step message a user should see, based on what
    they've completed. Opinionated: one step at a time, no menu."""
    has_ai = bool(storage.get_anth_key(user_db_id))
    has_omnihr = bool(u and u.get("access_jwt"))
    if not has_ai:
        return step1_prompt(first_name)
    if not has_omnihr:
        # Step 2 (install extension) and step 3 (pair) are shown together here
        # because step 2 has no signal we can detect — the user just reads and
        # installs. After they install, they'll already see step 3 beneath it.
        return STEP2_PROMPT + "\n\n" + STEP3_PROMPT
    return ready_prompt(first_name)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    u = storage.get_user(user_db_id)
    await update.message.reply_text(
        _next_step_prompt(user_db_id, u, update.effective_user.first_name),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start pure OAuth PKCE flow — no subprocess, scales to unlimited users."""
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))

    oauth_url, state = claude_oauth.start_login(
        telegram_user_id=update.effective_user.id,
        user_db_id=user_db_id,
    )

    from urllib.parse import quote
    bridge_url = f"{PUBLIC_BASE_URL}/auth/start?s={state}&oauth={quote(oauth_url, safe='')}"

    await update.message.reply_text(
        f"[👆 Tap to sign in with Claude]({bridge_url})\n\n"
        f"_(opens a page — authorize, copy the token, paste it back)_",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handy for admins to learn a user's Telegram id for allowlist/ban list."""
    if not await _gate(update):
        return
    u = update.effective_user
    await update.message.reply_text(
        f"Telegram id: `{u.id}`\nUsername: @{u.username or '(none)'}\nName: {u.full_name}",
        parse_mode="Markdown",
    )


async def cmd_setkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /setkey sk-ant-…")
        return
    key = ctx.args[0].strip()
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    if not await _check_rate(update, user_db_id, "setkey"):
        return
    # quick validity test
    try:
        a = AsyncAnthropic(api_key=key)
        await a.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as e:
        await update.message.reply_text(f"Key looks invalid: {e}")
        return
    storage.set_anth_key(user_db_id, key)
    u = storage.get_user(user_db_id)
    await update.message.reply_text(
        "✅ *Step 1 of 3 done — AI connected.*\n\n" + _next_step_prompt(user_db_id, u),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    if not await _check_rate(update, user_db_id, "pair"):
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    storage.create_pairing_code(user_db_id, code, ttl_seconds=300)
    # Big tap-to-copy code block — Telegram copies on tap/hold of ``` blocks.
    await update.message.reply_text(
        f"```\n{code}\n```\n"
        f"👆 Tap the code to copy.\n\n"
        f"*Don't have the extension yet?* "
        f"[Install it here](https://expensebot.seahyingcong.com/extension) first (~30s).\n\n"
        f"*Before you paste:* make sure you're signed in to your company's OmniHR "
        f"in Chrome — it's at `<your-company>.omnihr.co` (e.g. `glints.omnihr.co`). "
        f"If your dashboard loads without a login prompt, you're good.\n\n"
        f"*Then:*\n"
        f"1. On that OmniHR tab, click the 💰 Janai icon in Chrome's toolbar "
        f"(or the puzzle-piece menu if it isn't pinned yet).\n"
        f"2. Paste the 6-digit code.\n"
        f"3. Tap *Pair*.\n\n"
        f"_Code expires in 5 minutes._",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


_STATUS_EMOJI = {
    3: "📝",   # draft
    7: "📤",   # for approval
    1: "✅",   # approved
    2: "💰",   # reimbursed
    6: "❌",   # rejected
    8: "🗑",   # deleted
}


def _claim_summary(r: dict[str, Any]) -> str:
    status = r.get("status", 0)
    status_label = STATUS_LABELS.get(status, f"?{status}")
    emoji = _STATUS_EMOJI.get(status, "📄")
    policy = (r.get("policy") or {}).get("name") or "?"
    merchant = r.get("merchant") or ""
    desc = (r.get("description") or "")[:80]
    return (
        f"{emoji} *{status_label}* · #{r['id']}\n"
        f"{r.get('receipt_date','?')} · {r.get('amount_currency','?')} {r.get('amount','?')}\n"
        f"{merchant} — {policy}\n"
        f"_{desc}_"
    )


def _claim_buttons(r: dict[str, Any]) -> InlineKeyboardMarkup | None:
    status = r.get("status", 0)
    claim_id = r["id"]
    row = []
    if status == STATUS_DRAFT:
        row.append(InlineKeyboardButton("📤 Submit", callback_data=f"submit:{claim_id}"))
        row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{claim_id}"))
    elif status == STATUS_FOR_APPROVAL:
        row.append(InlineKeyboardButton("🗑 Withdraw", callback_data=f"delete:{claim_id}"))
    elif status == STATUS_APPROVED:
        pass  # no actions needed — it's approved
    return InlineKeyboardMarkup([row]) if row else None


def _list_filter_keyboard(active: str = "all") -> InlineKeyboardMarkup:
    """Inline filter buttons for /list — user taps to switch view."""
    buttons = [
        ("All", "list:all"),
        ("Drafts", "list:draft"),
        ("Pending", "list:submitted"),
        ("Approved", "list:approved"),
        ("Paid", "list:reimbursed"),
    ]
    row = []
    for label, data in buttons:
        prefix = "▸ " if data == f"list:{active}" else ""
        row.append(InlineKeyboardButton(f"{prefix}{label}", callback_data=data))
    return InlineKeyboardMarkup([row])


def _parse_list_args(args: list[str]) -> tuple[str, str | None, str | None]:
    """Parse /list args → (status_filter_key, date_from, date_to).

    Examples:
      /list                    → ("all", None, None)
      /list approved           → ("approved", None, None)
      /list apr                → ("all", "2026-04-01", "2026-04-30")
      /list approved apr       → ("approved", "2026-04-01", "2026-04-30")
      /list 2026-04-01 2026-04-15  → ("all", "2026-04-01", "2026-04-15")
    """
    from datetime import date as dt_date
    import calendar

    status_key = "all"
    date_from = None
    date_to = None

    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }

    for arg in args:
        low = arg.lower()
        if low in FILTER_SHORTCUTS:
            status_key = low
        elif low in months:
            m = months[low]
            y = dt_date.today().year
            _, last_day = calendar.monthrange(y, m)
            date_from = f"{y}-{m:02d}-01"
            date_to = f"{y}-{m:02d}-{last_day}"
        elif len(low) == 10 and low[4] == "-":  # YYYY-MM-DD
            if not date_from:
                date_from = low
            else:
                date_to = low

    return status_key, date_from, date_to


async def _do_list(
    update: Update,
    u: dict,
    status_key: str = "all",
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    is_callback: bool = False,
) -> None:
    """Shared list logic for both /list command and inline filter callbacks."""
    filters = FILTER_SHORTCUTS.get(status_key, ACTIVE_STATUS_FILTERS)

    async with client_for(u) as client:
        try:
            data = await client.list_submissions(status_filters=filters, page_size=20)
        except AuthError:
            msg = "Session expired — run /pair to re-link."
            if is_callback:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            return

    rows = data.get("results", [])

    # Client-side date filter
    if date_from or date_to:
        filtered = []
        for r in rows:
            rd = r.get("receipt_date", "")
            if date_from and rd < date_from:
                continue
            if date_to and rd > date_to:
                continue
            filtered.append(r)
        rows = filtered

    target = update.callback_query.message if is_callback else update.message
    filter_kb = _list_filter_keyboard(status_key)

    date_label = ""
    if date_from and date_to:
        date_label = f" ({date_from} → {date_to})"
    elif date_from:
        date_label = f" (from {date_from})"

    if not rows:
        await target.reply_text(
            f"No *{status_key}*{date_label} claims found.",
            parse_mode="Markdown",
            reply_markup=filter_kb,
        )
        return

    total = len(rows)
    await target.reply_text(
        f"_{status_key}{date_label}: {total} claim{'s' if total != 1 else ''}_",
        parse_mode="Markdown",
        reply_markup=filter_kb,
    )
    for r in rows[:10]:  # cap at 10 cards to avoid spam
        caption = _claim_summary(r)
        kb = _claim_buttons(r)
        local = storage.find_receipt_by_submission(u["id"], r["id"])
        if local and local.get("tg_file_id"):
            try:
                fid = local["tg_file_id"]
                if local.get("tg_file_type") == "photo":
                    await target.reply_photo(photo=fid, caption=caption, parse_mode="Markdown", reply_markup=kb)
                else:
                    await target.reply_document(document=fid, caption=caption, parse_mode="Markdown", reply_markup=kb)
                continue
            except Exception as e:
                log.warning("tg_file_id replay failed for %s: %s", r["id"], e)
        await target.reply_text(caption, parse_mode="Markdown", reply_markup=kb)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    if not u or not u.get("access_jwt"):
        await update.message.reply_text("Not paired yet — run /pair")
        return
    if not await _check_rate(update, u["id"], "list"):
        return

    status_key, date_from, date_to = _parse_list_args(ctx.args or [])
    await _do_list(update, u, status_key, date_from, date_to)


def _build_confirm_message(
    parsed: ParsedReceipt, policies: list[PolicyEntry]
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the confirmation message and keyboard shown after parsing a receipt."""
    lines = [
        f"📄 *{parsed.merchant or 'Unknown merchant'}*",
        f"{parsed.currency or '?'} {parsed.amount or '?'}  ·  {parsed.receipt_date or '?'}",
    ]
    low_conf = [
        k for k in ("amount", "date", "merchant")
        if parsed.confidence.get(k, 1.0) < 0.7
    ]
    if low_conf:
        lines.append(f"⚠️ Low confidence on: {', '.join(low_conf)} — please double-check")
    if parsed.suggested_sub_category_label:
        lines.append(f"Category: {parsed.suggested_sub_category_label}")

    text = "\n".join(lines)

    if not parsed.suggested_policy_id:
        if policies:
            text += "\n\nI couldn't auto-classify. Pick a policy:"
            rows: list[list[InlineKeyboardButton]] = []
            row: list[InlineKeyboardButton] = []
            for p in policies[:20]:
                row.append(InlineKeyboardButton(p.label[:32], callback_data=f"pick_policy:{p.id}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_file")])
        else:
            text += "\n\nI couldn't auto-classify and no policies are available. Try adding a hint as a caption (e.g. 'travel local')."
            rows = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_file")]]
        return text, InlineKeyboardMarkup(rows)

    policy_label = next(
        (p.label for p in policies if p.id == parsed.suggested_policy_id),
        f"policy #{parsed.suggested_policy_id}",
    )
    text += f"\nPolicy: *{policy_label}*\n\nFile this as a draft?"
    return text, InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ File it", callback_data="confirm_file"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_file"),
    ]])


async def _do_file_draft(q: Any, chat_id: int, policy_id: int) -> None:
    """Upload doc + create OmniHR draft for a confirmed pending receipt."""
    pending = _pending_files.pop(chat_id, None)
    if not pending:
        await q.edit_message_text("No pending receipt — please resend the file.")
        return

    parsed = pending.parsed
    u = pending.u

    async def _fail(text: str, **kw) -> None:
        try:
            await q.edit_message_text(text, **kw)
        except Exception:
            await q._bot.send_message(chat_id=chat_id, text=text, **kw)

    if not parsed.amount or not parsed.receipt_date:
        await _fail(
            f"Missing amount or date — couldn't parse those from the receipt.\n"
            f"Parsed: {parsed.merchant} {parsed.amount} {parsed.currency} {parsed.receipt_date}"
        )
        return

    try:
        await q.edit_message_text("On it… 💼")
    except Exception:
        pass

    try:
        async with client_for(u) as client:
            try:
                doc = await client.upload_document(
                    file_bytes=pending.file_bytes,
                    name=pending.filename,
                    media_type=pending.media_type if pending.media_type.startswith("application/") else "image/jpeg",
                )
                doc_id = doc["id"]
                doc_path = doc["file_path"]
            except Exception as e:
                await _fail(f"Upload failed: {e}")
                return

            try:
                schema = await client.schema(policy_id, parsed.receipt_date)
            except Exception as e:
                await _fail(f"Schema fetch failed: {e}")
                return

            values: dict[str, Any] = {
                "AMOUNT": {"amount": str(parsed.amount), "amount_currency": parsed.currency or "SGD"},
                "MERCHANT": parsed.merchant or "",
                "RECEIPT_DATE": parsed.receipt_date.isoformat(),
                "DESCRIPTION": parsed.description_draft or pending.user_note or "",
            }
            for lbl, val in (parsed.custom_fields or {}).items():
                values[lbl] = val
            if parsed.suggested_sub_category_label:
                for f in schema.custom_fields():
                    if f.field_type == "SINGLE_SELECT" and "sub" in f.label.lower():
                        values[f.label] = parsed.suggested_sub_category_label
                        break
            for f in schema.custom_fields():
                if f.is_mandatory and f.field_id not in {ff.field_id for ff in schema.custom_fields() if f.label in values}:
                    label_low = f.label.lower()
                    if ("trip start" in label_low or "trip end" in label_low) and f.label not in values:
                        values[f.label] = parsed.receipt_date.isoformat()
                    if "destination" in label_low and f.label not in values:
                        values[f.label] = "Singapore"

            receipts_payload = [{"id": doc_id, "file_path": doc_path}]
            try:
                draft = await client.create_draft(
                    policy_id=policy_id,
                    schema=schema,
                    values=values,
                    receipts=receipts_payload,
                )
            except SchemaDriftError as e:
                await invalidate_schema(tenant_id=client.tenant_id, policy_id=policy_id)
                labels = []
                for err in (e.field_errors or []):
                    fid = err.get("field_id")
                    f = next((ff for ff in schema.custom_fields() if ff.field_id == fid), None)
                    labels.append(f.label if f else f"#{fid}")
                await _fail(
                    f"📝 Couldn't file — this policy requires fields I couldn't guess:\n"
                    + "".join(f"• *{lbl}*\n" for lbl in labels)
                    + "\nReply with a caption like _'for client meeting, origin Tanjong Pagar, "
                    "destination Raffles Place'_ and I'll retry, or fill them in on OmniHR directly.",
                    parse_mode="Markdown",
                )
                return
            except ValidationError as e:
                await _fail(
                    f"Couldn't file — {e}\n"
                    f"Parsed: {parsed.merchant} {parsed.currency} {parsed.amount} on {parsed.receipt_date}\n"
                    f"Values attempted: {list(values.keys())}"
                )
                return
            except Exception as e:
                await _fail(f"Draft create failed: {e}")
                return

        sub_id = draft["id"]
        storage.insert_receipt(
            u["id"],
            file_sha256=pending.sha,
            parsed=parsed.raw,
            omnihr_doc_id=doc_id,
            omnihr_submission_id=sub_id,
            omnihr_file_path=doc_path,
            omnihr_file_name=pending.filename,
            omnihr_file_mime=pending.media_type,
            tg_file_id=pending.tg_file_id,
            tg_file_type=pending.tg_file_type,
            status=draft.get("status", 3),
        )
        kb = _claim_buttons({"id": sub_id, "status": STATUS_DRAFT})
        caption = (
            f"✅ Drafted *#{sub_id}*\n"
            f"{parsed.merchant} {parsed.currency} {parsed.amount} · {parsed.receipt_date}\n"
            f"{parsed.suggested_sub_category_label or '?'}"
        )
        try:
            await q.delete_message()
        except Exception:
            pass
        import io
        buf = io.BytesIO(pending.file_bytes)
        buf.name = pending.filename
        if pending.media_type.startswith("image/"):
            await q._bot.send_photo(chat_id=chat_id, photo=buf, caption=caption, parse_mode="Markdown", reply_markup=kb)
        else:
            await q._bot.send_document(chat_id=chat_id, document=buf, filename=pending.filename, caption=caption, parse_mode="Markdown", reply_markup=kb)

    except AuthError:
        await _fail("Session expired — run /pair to re-link.")
    except Exception as e:
        log.exception("_do_file_draft failed")
        await _fail(f"Filing failed: {e}")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline-keyboard taps: submit:<id>, delete:<id>, confirm_delete:<id>."""
    q = update.callback_query
    if not q or not q.data:
        return
    log.info("callback: user=%s data=%s", q.from_user.id if q.from_user else "?", q.data)
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(q.from_user.id))
    action, _, rest = q.data.partition(":")

    # --- Receipt confirmation / policy-pick callbacks (no OmniHR auth guard needed for cancel) ---
    if action == "cancel_file":
        await q.answer()
        chat_id = q.message.chat_id if q.message else q.from_user.id
        _pending_files.pop(chat_id, None)
        try:
            await q.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if action in ("confirm_file", "pick_policy"):
        await q.answer()
        chat_id = q.message.chat_id if q.message else q.from_user.id
        if action == "confirm_file":
            pending = _pending_files.get(chat_id)
            if not pending or not pending.parsed.suggested_policy_id:
                await q.edit_message_text("No pending receipt with a known policy.")
                return
            policy_id = pending.parsed.suggested_policy_id
        else:
            try:
                policy_id = int(rest)
            except ValueError:
                await q.answer("Bad policy ID", show_alert=True)
                return
        await _do_file_draft(q, chat_id, policy_id)
        return

    if not u or not u.get("access_jwt"):
        await q.answer("Not paired — run /pair", show_alert=True)
        return

    # Handle list filter callbacks: list:approved, list:draft, etc.
    if action == "list":
        await q.answer()
        status_key = rest or "all"
        await _do_list(update, u, status_key, is_callback=True)
        return

    try:
        claim_id = int(rest)
    except ValueError:
        await q.answer("Bad action", show_alert=True)
        return
    await q.answer()

    async def _reply(text: str, markup=None):
        """Reply in the chat — works regardless of whether the original message is text or media."""
        chat_id = q.message.chat_id if q.message else q.from_user.id
        await q._bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode="Markdown")

    try:
        async with client_for(u) as client:
            if action == "submit":
                await client.submit_draft(claim_id)
                await _reply(f"📤 Submitted *#{claim_id}*.")
                try:
                    await q.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            elif action == "delete":
                await client.delete_submission(claim_id)
                await _reply(f"🗑 Deleted *#{claim_id}*")
                try:
                    await q.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            else:
                await q.answer(f"Unknown action: {action}", show_alert=True)
    except AuthError:
        await _reply("Session expired — run /pair to re-link.")
    except Exception as e:
        log.exception("callback failed")
        await _reply(f"Action failed: {e}")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /delete <draft_id>")
        return
    sub_id = int(ctx.args[0])
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    async with client_for(u) as client:
        await client.delete_submission(sub_id)
    await update.message.reply_text(f"✅ Deleted #{sub_id}")


async def cmd_submit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /submit <draft_id>")
        return
    sub_id = int(ctx.args[0])
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    async with client_for(u) as client:
        try:
            await client.submit_draft(sub_id)
            await update.message.reply_text(
                f"📤 Sent submit-action for #{sub_id}. (Action code is tentative — verify on dashboard.)"
            )
        except Exception as e:
            await update.message.reply_text(f"Submit failed: {e}\nThe action code may need probing — try via web UI once and tell me.")


async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what the bot has learned about the user. They can modify it by
    talking to the bot in plain English — the agent has an update_memories tool."""
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    if not u:
        await update.message.reply_text("Run /start first.")
        return
    memories = storage.get_user_md(u["id"]) or ""
    if not memories.strip():
        await update.message.reply_text(
            "_I haven't remembered anything yet._\n\n"
            "As you correct my classifications, I'll ask if I should remember — "
            "you approve, I save it. Or just tell me: "
            "_\"remember that Grab rides after 10pm are personal.\"_",
            parse_mode="Markdown",
        )
        return

    # Telegram caps a message at 4096 chars. Reserve ~200 chars for the
    # header + footer and the code-block wrapping.
    HEADER = "*What I remember about you:*\n\n"
    FOOTER = "\n_Tell me what to change and I'll update it._"
    wrapped = f"```\n{memories}\n```"

    if len(HEADER) + len(wrapped) + len(FOOTER) <= 4000:
        await update.message.reply_text(
            HEADER + wrapped + FOOTER,
            parse_mode="Markdown",
        )
        return

    # Too long for a single message — send as an attached file so the user
    # sees every byte, then a short explainer.
    import io
    buf = io.BytesIO(memories.encode("utf-8"))
    buf.name = "memories.md"
    await update.message.reply_document(
        document=buf,
        filename="memories.md",
        caption="Full memory attached. Tell me what to change and I'll update it.",
    )


async def _build_tool_executor(u: dict, file_bytes: bytes | None = None, media_type: str = "", filename: str = ""):
    """Build a tool executor closure for the agent, bound to this user + file."""

    async def execute(tool_name: str, tool_input: dict) -> str:
        if tool_name == "parse_receipt":
            if not file_bytes:
                return "No receipt file attached. Ask the user to send a photo or PDF."
            tenant_md = load_tenant_md(u.get("tenant_id"))
            user_md = load_user_md(u)
            try:
                parsed = await parse_receipt(
                    anthropic=await anthropic_for(u),
                    file_bytes=file_bytes,
                    media_type=media_type,
                    tenant_md=tenant_md,
                    user_md=user_md,
                    recent_claims_summary="",
                    active_trip=None,
                )
                return json.dumps(parsed.raw, default=str)
            except Exception as e:
                return f"Parse failed: {e}"

        elif tool_name == "list_claims":
            status_key = tool_input.get("status", "all")
            filters = FILTER_SHORTCUTS.get(status_key, ACTIVE_STATUS_FILTERS)
            async with client_for(u) as client:
                data = await client.list_submissions(status_filters=filters, page_size=15)
            rows = data.get("results", [])
            if not rows:
                return f"No {status_key} claims found."
            lines = []
            for r in rows:
                sl = STATUS_LABELS.get(r.get("status", 0), "?")
                lines.append(
                    f"#{r['id']} {r.get('receipt_date','?')} "
                    f"{r.get('amount_currency','?')} {r.get('amount','?')} "
                    f"{r.get('merchant') or '?'} [{sl}] "
                    f"{(r.get('description') or '')[:50]}"
                )
            return "\n".join(lines)

        elif tool_name == "submit_claim":
            cid = tool_input["claim_id"]
            async with client_for(u) as client:
                await client.submit_draft(cid)
            return f"Submitted #{cid} for approval."

        elif tool_name == "delete_claim":
            cid = tool_input["claim_id"]
            async with client_for(u) as client:
                await client.delete_submission(cid)
            return f"Deleted #{cid}."

        elif tool_name == "update_memories":
            new_md = tool_input.get("new_markdown", "")
            change = tool_input.get("change_summary", "")
            if not new_md.strip():
                return "Refused: new_markdown was empty."
            # Sanity check — the five section headers must survive the round-trip
            required = [
                "## Classification rules",
                "## Merchant shortcuts",
                "## Defaults",
                "## Description style",
                "## Don't ask me about",
            ]
            missing = [h for h in required if h not in new_md]
            if missing:
                return (
                    f"Refused: markdown is missing required section header(s): "
                    f"{missing}. Preserve all five headers from the template."
                )
            storage.set_user_md(u["id"], new_md)
            log.info("memory updated for user=%s: %s", u["id"], change)
            return f"Saved. Summary: {change}"

        elif tool_name == "update_profile":
            new_profile = tool_input.get("new_profile_md", "")
            change = tool_input.get("change_summary", "")
            if len(new_profile) > 2000:
                return "Refused: profile too long (>2000 chars). Trim before saving."
            storage.set_profile_md(u["id"], new_profile)
            log.info("profile updated for user=%s: %s", u["id"], change)
            return f"Saved. Summary: {change}"

        elif tool_name == "get_claim_summary":
            async with client_for(u) as client:
                data = await client.list_submissions(page_size=30)
            rows = data.get("results", [])
            if not rows:
                return "No claims found."
            # Build a summary for Claude to interpret
            lines = []
            for r in rows:
                sl = STATUS_LABELS.get(r.get("status", 0), "?")
                lines.append(
                    f"#{r['id']} date={r.get('receipt_date','?')} "
                    f"amt={r.get('amount_currency','?')} {r.get('amount','?')} "
                    f"merchant={r.get('merchant') or '?'} "
                    f"policy={(r.get('policy') or {}).get('name','?')} "
                    f"status={sl}"
                )
            return f"Claims ({len(rows)} total):\n" + "\n".join(lines)

        return f"Unknown tool: {tool_name}"

    return execute


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """All non-command text → agent with tools."""
    if not await _gate(update):
        return
    msg = update.message
    u = storage.get_user_by_channel("telegram", str(msg.from_user.id))
    if not u:
        # First-ever message — kick straight into the guided flow.
        user_db_id = storage.upsert_user("telegram", str(msg.from_user.id))
        u = storage.get_user(user_db_id)
        await msg.reply_text(
            _next_step_prompt(user_db_id, u, msg.from_user.first_name),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    storage.bump_last_inbound_at(u["id"])

    text = (msg.text or "").strip()
    if not text:
        return

    storage.log_message(u["id"], "in", text)

    # Detect OAuth token pasted in Telegram (code#state or ?code=XXX format)
    import re
    oauth_code = None
    oauth_state = None
    url_match = re.search(r'[?&]code=([A-Za-z0-9_\-]+).*[?&]state=([A-Za-z0-9_\-]+)', text)
    hash_match = re.match(r'^([A-Za-z0-9_\-]{20,})#([A-Za-z0-9_\-]{20,})$', text.strip())
    if url_match:
        oauth_code, oauth_state = url_match.group(1), url_match.group(2)
    elif hash_match:
        oauth_code, oauth_state = hash_match.group(1), hash_match.group(2)

    if oauth_code and oauth_state and oauth_state in claude_oauth._pending:
        progress = await msg.reply_text("Just a moment, darling…")
        ok, err_msg, token_data = await claude_oauth.exchange_code(oauth_state, oauth_code)
        if ok and token_data:
            exp = None
            if token_data.get("expires_in"):
                exp = datetime.now(timezone.utc) + timedelta(seconds=int(token_data["expires_in"]))
            storage.set_anth_oauth(
                token_data["user_db_id"],
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                expires_at=exp,
            )
            u = storage.get_user(token_data["user_db_id"])
            await progress.edit_text(
                "✅ *Step 1 of 3 done — Claude subscription linked.*\n\n"
                + _next_step_prompt(token_data["user_db_id"], u),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            await progress.edit_text(f"Login failed: {err_msg}")
        return

    try:
        anth = await anthropic_for(u)
    except RuntimeError:
        await msg.reply_text(
            "I'd love to help! First, connect your AI:\n\n"
            "👉 /login — takes 30 seconds",
            parse_mode="Markdown",
        )
        return

    if not await _check_rate(update, u["id"], "parse"):
        return

    progress = await msg.reply_text("Give me a second, handsome… 😏")
    tenant_md = load_tenant_md(u.get("tenant_id"))
    user_md = load_user_md(u)
    executor = await _build_tool_executor(u)
    history = storage.get_recent_messages(u["id"], limit=12)

    try:
        reply = await run_agent(
            anthropic=anth,
            user_message=text,
            has_file=False,
            tenant_md=tenant_md,
            user_md=user_md,
            profile_md=storage.get_profile_md(u["id"]),
            recent_claims="(agent will fetch via tools if needed)",
            tool_executor=executor,
            conversation_history=history,
        )
    except Exception as e:
        log.warning("agent failed: %s", e)
        reply = f"Something went wrong: {e}"

    storage.log_message(u["id"], "out", reply)
    try:
        await progress.edit_text(reply, parse_mode="Markdown")
    except Exception:
        await progress.edit_text(reply)  # fallback without markdown


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo or document → agent with parse_receipt tool."""
    if not await _gate(update):
        return
    msg = update.message
    u = storage.get_user_by_channel("telegram", str(msg.from_user.id))
    if not u:
        # First-ever message — kick straight into the guided flow.
        user_db_id = storage.upsert_user("telegram", str(msg.from_user.id))
        u = storage.get_user(user_db_id)
        await msg.reply_text(
            _next_step_prompt(user_db_id, u, msg.from_user.first_name),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    storage.bump_last_inbound_at(u["id"])
    file_type = "photo" if update.message.photo else "document"
    storage.log_message(u["id"], "in", msg.caption or None, has_file=True, file_type=file_type)

    try:
        anth = await anthropic_for(u)
    except RuntimeError:
        await msg.reply_text(
            "I can't read receipts until we connect an AI.\n\n" + _next_step_prompt(u["id"], u),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return
    if not u.get("access_jwt"):
        await msg.reply_text(
            "AI is connected but I can't file claims until you link OmniHR.\n\n"
            + _next_step_prompt(u["id"], u),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    # Download file
    if msg.document:
        tg_file = await msg.document.get_file()
        media_type = msg.document.mime_type or "application/pdf"
        filename = msg.document.file_name or "receipt.pdf"
        tg_file_id = msg.document.file_id
        tg_file_type = "document"
    elif msg.photo:
        tg_file = await msg.photo[-1].get_file()
        media_type = "image/jpeg"  # default; corrected by magic-byte sniff below
        filename = "receipt.jpg"
        tg_file_id = msg.photo[-1].file_id
        tg_file_type = "photo"
    else:
        return

    file_bytes = bytes(await tg_file.download_as_bytearray())

    # Sniff actual format — Telegram's Bot API claims photos are JPEG, but
    # messages arriving via a Matrix bridge (Beeper, etc.) can deliver PNGs
    # through the same `msg.photo` path. Anthropic rejects the call with a
    # 400 when the declared media_type doesn't match the bytes.
    if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        media_type = "image/png"
        if filename.endswith(".jpg") or filename.endswith(".jpeg"):
            filename = "receipt.png"
    elif file_bytes[:3] == b"\xff\xd8\xff":
        media_type = "image/jpeg"
    elif file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        media_type = "image/webp"
        if filename.endswith((".jpg", ".jpeg", ".png")):
            filename = "receipt.webp"
    elif file_bytes[:4] == b"%PDF":
        media_type = "application/pdf"
        if not filename.lower().endswith(".pdf"):
            filename = "receipt.pdf"
    user_note = (msg.caption or "").strip()

    if not await _check_rate(update, u["id"], "parse"):
        return
    progress = await msg.reply_text("Leave it with me… 👀")

    import hashlib
    sha = hashlib.sha256(file_bytes).hexdigest()
    cached = storage.find_receipt_by_sha(u["id"], sha)
    if cached and cached.get("omnihr_submission_id"):
        await progress.edit_text(
            f"⚠️ Same file as #{cached['omnihr_submission_id']} (filed {cached['created_at']}).\n"
            f"Reply DELETE {cached['omnihr_submission_id']} or send a different receipt."
        )
        return

    # Build context
    tenant_md = load_tenant_md(u.get("tenant_id"))
    user_md = load_user_md(u)

    # Quick recent-claims summary from OmniHR (parallel-ish — keep simple for now)
    async with client_for(u) as client:
        try:
            recent = await client.list_submissions(page_size=10)
            recent_summary = "\n".join(
                f"- {r.get('receipt_date','?')} {r.get('amount_currency','?')} {r.get('amount','?')} "
                f"({r.get('policy', {}).get('name','?')})"
                for r in recent.get("results", [])[:10]
            ) or "(no recent claims)"
        except Exception:
            recent_summary = "(couldn't fetch recent claims)"

        try:
            policies: list[PolicyEntry] = await get_policies(client, u.get("tenant_id") or "")
        except Exception as _pe:
            log.warning("policies fetch failed: %s", _pe)
            policies = []

        # Parse — try Agent SDK first (subscription auth), fall back to direct API
        parsed = None
        agent_raw = None
        active_trip = f"User hint: {user_note}" if user_note else None
        try:
            agent_raw = await parse_receipt_via_agent(
                file_bytes=file_bytes,
                media_type=media_type,
                filename=filename,
                tenant_md=tenant_md,
                user_md=user_md,
                recent_claims_summary=recent_summary,
                active_trip=active_trip,
            )
        except Exception as e:
            log.info("Agent SDK parse skipped: %s", e)

        if agent_raw and agent_raw.get("is_receipt"):
            # Convert agent_raw dict to ParsedReceipt
            from .common.parser import _to_parsed_receipt
            parsed = _to_parsed_receipt(agent_raw)
        else:
            # Fallback to direct Anthropic API
            try:
                parsed = await parse_receipt(
                    anthropic=await anthropic_for(u),
                    file_bytes=file_bytes,
                    media_type=media_type,
                    tenant_md=tenant_md,
                    user_md=user_md,
                    recent_claims_summary=recent_summary,
                    active_trip=active_trip,
                    policies=policies,
                )
            except RuntimeError as e:
                if "No Anthropic key" in str(e):
                    await progress.edit_text(
                        "No API key and Agent SDK unavailable.\n"
                        "Either: /login (use Claude subscription) or /setkey sk-ant-…"
                    )
                    return
                raise
            except Exception as e:
                await progress.edit_text(f"Parse failed: {e}")
                return

        if not parsed.is_receipt:
            await progress.edit_text("That doesn't look like a receipt — anything else?")
            return

        # Store pending state and show confirmation / policy picker
        chat_id = msg.chat_id
        _pending_files[chat_id] = _PendingFile(
            tg_user_id=str(msg.from_user.id),
            u=u,
            file_bytes=file_bytes,
            media_type=media_type,
            filename=filename,
            sha=sha,
            tg_file_id=tg_file_id,
            tg_file_type=tg_file_type,
            parsed=parsed,
            policies=policies,
            user_note=user_note,
        )
        confirm_text, confirm_kb = _build_confirm_message(parsed, policies)
        storage.log_message(u["id"], "out", confirm_text)
        await progress.edit_text(confirm_text, parse_mode="Markdown", reply_markup=confirm_kb)


# ---------------------------------------------------------------------------
# Backend HTTP (extension pairing + health)
# ---------------------------------------------------------------------------

class PairPayload(BaseModel):
    pairing_code: str
    access_token: str
    refresh_token: str
    employee_id: int
    org: dict[str, Any] | None = None


def make_app(tg_app: Application | None = None) -> FastAPI:
    app = FastAPI(title="expensebot")

    # Allow the extension (any localhost or our public host) to POST
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from fastapi.responses import HTMLResponse, Response

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return pages.landing_page()

    @app.get("/auth/start")
    async def auth_start(s: str = "", oauth: str = "") -> HTMLResponse:
        """Bridge page: OAuth sign-in OR API key — both options on one page."""
        if not s or not oauth:
            return HTMLResponse("<h1>Invalid link</h1><p>Try /login again.</p>", status_code=400)
        page = f"""<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Janai — Sign in</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:-apple-system,sans-serif;background:#1a1a2e;color:#eee;
       min-height:100vh;display:flex;align-items:center;justify-content:center}}
  .c{{background:#16213e;border-radius:16px;padding:28px;max-width:420px;width:92%}}
  h1{{font-size:20px;margin-bottom:4px;text-align:center}}
  .sub{{font-size:13px;color:#888;text-align:center;margin-bottom:20px}}
  p{{font-size:14px;color:#aaa;margin:10px 0;line-height:1.5}}
  a.btn{{display:block;background:#6633ee;color:#fff;padding:14px;border-radius:8px;
        text-decoration:none;font-size:16px;font-weight:600;margin:12px 0;text-align:center}}
  .or{{text-align:center;color:#555;margin:16px 0;font-size:13px;position:relative}}
  .or::before,.or::after{{content:'';position:absolute;top:50%;width:35%;height:1px;background:#333}}
  .or::before{{left:0}} .or::after{{right:0}}
  input{{width:100%;padding:12px;border-radius:8px;border:1px solid #333;
        background:#0f3460;color:#eee;font-size:13px;margin:6px 0}}
  button{{width:100%;padding:14px;border-radius:8px;border:0;background:#6633ee;
         color:#fff;font-size:16px;font-weight:600;cursor:pointer;margin:8px 0}}
  button:disabled{{background:#444;cursor:default}}
  .btn2{{background:#2d4a7a}}
  .ok{{background:#1a4d2e;padding:20px;border-radius:12px;margin:16px 0;text-align:center}}
  .err{{background:#4d1a1a;padding:12px;border-radius:8px;margin:10px 0;font-size:13px}}
  .hide{{display:none}}
  .small{{font-size:11px;color:#555;text-align:center;margin-top:4px}}
</style></head><body>
<div class="c">
  <h1>💰 Janai</h1>
  <div class="sub">Connect your AI to parse receipts</div>

  <!-- Single combined view: open Claude in new tab, come back, paste here -->
  <div id="s1">
    <p style="color:#ccc;font-size:13px;margin-bottom:4px"><strong style="color:#fff">Step 1 —</strong> open Claude and authorize (in a new tab)</p>
    <a class="btn" href="{oauth}" id="authBtn" target="_blank" rel="noopener">Open Claude Login →</a>
    <p class="small">Uses your existing Claude Pro/Max plan. No extra billing.</p>

    <p style="color:#ccc;font-size:13px;margin:18px 0 4px"><strong style="color:#fff">Step 2 —</strong> come back here and paste the code</p>
    <input id="url" placeholder="Paste the authentication code here…" autocomplete="off">
    <button onclick="submitCode()">Complete Login</button>
    <div id="st"></div>

    <div class="or">or use an API key instead</div>

    <input id="apikey" placeholder="sk-ant-api03-..." autocomplete="off">
    <button class="btn2" onclick="submitKey()">Use API Key</button>
    <p class="small">Get one at <a href="https://console.anthropic.com/settings/keys" style="color:#6699cc">console.anthropic.com</a>.</p>
  </div>

  <!-- DONE (rendered dynamically from server response) -->
  <div id="s3" class="hide">
    <div class="ok">
      <h2 id="s3-title">✅ Connected!</h2>
      <div id="s3-body" style="margin-top:10px"></div>
    </div>
  </div>
</div>
<script>
const S='{s}';
function showSuccess(next){{
  document.getElementById('s1').classList.add('hide');
  document.getElementById('s3').classList.remove('hide');
  if(next){{
    if(next.title) document.getElementById('s3-title').textContent = next.title;
    if(next.body_html) document.getElementById('s3-body').innerHTML = next.body_html;
  }}
}}
function clearErrors(){{
  const st=document.getElementById('st'); if(st) st.innerHTML='';
}}
// When user comes back from the Claude tab, focus the paste field.
document.getElementById('authBtn').addEventListener('click',function(){{
  setTimeout(()=>{{ try{{ document.getElementById('url').focus(); }}catch(e){{}} }},400);
}});
window.addEventListener('focus',function(){{
  const u=document.getElementById('url');
  if(u && !u.value) u.focus();
}});
async function post(url,body){{
  const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  return [r, await r.json()];
}}
async function submitCode(){{
  clearErrors();
  const input=document.getElementById('url').value.trim();
  const st=document.getElementById('st');
  let code=null;
  const urlMatch=input.match(/[?&]code=([A-Za-z0-9_\\-]+)/);
  const hashMatch=input.match(/^([A-Za-z0-9_\\-]+)#/);
  if(urlMatch) code=urlMatch[1];
  else if(hashMatch) code=hashMatch[1];
  else if(input.length>20&&/^[A-Za-z0-9_\\-]+$/.test(input)) code=input;
  if(!code){{st.innerHTML='<div class="err">Couldn\\'t find the code. Tap "Copy Code" on the auth page and paste here.</div>';return;}}
  try{{
    const[r,d]=await post('/auth/complete',{{session:S,code:code}});
    if(r.ok&&d.ok){{ showSuccess(d.next); }}
    else if(r.status===404){{
      st.innerHTML='<div class="err">Login session expired (took too long or server restarted). '
        +'Head back to Telegram and send <code>/login</code> again — this only takes a few seconds.</div>';
    }}
    else st.innerHTML='<div class="err">'+(d.detail||'Failed')+'</div>';
  }}catch(e){{st.innerHTML='<div class="err">'+e.message+'</div>';}}
}}
async function submitKey(){{
  clearErrors();
  const key=document.getElementById('apikey').value.trim();
  if(!key.startsWith('sk-ant-')){{alert('Key should start with sk-ant-');return;}}
  try{{
    const[r,d]=await post('/auth/setkey',{{session:S,key:key}});
    if(r.ok&&d.ok){{ showSuccess(d.next); }}
    else alert(d.detail||'Failed');
  }}catch(e){{alert(e.message);}}
}}
</script></body></html>"""
        return HTMLResponse(page)

    @app.post("/auth/complete")
    async def auth_complete(payload: dict[str, Any]) -> dict:
        """Exchange OAuth code for tokens (pure PKCE, no subprocess)."""
        state = payload.get("session", "")
        code = payload.get("code", "")
        if not state or not code:
            raise HTTPException(400, "Missing session or code")

        ok, msg, token_data = await claude_oauth.exchange_code(state, code)
        if not ok or not token_data:
            raise HTTPException(400, msg)

        # Persist access + refresh + expiry so we can auto-refresh when the
        # ~8h-TTL access token expires.
        exp = None
        if token_data.get("expires_in"):
            exp = datetime.now(timezone.utc) + timedelta(seconds=int(token_data["expires_in"]))
        storage.set_anth_oauth(
            token_data["user_db_id"],
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            expires_at=exp,
        )

        # DM the user and nudge them into step 2
        if tg_app:
            try:
                u = storage.get_user(token_data["user_db_id"])
                await tg_app.bot.send_message(
                    chat_id=token_data["telegram_user_id"],
                    text=(
                        "✅ *Step 1 of 3 done — Claude subscription linked.*\n"
                        "Receipts will be parsed using your Claude plan — no API key needed.\n\n"
                        + _next_step_prompt(token_data["user_db_id"], u)
                    ),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning("couldn't DM after login: %s", e)

        u = storage.get_user(token_data["user_db_id"])
        return {"ok": True, "next": _web_next_step_html(token_data["user_db_id"], u)}

    @app.post("/auth/setkey")
    async def auth_setkey(payload: dict[str, Any]) -> dict:
        """Set API key via the web page (alternative to OAuth)."""
        state = payload.get("session", "")
        key = payload.get("key", "").strip()
        if not state or not key:
            raise HTTPException(400, "Missing session or key")
        pending = claude_oauth._pending.pop(state, None)
        if not pending:
            raise HTTPException(404, "Session expired. Run /login again.")
        if not _plausible_anth_key(key):
            raise HTTPException(400, "Invalid key format. Should start with sk-ant-")

        storage.set_anth_key(pending.user_db_id, key)
        u = storage.get_user(pending.user_db_id)
        if tg_app:
            try:
                await tg_app.bot.send_message(
                    chat_id=pending.telegram_user_id,
                    text=(
                        "✅ *Step 1 of 3 done — API key saved.*\n\n"
                        + _next_step_prompt(pending.user_db_id, u)
                    ),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        return {"ok": True, "next": _web_next_step_html(pending.user_db_id, u)}

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    async def favicon() -> Response:
        # Just the 💰 emoji. Modern browsers render emoji in SVG favicons
        # using the OS's native color-emoji font (Apple on Mac/iOS, Segoe UI
        # Emoji on Windows, Noto on Linux) — stays visually consistent with
        # the 💰 used in page headers + bot messages.
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<text y=".9em" font-size="90">\U0001F4B0</text>'
            "</svg>"
        )
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/extension")
    async def extension_page() -> HTMLResponse:
        return HTMLResponse(pages.extension_page())

    from fastapi.responses import FileResponse

    @app.get("/extension/download")
    async def extension_download() -> FileResponse:
        # Inner folder gets a distinct, self-explanatory name so a
        # non-technical user doesn't wonder which of two "extension" folders
        # to pick in Chrome's "Load unpacked" dialog.
        import zipfile, tempfile
        zip_path = Path(tempfile.gettempdir()) / "Janai-Chrome-Extension.zip"
        ext_dir = REPO_ROOT / "extension"
        inner = "Janai-Chrome-Extension"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in ext_dir.rglob("*"):
                if f.is_file() and not f.name.startswith("."):
                    zf.write(f, f"{inner}/{f.relative_to(ext_dir)}")
        return FileResponse(
            zip_path,
            filename="Janai-Chrome-Extension.zip",
            media_type="application/zip",
        )

    @app.get("/terms", response_class=HTMLResponse)
    async def terms() -> str:
        return pages.terms_page()

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> str:
        return pages.privacy_page()

    @app.post("/extension/pair")
    async def extension_pair(p: PairPayload) -> dict:
        user_db_id = storage.consume_pairing_code(p.pairing_code)
        if not user_db_id:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")

        # Parse JWTs to get expiry
        try:
            access_exp = parse_jwt_exp(p.access_token)
            refresh_exp = parse_jwt_exp(p.refresh_token)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Bad JWT: {e}")

        # Fetch user details from OmniHR to confirm
        async with httpx.AsyncClient(base_url="https://api.omnihr.co/api/v1") as http:
            r = await http.get(
                "/auth/details/",
                cookies={"access_token": p.access_token, "refresh_token": p.refresh_token},
            )
            if r.status_code != 200:
                raise HTTPException(status_code=400, detail=f"OmniHR /auth/details/ → {r.status_code}")
            me = r.json()

        # Derive tenant id from org (subdomain best, but use org name as fallback)
        org_name = (p.org or {}).get("name") or me.get("org", {}).get("name") or ""
        tenant_id = org_name.lower().split()[0] if org_name else "unknown"

        # Enforce email-domain allowlist
        user_email_raw = me.get("primary_email")
        user_email = access._extract_email(user_email_raw)
        ok, reason = access.email_allowed(user_email_raw)
        if not ok:
            log.info("pair rejected for email=%s reason=%s", user_email, reason)
            raise HTTPException(status_code=403, detail=reason)

        storage.set_omnihr_session(
            user_db_id,
            access_jwt=p.access_token,
            refresh_jwt=p.refresh_token,
            access_expires_at=access_exp,
            refresh_expires_at=refresh_exp,
            employee_id=me.get("id") or p.employee_id,
            full_name=me.get("full_name"),
            email=user_email,
            tenant_id=tenant_id,
        )

        # DM the user via Telegram — this is the payoff message after all 3 steps
        if tg_app:
            user = storage.get_user(user_db_id)
            try:
                await tg_app.bot.send_message(
                    chat_id=int(user["channel_user_id"]),
                    text=(
                        f"✅ *Step 3 of 3 done — paired as {me.get('full_name','?')} "
                        f"({tenant_id}, employee #{me.get('id')}).*\n\n"
                        + ready_prompt(me.get("full_name"))
                    ),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning("Couldn't DM paired user: %s", e)

        return {"ok": True, "user_id": user_db_id}

    return app


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

async def run() -> None:
    storage.init_db()
    if not TG_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN missing — set in .env or env")
        raise SystemExit(1)

    tg_app = Application.builder().token(TG_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("login", cmd_login))
    tg_app.add_handler(CommandHandler("whoami", cmd_whoami))
    tg_app.add_handler(CommandHandler("setkey", cmd_setkey))
    tg_app.add_handler(CommandHandler("pair", cmd_pair))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("delete", cmd_delete))
    tg_app.add_handler(CommandHandler("submit", cmd_submit))
    tg_app.add_handler(CommandHandler("memories", cmd_memories))
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_file))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def on_error(update, context):
        log.exception("unhandled error in handler", exc_info=context.error)

    tg_app.add_error_handler(on_error)

    app = make_app(tg_app)

    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
    server = uvicorn.Server(config)

    # Run uvicorn + telegram polling + refresh sweeper in parallel
    await tg_app.initialize()
    try:
        me = await tg_app.bot.get_me()
        pages.BOT_USERNAME = me.username
        log.info("telegram bot identified as @%s", me.username)
    except Exception:
        log.exception("could not resolve telegram bot username; pages will show fallback copy")

    # Set the command menu so users see /list, /memories, etc. when typing /
    try:
        from telegram import BotCommand
        await tg_app.bot.set_my_commands([
            BotCommand("start", "Welcome & setup"),
            BotCommand("login", "Connect your Claude AI"),
            BotCommand("pair", "Link your OmniHR account"),
            BotCommand("list", "Show your recent claims"),
            BotCommand("memories", "What I remember about you"),
        ])
    except Exception:
        log.exception("could not set telegram command menu")

    await tg_app.start()
    polling_task = asyncio.create_task(
        tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    )
    from ops.refresh_sweeper import run_forever as refresh_sweeper_forever
    from ops.nudge_sweeper import run_forever as nudge_sweeper_forever

    async def notify_session_expired(user: dict, message: str) -> None:
        channel = user.get("channel")
        chan_uid = user.get("channel_user_id")
        if channel == "telegram" and chan_uid:
            await tg_app.bot.send_message(
                chat_id=int(chan_uid),
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            log.info("session-expired notify skipped: channel=%s (no adapter)", channel)

    async def notify_nudge(user: dict, message: str) -> None:
        channel = user.get("channel")
        chan_uid = user.get("channel_user_id")
        if channel == "telegram" and chan_uid:
            await tg_app.bot.send_message(
                chat_id=int(chan_uid),
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            log.info("nudge skipped: channel=%s (no adapter)", channel)

    sweeper_task = asyncio.create_task(
        refresh_sweeper_forever(notifier=notify_session_expired)
    )
    nudge_task = asyncio.create_task(
        nudge_sweeper_forever(notifier=notify_nudge)
    )

    try:
        await server.serve()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        polling_task.cancel()
        sweeper_task.cancel()
        nudge_task.cancel()


def main() -> None:
    # load .env if present
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    asyncio.run(run())


if __name__ == "__main__":
    main()
