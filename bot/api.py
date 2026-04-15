"""FastAPI app — webhook receivers + extension pairing endpoint."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

log = logging.getLogger(__name__)

app = FastAPI(title="expensebot")


# --- Telegram webhook ---

@app.post("/webhook/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict[str, str]:
    # Validate path-secret matches TELEGRAM_WEBHOOK_SECRET
    raise NotImplementedError("v1: dispatch to bot.telegram.handlers")


# --- Lark webhook ---

@app.post("/webhook/lark")
async def lark_webhook(request: Request) -> dict[str, Any]:
    raise NotImplementedError("v3: handle Lark URL verification + event dispatch")


# --- Email inbound (Postmark / SES / etc.) ---

@app.post("/webhook/email")
async def email_webhook(request: Request) -> dict[str, Any]:
    """Inbound email → user identified by To: alias → file as draft."""
    raise NotImplementedError("v2: parse email, identify user, run pipeline with auto_file=true (DRAFT mode default)")


# --- Stripe webhook ---

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    raise NotImplementedError("v2: validate signature, flip user tier on subscription events")


# --- Extension pairing ---

@app.post("/extension/pair")
async def extension_pair(payload: dict[str, Any]) -> dict[str, str]:
    """Called by the Chrome extension after user pastes pairing code.

    Body: {pairing_code, access_token, refresh_token, employee_id, org}
    Looks up pairing_code in Redis (5-min TTL, mapped to channel_user_id),
    persists tokens encrypted, DMs the user via the channel adapter.
    """
    raise NotImplementedError("v1: validate code, decrypt+store tokens, DM user")


# --- Health ---

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Helpful for local dev — generate pairing codes for testing
def gen_pairing_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"
