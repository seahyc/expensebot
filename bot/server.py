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
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from omnihr_client.auth import Tokens, parse_jwt_exp, refresh_access_token
from omnihr_client.client import OmniHRClient
from omnihr_client.exceptions import AuthError, SchemaDriftError, ValidationError
from omnihr_client.schema import invalidate_schema

from . import access, legal, logging_setup, rate_limit, storage
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


def anthropic_for(user: dict) -> AsyncAnthropic:
    key = storage.get_anth_key(user["id"]) or os.environ.get("MAINTAINER_ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic key — run /setkey sk-ant-…")
    return AsyncAnthropic(api_key=key)


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
    await update.message.reply_text(
        f"Pairing code: `{code}`\n\n"
        f"Open the extension on omnihr.co (after signing in normally), "
        f"paste this code, click Pair. Expires in 5 min.",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    u = storage.get_user_by_channel("telegram", str(update.effective_user.id))
    if not u or not u.get("access_jwt"):
        await update.message.reply_text("Not paired yet — run /pair")
        return
    if not await _check_rate(update, u["id"], "list"):
        return
    async with client_for(u) as client:
        try:
            data = await client.list_submissions(page_size=10)
        except AuthError:
            await update.message.reply_text("Session expired — run /pair to re-link.")
            return
    rows = data.get("results", [])
    if not rows:
        await update.message.reply_text("No claims found.")
        return
    lines = [f"Last {len(rows)} claims:"]
    for r in rows:
        status_label = {3: "DRAFT", 1: "ACT", 2: "ACT", 5: "ACT"}.get(r.get("status", 0), str(r.get("status")))
        lines.append(
            f"• #{r['id']} {r.get('receipt_date','?')} "
            f"{r.get('amount_currency','?')} {r.get('amount','?')}"
            f" — {(r.get('description') or '')[:40]} [{status_label}]"
        )
    await update.message.reply_text("\n".join(lines))


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

    # Download file
    if msg.document:
        tg_file = await msg.document.get_file()
        media_type = msg.document.mime_type or "application/pdf"
        filename = msg.document.file_name or "receipt.pdf"
    elif msg.photo:
        tg_file = await msg.photo[-1].get_file()
        media_type = "image/jpeg"
        filename = "receipt.jpg"
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

        # Parse
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
            # Find which custom field is the SINGLE_SELECT for sub-category
            for f in schema.custom_fields():
                if f.field_type == "SINGLE_SELECT" and "sub" in f.label.lower():
                    values[f.label] = parsed.suggested_sub_category_label
                    break

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
            await progress.edit_text(
                f"Couldn't file — missing required fields:\n{e.field_errors}\n"
                f"Reply with the values and I'll retry."
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
            status=draft.get("status", 3),
        )
        await progress.edit_text(
            f"✅ Drafted #{sub_id}\n"
            f"{parsed.merchant} {parsed.currency} {parsed.amount} · {parsed.receipt_date}\n"
            f"{parsed.suggested_sub_category_label or '?'}\n\n"
            f"/submit {sub_id}  ·  /delete {sub_id}  ·  /list"
        )


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
        user_email = me.get("primary_email")
        ok, reason = access.email_allowed(user_email)
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
            email=me.get("primary_email"),
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
    tg_app.add_handler(CommandHandler("whoami", cmd_whoami))
    tg_app.add_handler(CommandHandler("setkey", cmd_setkey))
    tg_app.add_handler(CommandHandler("pair", cmd_pair))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("delete", cmd_delete))
    tg_app.add_handler(CommandHandler("submit", cmd_submit))
    tg_app.add_handler(CommandHandler("export_me", cmd_export))
    tg_app.add_handler(CommandHandler("delete_account", cmd_delete_account))
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_file))

    app = make_app(tg_app)

    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
    server = uvicorn.Server(config)

    # Run uvicorn + telegram polling in parallel
    await tg_app.initialize()
    await tg_app.start()
    polling_task = asyncio.create_task(tg_app.updater.start_polling())

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
