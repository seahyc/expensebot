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
    /start     /setkey <key>      /connect_omnihr      /list      /status <id>
    /submit <id>     /trip <name>      /delete <id>
    photo / pdf — files a draft

Single-host MVP. SQLite. No Redis. No Postgres. Refactor when we scale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, Request
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

from . import access, claude_oauth, learning, logging_setup, pages, rate_limit, storage
from .common.agent import run_agent, render_merchants_block
from .common.boss_profile import refresh_boss_profile
from .plugins.registry import load_enabled_skills, load_enabled_tools
from .common.agent_parser import parse_receipt_via_agent
from .common import context_lookup
from .common.context_lookup import triangulate
from .common.parser import ParsedReceipt, parse_receipt
from .common.pipeline import format_dupe_warning, match_dupes
from .voice import default_voice, memory_template, voice_for_user

logging_setup.setup()
log = logging.getLogger("expensebot")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

REPO_ROOT = Path(__file__).parent.parent
TENANTS_DIR = REPO_ROOT / "tenants"

# Tenant ids become filenames on disk. Restrict to a simple slug charset to
# prevent path traversal (../../etc/foo) and any filesystem-special chars.
_SAFE_TENANT_RE = re.compile(r"[^a-z0-9_-]+")
MAX_RECEIPT_BYTES = int(os.environ.get("MAX_RECEIPT_BYTES", 10 * 1024 * 1024))  # 10 MB default


def sanitize_tenant_id(raw: str | None) -> str:
    """Reduce a free-form org label to a safe on-disk tenant slug."""
    if not raw:
        return "unknown"
    slug = _SAFE_TENANT_RE.sub("", raw.lower())
    return slug or "unknown"


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
    triangulation_md: str | None = None


def _infer_receipt_type(parsed: ParsedReceipt) -> str:
    """Infer receipt type from parsed fields for triangulation window selection."""
    merchant = (parsed.merchant or "").lower()
    sub_cat = (parsed.suggested_sub_category_label or "").lower()
    combined = merchant + " " + sub_cat

    transport_keywords = {"grab", "gojek", "grab express", "comfort", "transit", "mrt", "bus", "taxi", "lyft", "uber"}
    flight_keywords = {"flight", "airfare", "airline", "airasia", "scoot", "singapore air", "jetstar"}
    meal_keywords = {"meal", "food", "restaurant", "cafe", "coffee", "starbucks", "lunch", "dinner", "breakfast", "hawker"}
    hotel_keywords = {"hotel", "accommodation", "inn", "resort", "marriott", "hilton", "ibis"}

    for kw in transport_keywords:
        if kw in combined:
            return "transport"
    for kw in flight_keywords:
        if kw in combined:
            return "flight"
    for kw in meal_keywords:
        if kw in combined:
            return "meal"
    for kw in hotel_keywords:
        if kw in combined:
            return "hotel"
    return "other"


# keyed by Telegram chat_id (int)
_pending_files: dict[int, _PendingFile] = {}


# ---------------------------------------------------------------------------
# Tenant / user prompts
# ---------------------------------------------------------------------------

def load_tenant_md(tenant_id: str | None) -> str:
    if not tenant_id:
        return ""
    # Defense-in-depth: even if a malformed tenant_id got into the DB (old
    # row, migration, etc.), never let it escape TENANTS_DIR.
    safe = sanitize_tenant_id(tenant_id)
    if safe == "unknown":
        return ""
    path = TENANTS_DIR / f"{safe}.md"
    try:
        resolved = path.resolve()
        resolved.relative_to(TENANTS_DIR.resolve())
    except (ValueError, OSError):
        return ""
    if resolved.exists():
        return resolved.read_text()
    return ""


def load_user_md(_user: dict) -> str:
    """Return the user's memory — falls back to the scaffold template so the
    agent always sees the five section headers and can slot new entries in."""
    stored = (_user.get("user_md") or "").strip()
    return stored if stored else memory_template(_user)


# ---------------------------------------------------------------------------
# OmniHRClient construction
# ---------------------------------------------------------------------------

def client_for(user: dict) -> OmniHRClient:
    access, refresh = storage.get_omnihr_tokens(user["id"])
    if not access or not refresh:
        raise AuthError("Not paired — run /connect_omnihr first")
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

SKILLS_DIR = REPO_ROOT / "bot" / "skills"

# Load plugin skills and tools at startup — all disabled by default.
# To enable a plugin, set "enabled": True in bot/plugins/registry.py PLUGINS.
_PLUGIN_SKILLS = load_enabled_skills()
_PLUGIN_TOOLS = load_enabled_tools()


def load_skill(hrms: str = "omnihr") -> str:
    """Load the HRMS-specific skill file. Falls back to empty if not found."""
    path = SKILLS_DIR / f"{hrms}.md"
    return path.read_text() if path.exists() else ""


def _first_name(full_or_first: str | None, user: dict | None = None) -> str:
    """Pick a first-name to address the user with. Falls back to a neutral noun
    so copy always reads naturally even when we have no name."""
    voice = voice_for_user(user)
    if not full_or_first:
        return voice.text("anonymous_name")
    return full_or_first.strip().split()[0] or voice.text("anonymous_name")


def step1_prompt(first_name: str | None = None, user: dict | None = None) -> str:
    voice = voice_for_user(user)
    return voice.text(
        "step1_prompt",
        name=_first_name(first_name, user),
        brand_name=voice.text("brand_name"),
    )


# Kept for backward-compat with any other call site; prefer step1_prompt().
STEP1_PROMPT = step1_prompt(None)

def step2_prompt(user: dict | None = None) -> str:
    return voice_for_user(user).text("step2_prompt")

def step3_prompt(user: dict | None = None) -> str:
    voice = voice_for_user(user)
    return voice.text("step3_prompt", brand_name=voice.text("brand_name"))

def ready_prompt(first_name: str | None = None, user: dict | None = None) -> str:
    return voice_for_user(user).text("ready_prompt", name=_first_name(first_name, user))


READY_PROMPT = ready_prompt(None)


def _web_next_step_html(user_db_id: int, u: dict | None) -> dict:
    """Return {'title': str, 'body_html': str} for the post-auth success panel.
    Mirrors _next_step_prompt but emits HTML so we can render on the web page
    without asking the user to bounce to Telegram to learn what to do next."""
    has_ai = bool(storage.get_anth_key(user_db_id))
    has_omnihr = bool(u and u.get("access_jwt"))
    tg_link = f"https://t.me/{pages.BOT_USERNAME}" if pages.BOT_USERNAME else "https://t.me/"
    voice = voice_for_user(u)

    if has_ai and has_omnihr:
        return {
            "title": voice.text("web_all_set_title"),
            "body_html": voice.text("web_all_set_body", tg_link=tg_link),
        }
    if has_ai and not has_omnihr:
        return {
            "title": voice.text("web_ai_connected_title"),
            "body_html": voice.text("web_ai_connected_body", tg_link=tg_link),
        }
    # AI not yet connected — unusual on this page, but handle gracefully
    return {
        "title": voice.text("web_progress_saved_title"),
        "body_html": voice.text("web_progress_saved_body", tg_link=tg_link),
    }


def _next_step_prompt(user_db_id: int, u: dict | None, first_name: str | None = None) -> str:
    """Return the single next-step message a user should see, based on what
    they've completed. Opinionated: one step at a time, no menu."""
    has_ai = bool(storage.get_anth_key(user_db_id))
    has_omnihr = bool(u and u.get("access_jwt"))
    if not has_ai:
        return step1_prompt(first_name, u)
    if not has_omnihr:
        # Step 2 (install extension) and step 3 (pair) are shown together here
        # because step 2 has no signal we can detect — the user just reads and
        # installs. After they install, they'll already see step 3 beneath it.
        return step2_prompt(u) + "\n\n" + step3_prompt(u)
    return ready_prompt(first_name, u)


def _setup_status_text(u: dict) -> str:
    """Build connection status message for fully-paired users."""
    uid = u["id"]
    google_ok = bool(storage.get_google_tokens(uid)[0])
    tg_ok = bool(storage.get_telegram_session(uid))
    wa_ok = storage.get_whatsapp_connected(uid)
    ext_url = f"{PUBLIC_BASE_URL}/extension"

    lines = ["*Janai — connection status*\n"]
    lines.append("✅ *OmniHR* — expense filing")
    lines.append(f"{'✅' if google_ok else '⬜'} *Gmail & Calendar* — receipts in email, spending profile")
    if not google_ok:
        lines.append(f"  → /connect\\_google, then open the [Janai extension]({ext_url})")
    lines.append(f"{'✅' if tg_ok else '⬜'} *Telegram messages* — read your chats for context")
    if not tg_ok:
        lines.append(f"  → /connect\\_telegram, then open the [Janai extension]({ext_url})")
    lines.append(f"{'✅' if wa_ok else '⬜'} *WhatsApp messages* — read your chats for context")
    if not wa_ok:
        lines.append(f"  → /connect\\_whatsapp, then scan QR in the [Janai extension]({ext_url})")
    if google_ok and tg_ok and wa_ok:
        lines.append("\n_All connected. I'll keep your profile updated automatically._")
    else:
        lines.append(f"\nNeed the extension? [Install it here]({ext_url}) — takes 30 seconds.")
    return "\n".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    u = storage.get_user(user_db_id)
    has_omnihr = bool(u and u.get("access_jwt"))
    if has_omnihr:
        # Already set up — show integration status instead of onboarding steps
        await update.message.reply_text(
            _setup_status_text(u),
            parse_mode="Markdown",
        )
    else:
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
        voice_for_user(storage.get_user(user_db_id)).text("login_link_prompt", bridge_url=bridge_url),
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
        voice_for_user(u).text("step1_done_ai_connected", next_steps=_next_step_prompt(user_db_id, u)),
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
    voice = voice_for_user(storage.get_user(user_db_id))
    # Big tap-to-copy code block — Telegram copies on tap/hold of ``` blocks.
    await update.message.reply_text(
        voice.text("pair_code_prompt", code=code, brand_name=voice.text("brand_name")),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_connect_google(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    if not await _check_rate(update, user_db_id, "pair"):
        return
    if not GOOGLE_CLIENT_ID:
        await update.message.reply_text(
            default_voice().text("google_not_configured")
        )
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    storage.create_pairing_code(user_db_id, code, ttl_seconds=300)
    voice = voice_for_user(storage.get_user(user_db_id))
    await update.message.reply_text(
        voice.text("google_pair_prompt", code=code, brand_name=voice.text("brand_name")),
        parse_mode="Markdown",
    )


async def cmd_connect_telegram(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a pairing code for connecting the user's personal Telegram account."""
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    if not await _check_rate(update, user_db_id, "pair"):
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    storage.create_pairing_code(user_db_id, code, ttl_seconds=300)
    from telegram import KeyboardButton, ReplyKeyboardMarkup
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share my number", request_contact=True)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"```\n{code}\n```\n"
        f"👆 Tap to copy.\n\n"
        f"*Step 1:* Tap *Share my number* below so I know which account to read.\n"
        f"*Step 2:* Open the [Janai extension]({PUBLIC_BASE_URL}/extension) → paste the code above → tap *Connect Telegram*.\n\n"
        f"Don't have the extension? [Install here]({PUBLIC_BASE_URL}/extension) — 30 seconds.\n\n"
        f"_(Code valid for 5 minutes.)_",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def cmd_connect_whatsapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a pairing code for connecting the user's WhatsApp account."""
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    if not await _check_rate(update, user_db_id, "pair"):
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    storage.create_pairing_code(user_db_id, code, ttl_seconds=300)
    await update.message.reply_text(
        f"```\n{code}\n```\n"
        f"👆 Tap to copy.\n\n"
        f"Open the [Janai extension]({PUBLIC_BASE_URL}/extension) → Connections tab → paste this code → scan the QR with WhatsApp (Linked Devices → Link a Device).\n\n"
        f"Don't have it? [Install here]({PUBLIC_BASE_URL}/extension) — 30 seconds.\n\n"
        f"_(Code valid for 5 minutes.)_",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/setup is an alias for /start."""
    await cmd_start(update, ctx)


_STATUS_EMOJI = {
    3: "📝",   # draft
    7: "📤",   # for approval
    1: "✅",   # approved
    2: "💰",   # reimbursed
    6: "❌",   # rejected
    8: "🗑",   # deleted
}


def _claim_summary(r: dict[str, Any], tenant_id: str | None = None, doc_id: int | None = None) -> str:
    status = r.get("status", 0)
    status_label = STATUS_LABELS.get(status, f"?{status}")
    emoji = _STATUS_EMOJI.get(status, "📄")
    policy = (r.get("policy") or {}).get("name") or "?"
    merchant = r.get("merchant") or ""
    desc = (r.get("description") or "")[:80]
    preview = ""
    if tenant_id and tenant_id != "unknown" and doc_id:
        preview = f"\nhttps://{tenant_id}.omnihr.co/document/preview/expense/{doc_id}/"
    return (
        f"{emoji} *{status_label}* · #{r['id']}\n"
        f"{r.get('receipt_date','?')} · {r.get('amount_currency','?')} {r.get('amount','?')}\n"
        f"{merchant} — {policy}\n"
        f"_{desc}_"
        f"{preview}"
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


_LIST_PAGE_SIZE = 10   # cards shown per Load more tap
_LIST_FETCH_SIZE = 50  # max fetched from OmniHR in one call


async def _do_list(
    update: Update,
    u: dict,
    status_key: str = "all",
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    is_callback: bool = False,
    offset: int = 0,
) -> None:
    """Shared list logic for /list command, filter callbacks, and Load more."""
    filters = FILTER_SHORTCUTS.get(status_key, ACTIVE_STATUS_FILTERS)

    async with client_for(u) as client:
        try:
            data = await client.list_submissions(status_filters=filters, page_size=_LIST_FETCH_SIZE)
        except AuthError:
            msg = "Session expired — run /connect_omnihr to re-link."
            if is_callback:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            return

    rows = data.get("results", [])

    # Client-side date filter
    if date_from or date_to:
        rows = [
            r for r in rows
            if (not date_from or r.get("receipt_date", "") >= date_from)
            and (not date_to or r.get("receipt_date", "") <= date_to)
        ]

    target = update.callback_query.message if is_callback else update.message

    date_label = ""
    if date_from and date_to:
        date_label = f" ({date_from} → {date_to})"
    elif date_from:
        date_label = f" (from {date_from})"

    total = len(rows)

    # Send header only on first load (offset == 0)
    if offset == 0:
        filter_kb = _list_filter_keyboard(status_key)
        if not rows:
            await target.reply_text(
                f"No *{status_key}*{date_label} claims found.",
                parse_mode="Markdown",
                reply_markup=filter_kb,
            )
            return
        await target.reply_text(
            f"_{status_key}{date_label}: {total} claim{'s' if total != 1 else ''}_",
            parse_mode="Markdown",
            reply_markup=filter_kb,
        )

    page_rows = rows[offset:offset + _LIST_PAGE_SIZE]
    for r in page_rows:
        local = storage.find_receipt_by_submission(u["id"], r["id"])
        doc_id = local.get("omnihr_doc_id") if local else None
        caption = _claim_summary(r, tenant_id=u.get("tenant_id"), doc_id=doc_id)
        kb = _claim_buttons(r)
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
        await target.reply_text(caption, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)

    # "Load more" button if there are more results
    next_offset = offset + _LIST_PAGE_SIZE
    if next_offset < total:
        remaining = total - next_offset
        df = date_from or ""
        dt = date_to or ""
        more_cb = f"listmore:{status_key}:{next_offset}:{df}:{dt}"
        await target.reply_text(
            f"_{remaining} more_ — tap to load",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Load more ({remaining})", callback_data=more_cb)
            ]]),
        )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    if not u or not u.get("access_jwt"):
        await update.message.reply_text("Not paired yet — run /connect_omnihr")
        return
    if not await _check_rate(update, u["id"], "list"):
        return

    status_key, date_from, date_to = _parse_list_args(ctx.args or [])
    await _do_list(update, u, status_key, date_from, date_to)


def _build_confirm_message(
    parsed: ParsedReceipt,
    policies: list[PolicyEntry],
    sha: str | None = None,
    triangulation_md: str | None = None,
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

    if triangulation_md:
        lines.append("")
        lines.append(triangulation_md)

    text = "\n".join(lines)

    # Correction row — always shown as a second row of buttons
    correction_row = [
        InlineKeyboardButton("✏️ Edit description", callback_data="edit_desc:"),
    ]
    if sha:
        correction_row.append(
            InlineKeyboardButton("🔄 Search again", callback_data=f"retriangulate:{sha}")
        )

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
            rows.append(correction_row)
            rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_file")])
        else:
            text += "\n\nI couldn't auto-classify and no policies are available. Try adding a hint as a caption (e.g. 'travel local')."
            rows = [correction_row, [InlineKeyboardButton("❌ Cancel", callback_data="cancel_file")]]
        return text, InlineKeyboardMarkup(rows)

    policy_label = next(
        (p.label for p in policies if p.id == parsed.suggested_policy_id),
        f"policy #{parsed.suggested_policy_id}",
    )
    text += f"\nPolicy: *{policy_label}*\n\nFile this as a draft?"
    return text, InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ File it", callback_data="confirm_file"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_file"),
        ],
        correction_row,
    ])


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
        await _fail("Session expired — run /connect_omnihr to re-link.")
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

    if action == "edit_desc":
        await q.answer()
        chat_id = q.message.chat_id if q.message else q.from_user.id
        pending = _pending_files.get(chat_id)
        if not pending:
            try:
                await q.edit_message_text("No pending receipt — please resend the file.")
            except Exception:
                pass
            return
        try:
            await q._bot.send_message(
                chat_id=chat_id,
                text=voice_for_user(pending.u).text("description_edit_prompt"),
            )
        except Exception as e:
            log.warning("edit_desc send failed: %s", e)
        return

    if action == "retriangulate":
        await q.answer("Searching again…")
        chat_id = q.message.chat_id if q.message else q.from_user.id
        pending = _pending_files.get(chat_id)
        if not pending:
            try:
                await q.edit_message_text("No pending receipt — please resend the file.")
            except Exception:
                pass
            return
        parsed = pending.parsed
        try:
            SGT = timezone(timedelta(hours=8))
            from datetime import time as _time
            dt = datetime.combine(parsed.receipt_date, _time(12, 0), tzinfo=SGT) if parsed.receipt_date else datetime.now(SGT)
            result = await asyncio.wait_for(
                triangulate(
                    merchant=parsed.merchant or "",
                    dt=dt,
                    receipt_type=_infer_receipt_type(parsed),
                    user_id=pending.u.get("id"),
                ),
                timeout=5.0,
            )
            tri_md = result.as_markdown()
        except Exception as e:
            log.warning("retriangulate failed: %s", e)
            tri_md = None
        confirm_text, confirm_kb = _build_confirm_message(
            parsed, pending.policies, sha=pending.sha, triangulation_md=tri_md
        )
        try:
            await q.edit_message_text(confirm_text, parse_mode="Markdown", reply_markup=confirm_kb)
        except Exception as e:
            log.warning("retriangulate edit failed: %s", e)
        return

    if not u or not u.get("access_jwt"):
        await q.answer("Not paired — run /connect_omnihr", show_alert=True)
        return

    # Handle list filter callbacks: list:approved, list:draft, etc.
    if action == "list":
        await q.answer()
        status_key = rest or "all"
        await _do_list(update, u, status_key, is_callback=True)
        return

    # Load more: listmore:{status_key}:{offset}:{date_from}:{date_to}
    if action == "listmore":
        await q.answer()
        parts = (rest or "").split(":", 3)
        sk = parts[0] if parts else "all"
        off = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        df = parts[2] if len(parts) > 2 and parts[2] else None
        dt = parts[3] if len(parts) > 3 and parts[3] else None
        await _do_list(update, u, sk, df, dt, is_callback=True, offset=off)
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
                await _record_merchant_after_submit(client, u, claim_id)
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
        await _reply("Session expired — run /connect_omnihr to re-link.")
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
            await _record_merchant_after_submit(client, u, sub_id)
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


async def _record_merchant_after_submit(client, u: dict, submission_id: int) -> None:
    """Best-effort: fetch the submitted claim and record the merchant pattern
    for Janai's memory. Errors are swallowed — recording must never break
    the submit flow."""
    try:
        claim = await client.get_submission(submission_id)
    except Exception as e:
        log.warning("get_submission fetch failed for #%s: %s", submission_id, e)
        return
    if not claim:
        return
    merchant = (claim.get("merchant") or "").strip()
    policy_id = (claim.get("policy") or {}).get("id") or ""
    sub_cat = (claim.get("sub_category") or {}).get("name")
    if merchant and policy_id:
        try:
            storage.record_merchant_choice(u["id"], merchant, str(policy_id), sub_cat)
        except Exception:
            log.exception("record_merchant_choice failed for user=%s", u["id"])


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
            except Exception as e:
                return f"Parse failed: {e}"

            # Dupe sniff — best-effort; failure must not block the parse result.
            dupe_warning = ""
            try:
                async with client_for(u) as client:
                    # 90 matches the submit-side merchant-record paging window in
                    # omnihr_client.get_submission — keep the dupe horizon aligned.
                    recent = await client.list_submissions(page_size=90)
                hints = match_dupes(parsed, recent.get("results", []))
                dupe_warning = format_dupe_warning(hints)
            except Exception as e:
                log.warning("dupe sniff failed: %s", e)

            parsed_json = json.dumps(parsed.raw, default=str)
            if dupe_warning:
                return f"{dupe_warning}\n\n---\n\n{parsed_json}"
            return parsed_json

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
                await _record_merchant_after_submit(client, u, cid)
            storage.increment_submit_count(u["id"])
            recent = storage.get_recent_messages(u["id"], limit=12)
            asyncio.create_task(learning.maybe_trigger_review(
                user_id=u["id"], db=storage, anthropic_client=await anthropic_for(u),
                recent_messages=recent, trigger="submit",
            ))
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

        elif tool_name == "list_recent_emails":
            days = int(tool_input.get("days", 7))
            now_sgt = datetime.now(timezone(timedelta(hours=8)))
            results = await asyncio.wait_for(context_lookup.gmail_context("", now_sgt, user_id=u.get("id"), window_days=days), timeout=8.0)
            return "\n".join(results) if results else f"No emails found in the last {days} days."

        elif tool_name == "list_upcoming_events":
            days = int(tool_input.get("days", 7))
            now_sgt = datetime.now(timezone(timedelta(hours=8)))
            results = await asyncio.wait_for(context_lookup.gcal_context(now_sgt, user_id=u.get("id"), broad=True, window_hours=days * 24), timeout=8.0)
            return "\n".join(results) if results else f"No upcoming events in the next {days} days."

        elif tool_name == "search_email_context":
            merchant = tool_input.get("merchant", "")
            date_hint = tool_input.get("date_hint", "")
            time_hint = tool_input.get("time_hint", "")
            now_sgt = datetime.now(timezone(timedelta(hours=8)))
            if date_hint:
                try:
                    dt = datetime.fromisoformat(f"{date_hint}T{time_hint}" if time_hint else date_hint)
                except ValueError:
                    dt = now_sgt
            else:
                dt = now_sgt
            results = await asyncio.wait_for(context_lookup.gmail_context(merchant, dt, user_id=u.get("id")), timeout=8.0)
            return "\n".join(results) if results else "No relevant emails found."

        elif tool_name == "search_calendar_context":
            date_hint = tool_input.get("date_hint", "")
            time_hint = tool_input.get("time_hint", "")
            now_sgt = datetime.now(timezone(timedelta(hours=8)))
            if date_hint:
                try:
                    dt = datetime.fromisoformat(f"{date_hint}T{time_hint}" if time_hint else date_hint)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                except ValueError:
                    dt = now_sgt
            else:
                dt = now_sgt
            results = await asyncio.wait_for(context_lookup.gcal_context(dt, user_id=u.get("id"), broad=False), timeout=8.0)
            return "\n".join(results) if results else "No calendar events found near that time."

        elif tool_name == "get_whatsapp_messages":
            days = int(tool_input.get("days", 7))
            if not u.get("whatsapp_connected"):
                return "WhatsApp is not connected. Ask the user to run /connect_whatsapp."
            from .common.boss_profile import _bulk_whatsapp
            since = datetime.now(timezone.utc) - timedelta(days=days)
            msgs = await asyncio.wait_for(_bulk_whatsapp(user_id=u["id"], since=since), timeout=10.0)
            return "\n".join(msgs) if msgs else f"No WhatsApp messages found in the last {days} days."

        elif tool_name == "get_telegram_messages":
            days = int(tool_input.get("days", 7))
            if not u.get("telegram_session"):
                return "Telegram is not connected. Ask the user to run /connect_telegram."
            from .common.boss_profile import _bulk_telegram
            since = datetime.now(timezone.utc) - timedelta(days=days)
            msgs = await asyncio.wait_for(_bulk_telegram(user_id=u["id"], since=since), timeout=10.0)
            return "\n".join(msgs) if msgs else f"No Telegram messages found in the last {days} days."

        elif tool_name == "get_omnihr_context":
            tenant_md = load_tenant_md(u.get("tenant_id"))
            policy_path = REPO_ROOT / "bot" / "skills" / "omnihr" / "policy.md"
            policy_md = policy_path.read_text() if policy_path.exists() else ""
            merchants = storage.top_merchants(u["id"], limit=20)
            merchants_rendered = render_merchants_block(merchants)
            try:
                async with client_for(u) as client:
                    recent = await client.list_submissions(page_size=15)
                rows = recent.get("results", [])
                claims_lines = [
                    f"#{r['id']} {r.get('receipt_date','?')} "
                    f"{r.get('amount_currency','?')} {r.get('amount','?')} "
                    f"{r.get('merchant') or '?'} "
                    f"[{STATUS_LABELS.get(r.get('status', 0), '?')}]"
                    for r in rows
                ]
                claims_md = "\n".join(claims_lines) if claims_lines else "(no recent claims)"
            except Exception as e:
                claims_md = f"(couldn't fetch: {e})"
            parts = [f"## Org config\n{tenant_md[:2000]}"]
            if policy_md:
                parts.append(f"## Expense policy\n{policy_md[:3500]}")
            if merchants_rendered:
                parts.append(
                    f"## Merchants you've filed before\n{merchants_rendered}\n"
                    f"_(confident) = filed same way 3+ times — file without asking._"
                )
            parts.append(f"## Recent claims\n{claims_md}")
            return "\n\n".join(parts)

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
        progress = await msg.reply_text(voice_for_user(u).text("oauth_progress"))
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
                voice_for_user(u).text(
                    "step1_done_claude_linked",
                    next_steps=_next_step_prompt(token_data["user_db_id"], u),
                ),
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
            voice_for_user(u).text("connect_ai_first"),
            parse_mode="Markdown",
        )
        return

    if not await _check_rate(update, u["id"], "parse"):
        return

    progress = await msg.reply_text(voice_for_user(u).text("agent_progress"))
    user_md = load_user_md(u)
    executor = await _build_tool_executor(u)
    history = storage.get_recent_messages(u["id"], limit=12)

    try:
        reply = await run_agent(
            anthropic=anth,
            user_message=text,
            has_file=False,
            user_md=user_md,
            profile_md=storage.get_profile_md(u["id"]),
            boss_profile_md=storage.get_boss_profile_md(u["id"]),
            tool_executor=executor,
            conversation_history=history,
            user=u,
        )
    except Exception as e:
        log.warning("agent failed: %s", e)
        reply = f"Something went wrong: {e}"

    storage.log_message(u["id"], "out", reply)
    asyncio.create_task(learning.maybe_trigger_review(
        user_id=u["id"], db=storage, anthropic_client=anth,
        recent_messages=history, trigger="turn",
    ))
    try:
        await progress.edit_text(reply, parse_mode="Markdown")
    except Exception:
        await progress.edit_text(reply)  # fallback without markdown


async def on_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User shared their phone number via the request_contact keyboard."""
    from telegram import ReplyKeyboardRemove
    contact = update.message.contact
    if not contact or contact.user_id != update.effective_user.id:
        return  # shared someone else's contact, ignore
    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    # Store phone so /extension/telegram-init can use it without the user typing it
    with storage.db() as conn:
        conn.execute("UPDATE users SET telegram_phone=? WHERE id=?", (phone, user_db_id))
    await update.message.reply_text(
        f"✅ Got it — {phone}.\n\nNow open the Janai extension, paste the code, and tap *Connect Telegram*.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


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
            voice_for_user(u).text(
                "connect_ai_before_receipts",
                next_steps=_next_step_prompt(u["id"], u),
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return
    if not u.get("access_jwt"):
        await msg.reply_text(
            voice_for_user(u).text(
                "connect_omnihr_before_receipts",
                next_steps=_next_step_prompt(u["id"], u),
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    # Download file. Check Telegram's reported size BEFORE downloading so a
    # malicious user can't force us to pull a 20 MB file and blow the worker's
    # memory.
    if msg.document:
        if msg.document.file_size and msg.document.file_size > MAX_RECEIPT_BYTES:
            await msg.reply_text(
                f"File too large ({msg.document.file_size} bytes). "
                f"Max is {MAX_RECEIPT_BYTES} bytes."
            )
            return
        tg_file = await msg.document.get_file()
        media_type = msg.document.mime_type or "application/pdf"
        filename = msg.document.file_name or "receipt.pdf"
        tg_file_id = msg.document.file_id
        tg_file_type = "document"
    elif msg.photo:
        photo = msg.photo[-1]
        if photo.file_size and photo.file_size > MAX_RECEIPT_BYTES:
            await msg.reply_text(
                f"Photo too large ({photo.file_size} bytes). "
                f"Max is {MAX_RECEIPT_BYTES} bytes."
            )
            return
        tg_file = await photo.get_file()
        media_type = "image/jpeg"  # default; corrected by magic-byte sniff below
        filename = "receipt.jpg"
        tg_file_id = photo.file_id
        tg_file_type = "photo"
    else:
        return

    file_bytes = bytes(await tg_file.download_as_bytearray())
    # Final guard in case the Telegram-reported size was missing / lied.
    if len(file_bytes) > MAX_RECEIPT_BYTES:
        await msg.reply_text(
            f"File too large ({len(file_bytes)} bytes). Max is {MAX_RECEIPT_BYTES} bytes."
        )
        return

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

        # Run triangulation (Gmail + Calendar) in parallel with a 2-second budget.
        # This is best-effort — never block filing on it.
        triangulation_md: str | None = None
        if parsed.merchant and parsed.receipt_date:
            try:
                SGT = timezone(timedelta(hours=8))
                from datetime import time as _time
                receipt_dt = datetime.combine(parsed.receipt_date, _time(12, 0), tzinfo=SGT)
                tri_result = await asyncio.wait_for(
                    triangulate(
                        merchant=parsed.merchant,
                        dt=receipt_dt,
                        receipt_type=_infer_receipt_type(parsed),
                        user_id=u.get("id"),
                    ),
                    timeout=2.0,
                )
                triangulation_md = tri_result.as_markdown()
            except asyncio.TimeoutError:
                log.debug("triangulation timed out — proceeding without context")
            except Exception as e:
                log.warning("triangulation failed: %s", e)

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
            triangulation_md=triangulation_md,
        )
        confirm_text, confirm_kb = _build_confirm_message(
            parsed, policies, sha=sha, triangulation_md=triangulation_md
        )
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

    # Restrict CORS to (a) our public host, (b) localhost for dev, and
    # (c) chrome-extension origins (32-char ids, a–p only per Chrome's
    # encoding). The prior `allow_origins=["*"]` let any site on the web
    # POST to /extension/pair.
    from fastapi.middleware.cors import CORSMiddleware
    allowed_origins: list[str] = []
    if PUBLIC_BASE_URL:
        allowed_origins.append(PUBLIC_BASE_URL.rstrip("/"))
    allowed_origins += [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    extra = os.environ.get("EXTRA_CORS_ORIGINS", "").strip()
    if extra:
        allowed_origins.extend(o.strip() for o in extra.split(",") if o.strip())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(dict.fromkeys(allowed_origins)),
        allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
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
        voice = default_voice()
        brand_name = voice.text("brand_name")
        page = f"""<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{brand_name} — {voice.text("auth_start_title")}</title>
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
  <h1>💰 {brand_name}</h1>
  <div class="sub">{voice.text("auth_start_subtitle")}</div>

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
                voice = voice_for_user(u)
                await tg_app.bot.send_message(
                    chat_id=token_data["telegram_user_id"],
                    text=voice.text(
                        "step1_done_claude_linked",
                        next_steps=_next_step_prompt(token_data["user_db_id"], u),
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
                voice = voice_for_user(u)
                await tg_app.bot.send_message(
                    chat_id=pending.telegram_user_id,
                    text=voice.text(
                        "step1_done_api_key_saved",
                        next_steps=_next_step_prompt(pending.user_db_id, u),
                    ),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        return {"ok": True, "next": _web_next_step_html(pending.user_db_id, u)}

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        icon_path = Path(__file__).parent.parent / "extension" / "favicon.ico"
        if icon_path.exists():
            return Response(
                content=icon_path.read_bytes(),
                media_type="image/x-icon",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<text y=".9em" font-size="90">\U0001F4B0</text>'
            "</svg>"
        )
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/icon-128.png")
    async def icon_png() -> Response:
        icon_path = Path(__file__).parent.parent / "extension" / "icons" / "icon-128.png"
        if icon_path.exists():
            return Response(
                content=icon_path.read_bytes(),
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        raise HTTPException(status_code=404)

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
    async def extension_pair(p: PairPayload, request: Request) -> dict:
        # Rate-limit by client IP. The pairing code space is only 1M combos;
        # without a per-IP cap an attacker can brute-force codes against the
        # 5-min TTL window. This is independent of the telegram-side /pair
        # per-user rate limit.
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(
                status_code=429,
                detail="Too many pairing attempts, try again later.",
                headers={"Retry-After": str(retry)},
            )

        # Validate pairing code shape before hitting the DB.
        if not re.fullmatch(r"\d{6}", p.pairing_code or ""):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")

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

        # Derive tenant id from org (subdomain best, but use org name as fallback).
        # Run through sanitize_tenant_id so the resulting slug is safe to use as
        # a filename in TENANTS_DIR (no path traversal via crafted org names).
        org_name = (p.org or {}).get("name") or me.get("org", {}).get("name") or ""
        first_token = org_name.lower().split()[0] if org_name else ""
        tenant_id = sanitize_tenant_id(first_token)

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

        import uuid as _uuid
        ext_token = str(_uuid.uuid4())
        storage.set_ext_session(user_db_id, ext_token)
        return {"ok": True, "user_id": user_db_id, "ext_session": ext_token}

    @app.get("/extension/status")
    async def extension_status(token: str = "") -> dict:
        if not token:
            raise HTTPException(status_code=400, detail="token required")
        u = storage.get_user_by_ext_session(token)
        if not u:
            raise HTTPException(status_code=404, detail="session not found")
        uid = u["id"]
        google_ok = bool(storage.get_google_tokens(uid)[0])
        return {
            "paired": bool(u.get("access_jwt")),
            "name": u.get("omnihr_full_name") or "",
            "google": google_ok,
            "google_email": u.get("google_email") or "",
            "telegram": bool(storage.get_telegram_session(uid)),
            "telegram_phone": u.get("telegram_phone") or "",
            "whatsapp": storage.get_whatsapp_connected(uid),
            "whatsapp_phone": u.get("whatsapp_phone") or "",
        }

    @app.get("/config/google")
    async def config_google() -> dict:
        return {"client_id": GOOGLE_CLIENT_ID}

    @app.post("/extension/google-auth")
    async def extension_google_auth(p: dict[str, Any], request: Request) -> dict:
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry)})

        pairing_code = p.get("pairing_code", "")
        auth_code = p.get("auth_code", "")
        redirect_uri = p.get("redirect_uri", "")

        if not re.fullmatch(r"\d{6}", pairing_code):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")
        if not auth_code:
            raise HTTPException(status_code=400, detail="auth_code required")
        if not redirect_uri:
            raise HTTPException(status_code=400, detail="redirect_uri required")
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="Google integration not configured")

        user_db_id = storage.consume_pairing_code(pairing_code)
        if not user_db_id:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")

        # Exchange auth code for tokens
        async with httpx.AsyncClient() as http:
            r = await http.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "code": auth_code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=10.0,
            )
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Google token exchange failed: {r.text[:200]}")

        token_data = r.json()
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3600)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Fetch the user's Google email
        google_email: str | None = None
        async with httpx.AsyncClient() as http:
            info_r = await http.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5.0,
            )
        if info_r.status_code == 200:
            google_email = info_r.json().get("email")

        storage.set_google_tokens(
            user_db_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry,
            email=google_email,
        )

        user = storage.get_user(user_db_id)

        # DM confirmation
        if tg_app:
            try:
                await tg_app.bot.send_message(
                    chat_id=int(user["channel_user_id"]),
                    text=(
                        f"✅ Gmail & Calendar connected"
                        + (f" ({google_email})" if google_email else "")
                        + ".\n\nReading your history now so I know you better. Give me a moment, darling. 📚"
                    ),
                )
            except Exception as e:
                log.warning("Couldn't DM after google-auth: %s", e)

        # Kick off background profile build
        asyncio.create_task(_build_boss_profile_bg(user, tg_app))

        return {"ok": True, "email": google_email}

    # -----------------------------------------------------------------------
    # Telegram personal reader endpoints
    # -----------------------------------------------------------------------

    # In-memory phone lookup for telegram init → verify handshake
    # Maps user_db_id -> E.164 phone string
    _tg_phones: dict[int, str] = {}

    @app.post("/extension/telegram-init")
    async def extension_telegram_init(p: dict[str, Any], request: Request) -> dict:
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry)})

        pairing_code = p.get("pairing_code", "")
        if not re.fullmatch(r"\d{6}", pairing_code):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")

        # Peek at pairing code without consuming — we need it again for verify
        with storage.db() as conn:
            row = conn.execute(
                "SELECT user_id FROM pairing_codes WHERE code=? AND expires_at >= datetime('now')",
                (pairing_code,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")
        user_db_id = row["user_id"]

        # Phone comes from the contact share in-bot, or falls back to extension-supplied
        user = storage.get_user(user_db_id)
        phone = (user or {}).get("telegram_phone") or p.get("phone", "")
        if not phone:
            raise HTTPException(status_code=400, detail="Share your phone number in the bot first (tap the 'Share my number' button)")

        _tg_phones[user_db_id] = phone

        from .common.telegram_reader import start_phone_auth
        try:
            await start_phone_auth(user_db_id, phone)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Telegram auth failed: {e}")

        return {"ok": True}

    @app.post("/extension/telegram-verify")
    async def extension_telegram_verify(p: dict[str, Any], request: Request) -> dict:
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry)})

        pairing_code = p.get("pairing_code", "")
        code = p.get("code", "")
        if not re.fullmatch(r"\d{6}", pairing_code):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")

        user_db_id = storage.consume_pairing_code(pairing_code)
        if not user_db_id:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")

        from .common.telegram_reader import verify_phone_code, _pending as _tg_pending
        if user_db_id not in _tg_pending:
            raise HTTPException(status_code=400, detail="No pending Telegram auth — call /telegram-init first")

        try:
            session_str = await verify_phone_code(user_db_id, code)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Telegram verification failed: {e}")

        if not session_str:
            raise HTTPException(status_code=400, detail="Verification failed — invalid code?")

        phone = _tg_phones.pop(user_db_id, "")
        storage.set_telegram_session(user_db_id, session_str, phone)

        user = storage.get_user(user_db_id)

        # Kick off a background boss profile rebuild
        if tg_app and user:
            asyncio.create_task(_build_boss_profile_bg(user, tg_app))
            try:
                await tg_app.bot.send_message(
                    chat_id=int(user["channel_user_id"]),
                    text="Telegram connected. I'll now read your messages when building your profile. 📱",
                )
            except Exception as e:
                log.warning("Couldn't DM after telegram-verify: %s", e)

        return {"ok": True}

    # -----------------------------------------------------------------------
    # WhatsApp bridge endpoints
    # -----------------------------------------------------------------------

    WHATSAPP_BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://localhost:3001")

    @app.post("/extension/whatsapp-init")
    async def extension_whatsapp_init(p: dict[str, Any], request: Request) -> dict:
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry)})

        pairing_code = p.get("pairing_code", "")
        if not re.fullmatch(r"\d{6}", pairing_code):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")

        # Peek without consuming — QR polling needs the pairing code still valid
        with storage.db() as conn:
            row = conn.execute(
                "SELECT user_id FROM pairing_codes WHERE code=? AND expires_at >= datetime('now')",
                (pairing_code,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")
        user_db_id = row["user_id"]

        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{WHATSAPP_BRIDGE_URL}/session/{user_db_id}",
                    timeout=10.0,
                )
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail=f"WhatsApp bridge error: {r.text[:200]}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"WhatsApp bridge unreachable: {e}")

        return {"ok": True, "session_id": str(user_db_id)}

    @app.get("/extension/whatsapp-qr")
    async def extension_whatsapp_qr(pairing_code: str, request: Request) -> dict:
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry)})

        if not re.fullmatch(r"\d{6}", pairing_code or ""):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")

        with storage.db() as conn:
            row = conn.execute(
                "SELECT user_id FROM pairing_codes WHERE code=? AND expires_at >= datetime('now')",
                (pairing_code,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")
        user_db_id = row["user_id"]

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{WHATSAPP_BRIDGE_URL}/qr/{user_db_id}",
                    timeout=5.0,
                )
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail="Bridge error")
            data = r.json()
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"WhatsApp bridge unreachable: {e}")

        # Persist connected state so /extension/status reflects it immediately
        if data.get("connected") and not storage.get_whatsapp_connected(user_db_id):
            phone = data.get("phone") or ""
            storage.set_whatsapp_connected(user_db_id, phone)
            user = storage.get_user(user_db_id)
            if tg_app and user:
                asyncio.create_task(_build_boss_profile_bg(user, tg_app))

        return data

    @app.get("/extension/whatsapp-status")
    async def extension_whatsapp_status(pairing_code: str, request: Request) -> dict:
        client_ip = (request.client.host if request.client else "unknown") or "unknown"
        ok, retry = rate_limit.check(f"ip:{client_ip}", "ip_pair")
        if not ok:
            raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry)})

        if not re.fullmatch(r"\d{6}", pairing_code or ""):
            raise HTTPException(status_code=400, detail="Pairing code must be 6 digits")

        with storage.db() as conn:
            row = conn.execute(
                "SELECT user_id FROM pairing_codes WHERE code=? AND expires_at >= datetime('now')",
                (pairing_code,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pairing code invalid or expired")
        user_db_id = row["user_id"]

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{WHATSAPP_BRIDGE_URL}/status/{user_db_id}",
                    timeout=5.0,
                )
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail="Bridge error")
            data = r.json()
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"WhatsApp bridge unreachable: {e}")

        # If newly connected and not yet stored, persist and trigger profile rebuild
        connected = data.get("connected", False)
        phone = data.get("phone") or ""
        if connected and not storage.get_whatsapp_connected(user_db_id):
            storage.set_whatsapp_connected(user_db_id, phone)
            user = storage.get_user(user_db_id)
            if tg_app and user:
                asyncio.create_task(_build_boss_profile_bg(user, tg_app))
                try:
                    await tg_app.bot.send_message(
                        chat_id=int(user["channel_user_id"]),
                        text=f"WhatsApp connected{' as ' + phone if phone else ''}. Reading your history now. 📲",
                    )
                except Exception as e:
                    log.warning("Couldn't DM after whatsapp-status: %s", e)

        return {"connected": connected, "phone": phone or None}

    return app


# ---------------------------------------------------------------------------
# Background: boss profile builder
# ---------------------------------------------------------------------------

async def _build_boss_profile_bg(user: dict, tg_app) -> None:
    """Background task: fetch all claims + Gmail + Calendar, condense into boss profile."""
    user_id = user["id"]
    first_name = (user.get("full_name") or "").split()[0] if user.get("full_name") else ""
    try:
        anth = await anthropic_for(user)
        tokens = storage.get_omnihr_tokens(user_id)
        access_token = tokens[0] if tokens else None
        if not access_token:
            log.warning("boss_profile: no OmniHR token for user=%s", user_id)
            return

        tenant_id = user.get("tenant_id") or ""
        async with httpx.AsyncClient(
            base_url="https://api.omnihr.co/api/v1",
            cookies={"access_token": access_token},
            timeout=20.0,
        ) as client:
            profile = await refresh_boss_profile(
                user_id=user_id,
                omnihr_http_client=client,
                tenant_id=tenant_id,
                first_name=first_name,
                anthropic_client=anth,
            )

        if profile and tg_app:
            try:
                await tg_app.bot.send_message(
                    chat_id=int(user["channel_user_id"]),
                    text="I've read through your history and I know you now. Ready when you are. 💼",
                )
            except Exception:
                pass
    except Exception as e:
        log.warning("_build_boss_profile_bg failed for user=%s: %s", user_id, e)


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
    tg_app.add_handler(CommandHandler("connect_omnihr", cmd_pair))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("delete", cmd_delete))
    tg_app.add_handler(CommandHandler("submit", cmd_submit))
    tg_app.add_handler(CommandHandler("memories", cmd_memories))
    tg_app.add_handler(CommandHandler("setup", cmd_setup))
    tg_app.add_handler(CommandHandler("connect_google", cmd_connect_google))
    tg_app.add_handler(CommandHandler("connect_telegram", cmd_connect_telegram))
    tg_app.add_handler(CommandHandler("connect_whatsapp", cmd_connect_whatsapp))
    tg_app.add_handler(MessageHandler(filters.CONTACT, on_contact))
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_file))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def on_error(update, context):
        log.exception("unhandled error in handler", exc_info=context.error)

    tg_app.add_error_handler(on_error)

    app = make_app(tg_app)

    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
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
            BotCommand("start", "Status & integrations"),
            BotCommand("login", "Connect your Claude AI"),
            BotCommand("connect_omnihr", "Link your OmniHR account"),
            BotCommand("connect_google", "Connect Gmail & Calendar"),
            BotCommand("connect_telegram", "Read your Telegram messages"),
            BotCommand("connect_whatsapp", "Read your WhatsApp messages"),
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

    from .heartbeat import HeartbeatRunner
    heartbeat = HeartbeatRunner(
        telegram_bot=tg_app.bot,
        anthropic_factory=anthropic_for,
        omnihr_factory=client_for,
    )
    heartbeat.start()

    try:
        await server.serve()
    finally:
        heartbeat.stop()
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
