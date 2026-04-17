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
from datetime import datetime, timezone
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
from omnihr_client.schema import invalidate_schema

from . import access, claude_oauth, legal, logging_setup, rate_limit, storage
from .common.agent_parser import parse_receipt_via_agent
from .common.parser import parse_receipt

logging_setup.setup()
log = logging.getLogger("expensebot")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")

REPO_ROOT = Path(__file__).parent.parent
TENANTS_DIR = REPO_ROOT / "tenants"


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
    return _user.get("user_md") or "(no per-user rules yet)"


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
    return OmniHRClient(
        tokens=tokens,
        employee_id=user["omnihr_employee_id"],
        tenant_id=user["tenant_id"] or "unknown",
    )


_ANTH_PLACEHOLDER_PREFIXES = ("sk-ant-...", "sk-ant-xxx", "sk-ant-your")


def _plausible_anth_key(key: str | None) -> bool:
    if not key:
        return False
    low = key.strip().lower()
    if any(low.startswith(p) for p in _ANTH_PLACEHOLDER_PREFIXES):
        return False
    return key.startswith("sk-ant-") and len(key) > 30


def anthropic_for(user: dict) -> AsyncAnthropic:
    user_key = storage.get_anth_key(user["id"])
    maintainer_key = os.environ.get("MAINTAINER_ANTHROPIC_API_KEY", "").strip()
    key = user_key if _plausible_anth_key(user_key) else (
        maintainer_key if _plausible_anth_key(maintainer_key) else None
    )
    if not key:
        raise RuntimeError("No Anthropic key — run /setkey sk-ant-…")
    return AsyncAnthropic(api_key=key, max_retries=0)


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


WELCOME = (
    "Hi! I file OmniHR claims for you.\n\n"
    "Setup:\n"
    "1. /setkey <your-anthropic-key>  (BYOK; ~$0.02 per receipt)\n"
    "2. Install the extension: load `extension/` unpacked in chrome://extensions\n"
    "3. /pair  → enter the code in the extension popup\n\n"
    "Then send any receipt photo or PDF. I'll parse, classify, and file as a draft."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))
    await update.message.reply_text(WELCOME)


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start OAuth PKCE flow — user taps link, authorizes, auto-redirected back."""
    if not await _gate(update):
        return
    user_db_id = storage.upsert_user("telegram", str(update.effective_user.id))

    auth_url, state = claude_oauth.start_login(
        telegram_user_id=update.effective_user.id,
        user_db_id=user_db_id,
        public_base_url=PUBLIC_BASE_URL,
    )

    await update.message.reply_text(
        f"[👆 Tap here to sign in with Claude]({auth_url})\n\n"
        f"After authorizing, you'll be redirected back automatically.\n"
        f"_(link expires in 10 minutes)_",
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
    await update.message.reply_text("✅ Key saved. Next: /pair")


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
        f"1. Open any omnihr.co tab (signed in)\n"
        f"2. Click the ExpenseBot extension icon\n"
        f"3. Paste the code → Pair\n\n"
        f"Expires in 5 minutes.",
        parse_mode="Markdown",
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


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline-keyboard taps: submit:<id>, delete:<id>, confirm_delete:<id>."""
    q = update.callback_query
    if not q or not q.data:
        return
    log.info("callback: user=%s data=%s", q.from_user.id if q.from_user else "?", q.data)
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(q.from_user.id))
    if not u or not u.get("access_jwt"):
        await q.answer("Not paired — run /pair", show_alert=True)
        return
    action, _, rest = q.data.partition(":")

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


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    if not u:
        await update.message.reply_text("No account to export.")
        return
    data = storage.export_user_data(u["id"])
    import io, json
    buf = io.BytesIO(json.dumps(data, indent=2, default=str).encode())
    buf.name = "expensebot-export.json"
    await update.message.reply_document(document=buf, filename=buf.name)


async def cmd_delete_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    # Two-step: first /delete-account replies with a warning; second "CONFIRM"
    # within 60s actually purges.
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    if not u:
        await update.message.reply_text("No account to delete.")
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if text.strip().upper() != "CONFIRM":
        await update.message.reply_text(
            "This wipes your key, OmniHR tokens, parsed receipts (claims on "
            "OmniHR are not affected — you filed those yourself).\n\n"
            "To proceed, send: `/delete-account CONFIRM`",
            parse_mode="Markdown",
        )
        return
    storage.delete_user(u["id"])
    await update.message.reply_text("✅ Account purged.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for free-form text — routes through Claude for questions."""
    if not await _gate(update):
        return
    msg = update.message
    u = storage.get_user_by_channel("telegram", str(msg.from_user.id))
    if not u:
        await msg.reply_text("Hi! Run /start first.")
        return

    text = (msg.text or "").strip()
    if not text:
        return

    # Need Anthropic key for the conversational layer
    try:
        anth = anthropic_for(u)
    except RuntimeError:
        await msg.reply_text("Set your API key first: /setkey sk-ant-…")
        return

    if not await _check_rate(update, u["id"], "parse"):
        return

    # Build context: recent claims from OmniHR (if paired)
    claims_summary = "(not paired — no claims data available)"
    if u.get("access_jwt"):
        try:
            async with client_for(u) as client:
                data = await client.list_submissions(page_size=20)
                rows = data.get("results", [])
                if rows:
                    lines = []
                    for r in rows:
                        status_label = STATUS_LABELS.get(r.get("status", 0), "?")
                        lines.append(
                            f"- #{r['id']} {r.get('receipt_date','?')} "
                            f"{r.get('amount_currency','?')} {r.get('amount','?')} "
                            f"{r.get('merchant') or '?'} — {(r.get('policy') or {}).get('name','?')} "
                            f"[{status_label}] "
                            f"{(r.get('description') or '')[:60]}"
                        )
                    claims_summary = "\n".join(lines)
                else:
                    claims_summary = "(no claims found)"
        except Exception as e:
            claims_summary = f"(couldn't fetch claims: {e})"

    tenant_md = load_tenant_md(u.get("tenant_id"))
    user_md = load_user_md(u)

    # Single Claude call — system prompt + HRMS skill + tenant + claims as context
    hrms_skill = load_skill("omnihr")  # TODO: read from tenant config
    try:
        resp = await anth.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=SYSTEM_PROMPT_MD,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"## HRMS integration\n{hrms_skill[:3000]}\n\n"
                                f"## Org rules\n{tenant_md[:2000]}\n\n"
                                f"## User preferences\n{user_md[:500]}\n\n"
                                f"## Recent claims\n{claims_summary}\n\n"
                                f"## User's message\n{text}"
                            ),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        )
        reply = resp.content[0].text if resp.content else "I couldn't understand that."
    except Exception as e:
        log.warning("conversational reply failed: %s", e)
        reply = (
            "I can help with expense claims. Try:\n"
            "• Send a receipt photo/PDF to file a claim\n"
            "• /list to see your claims\n"
            "• Ask me: 'how much did I spend in April?'"
        )

    await msg.reply_text(reply, parse_mode="Markdown")


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo or document arrived. Parse + file as draft."""
    if not await _gate(update):
        return
    msg = update.message
    u = storage.get_user_by_channel("telegram", str(msg.from_user.id))
    if not u:
        await msg.reply_text("Hi! Run /start first.")
        return
    if not u.get("anth_key") and not os.environ.get("MAINTAINER_ANTHROPIC_API_KEY"):
        await msg.reply_text("Set your Anthropic key first: /setkey sk-ant-…")
        return
    if not u.get("access_jwt"):
        await msg.reply_text("Not paired with OmniHR yet — run /pair")
        return

    # Download file + capture Telegram's file_id for instant replay later
    if msg.document:
        tg_file = await msg.document.get_file()
        media_type = msg.document.mime_type or "application/pdf"
        filename = msg.document.file_name or "receipt.pdf"
        tg_file_id = msg.document.file_id
        tg_file_type = "document"
    elif msg.photo:
        tg_file = await msg.photo[-1].get_file()
        media_type = "image/jpeg"
        filename = "receipt.jpg"
        tg_file_id = msg.photo[-1].file_id
        tg_file_type = "photo"
    else:
        return

    file_bytes = bytes(await tg_file.download_as_bytearray())
    user_note = (msg.caption or "").strip()

    if not await _check_rate(update, u["id"], "parse"):
        return
    progress = await msg.reply_text("⏳ Parsing receipt…")

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

        # Parse — try Agent SDK first (subscription auth), fall back to direct API
        parsed = None
        agent_raw = None
        try:
            agent_raw = await parse_receipt_via_agent(
                file_bytes=file_bytes,
                media_type=media_type,
                filename=filename,
                tenant_md=tenant_md,
                user_md=user_md,
                recent_claims_summary=recent_summary,
                active_trip=None,
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
                    anthropic=anthropic_for(u),
                    file_bytes=file_bytes,
                    media_type=media_type,
                    tenant_md=tenant_md,
                    user_md=user_md,
                    recent_claims_summary=recent_summary,
                    active_trip=None,
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

        await progress.edit_text(
            f"📄 {parsed.merchant or '?'} {parsed.currency or '?'} {parsed.amount or '?'} on {parsed.receipt_date or '?'}\n"
            f"Suggested: {parsed.suggested_sub_category_label or '?'} (policy {parsed.suggested_policy_id})\n"
            f"⏳ Filing draft…"
        )

        # Upload PDF to OmniHR
        try:
            doc = await client.upload_document(
                file_bytes=file_bytes,
                name=filename,
                media_type=media_type if media_type.startswith("application/") else "image/jpeg",
            )
            doc_id = doc["id"]
            doc_path = doc["file_path"]
        except Exception as e:
            await progress.edit_text(f"Upload failed: {e}")
            return

        # Build values + create draft
        if not parsed.suggested_policy_id or not parsed.amount or not parsed.receipt_date:
            await progress.edit_text(
                f"Couldn't auto-classify — add a hint like 'travel local' or 'subscription'.\n"
                f"Parsed: {parsed.merchant} {parsed.amount} {parsed.currency} {parsed.receipt_date}"
            )
            return

        try:
            schema = await client.schema(parsed.suggested_policy_id, parsed.receipt_date)
        except Exception as e:
            await progress.edit_text(f"Schema fetch failed: {e}")
            return

        values: dict[str, Any] = {
            "AMOUNT": {"amount": str(parsed.amount), "amount_currency": parsed.currency or "SGD"},
            "MERCHANT": parsed.merchant or "",
            "RECEIPT_DATE": parsed.receipt_date.isoformat(),
            "DESCRIPTION": parsed.description_draft or user_note or "",
        }

        # Fill custom fields from parsed dict (label-keyed) + sub-category
        for label, value in (parsed.custom_fields or {}).items():
            values[label] = value
        if parsed.suggested_sub_category_label:
            for f in schema.custom_fields():
                if f.field_type == "SINGLE_SELECT" and "sub" in f.label.lower():
                    values[f.label] = parsed.suggested_sub_category_label
                    break

        # Default trip dates to receipt_date when not provided (common for local same-day trips)
        for f in schema.custom_fields():
            if f.is_mandatory and f.field_id not in {ff.field_id for ff in schema.custom_fields() if f.label in values}:
                label_low = f.label.lower()
                if "trip start" in label_low or "trip end" in label_low:
                    if f.label not in values:
                        values[f.label] = parsed.receipt_date.isoformat()
                if "destination" in label_low and f.label not in values:
                    values[f.label] = "Singapore"  # default for local trips

        receipts_payload = [{"id": doc_id, "file_path": doc_path}]
        try:
            draft = await client.create_draft(
                policy_id=parsed.suggested_policy_id,
                schema=schema,
                values=values,
                receipts=receipts_payload,
            )
        except SchemaDriftError as e:
            await invalidate_schema(tenant_id=client.tenant_id, policy_id=parsed.suggested_policy_id)
            await progress.edit_text(f"Schema drift on policy {parsed.suggested_policy_id}: {e.field_errors}")
            return
        except ValidationError as e:
            # Show what we parsed + what's missing
            parsed_summary = (
                f"Parsed: {parsed.merchant} {parsed.currency} {parsed.amount} "
                f"on {parsed.receipt_date}, policy {parsed.suggested_policy_id}\n"
                f"Custom fields from Claude: {parsed.custom_fields}\n"
                f"Sub-cat: {parsed.suggested_sub_category_label}\n"
            )
            await progress.edit_text(
                f"Couldn't file — {e}\n\n{parsed_summary}\n"
                f"Values attempted: {list(values.keys())}"
            )
            return
        except Exception as e:
            await progress.edit_text(f"Draft create failed: {e}")
            return

        sub_id = draft["id"]
        storage.insert_receipt(
            u["id"],
            file_sha256=sha,
            parsed=parsed.raw,
            omnihr_doc_id=doc_id,
            omnihr_submission_id=sub_id,
            omnihr_file_path=doc_path,
            omnihr_file_name=filename,
            omnihr_file_mime=media_type,
            tg_file_id=tg_file_id,
            tg_file_type=tg_file_type,
            status=draft.get("status", 3),
        )
        kb = _claim_buttons({"id": sub_id, "status": STATUS_DRAFT})
        caption = (
            f"✅ Drafted *#{sub_id}*\n"
            f"{parsed.merchant} {parsed.currency} {parsed.amount} · {parsed.receipt_date}\n"
            f"{parsed.suggested_sub_category_label or '?'}"
        )
        # Replace the progress message with a preview-attached one
        try:
            await progress.delete()
        except Exception:
            pass
        import io
        buf = io.BytesIO(file_bytes)
        buf.name = filename
        if media_type.startswith("image/"):
            await msg.reply_photo(photo=buf, caption=caption, parse_mode="Markdown", reply_markup=kb)
        else:
            # PDFs preview as a file card with the first page rendered by Telegram
            await msg.reply_document(document=buf, caption=caption, parse_mode="Markdown", reply_markup=kb)


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

    from fastapi.responses import HTMLResponse

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return legal.html_page(
            "expensebot",
            "# ExpenseBot\n\n"
            "Files OmniHR expense claims from a Telegram bot.\n\n"
            "- [GitHub](https://github.com/seahyc/expensebot)\n"
            "- [Terms](/terms)  ·  [Privacy](/privacy)\n",
        )

    @app.get("/auth/callback")
    async def auth_callback(code: str = "", state: str = "") -> HTMLResponse:
        """OAuth callback — Claude redirects here after user authorizes."""
        if not code or not state:
            return HTMLResponse("<h1>Missing code or state</h1><p>Try /login again.</p>", status_code=400)

        ok, msg, token_data = await claude_oauth.complete_login(state, code)

        if ok and token_data:
            # Store the subscription token
            user_db_id = token_data["user_db_id"]
            access = token_data["access_token"]
            storage.set_anth_key(user_db_id, access)

            # DM the user via Telegram
            if tg_app:
                tid = token_data["telegram_user_id"]
                try:
                    await tg_app.bot.send_message(
                        chat_id=tid,
                        text=(
                            "✅ Claude subscription linked!\n"
                            "Your receipts will be parsed using your Claude plan — no API key needed.\n"
                            "Send a receipt to test."
                        ),
                    )
                except Exception as e:
                    log.warning("couldn't DM after login: %s", e)

            return HTMLResponse(
                "<html><body style='background:#1a1a2e;color:#eee;display:flex;"
                "align-items:center;justify-content:center;height:100vh;"
                "font-family:sans-serif'>"
                "<div style='text-align:center'>"
                "<h1>✅ Logged in!</h1>"
                "<p>Go back to Telegram — your bot is ready.</p>"
                "</div></body></html>"
            )
        else:
            return HTMLResponse(
                f"<html><body style='background:#1a1a2e;color:#eee;display:flex;"
                f"align-items:center;justify-content:center;height:100vh;"
                f"font-family:sans-serif'>"
                f"<div style='text-align:center'>"
                f"<h1>❌ Login failed</h1>"
                f"<p>{msg}</p>"
                f"<p>Go back to Telegram and try /login again.</p>"
                f"</div></body></html>",
                status_code=400,
            )

    @app.get("/terms", response_class=HTMLResponse)
    async def terms() -> str:
        return legal.html_page("expensebot — Terms", legal.TERMS_MD)

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> str:
        return legal.html_page("expensebot — Privacy", legal.PRIVACY_MD)

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

        # DM the user via Telegram
        if tg_app:
            user = storage.get_user(user_db_id)
            try:
                await tg_app.bot.send_message(
                    chat_id=int(user["channel_user_id"]),
                    text=(
                        f"✅ Paired as {me.get('full_name','?')} "
                        f"({tenant_id}, employee #{me.get('id')}).\n"
                        f"Send any receipt photo or PDF to file your first claim."
                    ),
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
    tg_app.add_handler(CommandHandler("export_me", cmd_export))
    tg_app.add_handler(CommandHandler("delete_account", cmd_delete_account))
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

    # Run uvicorn + telegram polling in parallel
    await tg_app.initialize()
    await tg_app.start()
    polling_task = asyncio.create_task(
        tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    )

    try:
        await server.serve()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        polling_task.cancel()


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
