"""Telegram handlers — channel adapter that wraps bot/common pipeline.

Stubs for now. Each handler:
  1. Authenticates the user (DB lookup by chat_id).
  2. Calls into bot/common.
  3. Formats response with Telegram-specific markup.

Commands implemented in v1:
  /start    /setkey    /pair    /list    /status <id>    /submit <id>
  /trip <name>    /endtrip    /portal <subdomain>    /orgconfig
  /upgrade   /myrules   /export
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def cmd_start(update, context):
    """First contact — onboarding flow."""
    raise NotImplementedError("v1: send onboarding message + setkey + pair instructions")


async def cmd_setkey(update, context):
    """Store user's Anthropic API key (BYOK tier)."""
    raise NotImplementedError("v1: validate via tiny test call, encrypt + store")


async def cmd_pair(update, context):
    """Issue 6-digit pairing code, expires in 5 min."""
    raise NotImplementedError("v1: gen code, store with 5-min TTL in Redis, return code")


async def on_document_or_photo(update, context):
    """Receipt arrived — file_bytes → bot.common.pipeline.file_receipt → reply."""
    raise NotImplementedError("v1: download file, call pipeline, format compact reply with edit shortcuts")


async def cmd_list(update, context):
    """Show last 10 across all statuses, with filters."""
    raise NotImplementedError("v1: query OmniHR submissions, format table")


async def cmd_status(update, context):
    """/status <draft_id> — fetch live OmniHR status."""
    raise NotImplementedError("v1: omnihr.list_submissions filter by id, format")


async def cmd_submit(update, context):
    """/submit <draft_id> — quick-action submit."""
    raise NotImplementedError("v1: omnihr.submit_draft + DM result")


async def cmd_trip(update, context):
    """/trip <name> — start a trip; further receipts inherit destination + dates."""
    raise NotImplementedError("v2: create trip in db, prompt for dest + dates")


async def cmd_orgconfig(update, context):
    """View / edit tenant.md (shepherd only)."""
    raise NotImplementedError("v2: surface tenants/<org>.md, allow edit")


async def cmd_upgrade(update, context):
    """Stripe checkout link for managed tier."""
    raise NotImplementedError("v2: stripe checkout session, return URL")
