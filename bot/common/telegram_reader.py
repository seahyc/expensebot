"""Telethon-based reader for the user's personal Telegram messages.

Provides phone-code auth (start_phone_auth / verify_phone_code) and
a fast-fetch helper (fetch_recent_messages) used by the boss-profile
builder to pull expense-relevant snippets from the user's own chats.

Requires TELEGRAM_API_ID and TELEGRAM_API_HASH env vars from
https://my.telegram.org (App API section).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Credentials from https://my.telegram.org — must be set in environment.
TG_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# Default expense-related keywords to filter messages.
_DEFAULT_KEYWORDS = [
    "receipt", "invoice", "booking", "hotel", "flight",
    "grab", "gojek", "expense", "reimbursement", "claim",
    "order", "payment", "paid", "transfer",
]

# In-memory pending auth state: user_id -> TelegramClient (mid-auth)
_pending: dict[int, Any] = {}


def _make_client(session_str: str = "") -> Any:
    """Create a TelegramClient. Import is deferred so missing telethon doesn't
    crash the whole bot on startup — only callers that actually use Telegram
    integration need it installed."""
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise RuntimeError(
            "telethon is not installed. Add it to pyproject.toml dependencies."
        ) from exc

    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in environment. "
            "Get them from https://my.telegram.org"
        )

    return TelegramClient(StringSession(session_str), TG_API_ID, TG_API_HASH)


async def start_phone_auth(user_id: int, phone: str, session_str: str = "") -> bool:
    """Send a login code to `phone`.

    Stores the in-progress TelegramClient in `_pending[user_id]` so
    verify_phone_code can complete the handshake.

    Returns True on success, raises on failure.
    """
    # Clean up any prior pending session for this user.
    if user_id in _pending:
        try:
            await _pending[user_id].disconnect()
        except Exception:
            pass
        del _pending[user_id]

    client = _make_client(session_str)
    try:
        await client.connect()
        await client.send_code_request(phone)
        _pending[user_id] = client
        log.info("telegram_reader: code sent to %s for user=%s", phone, user_id)
        return True
    except Exception as e:
        log.warning("telegram_reader start_phone_auth error for user=%s: %s", user_id, e)
        try:
            await client.disconnect()
        except Exception:
            pass
        raise


async def verify_phone_code(user_id: int, code: str) -> str | None:
    """Verify the OTP code for user_id.

    Returns the StringSession string on success, or None if there is no
    pending auth for this user. Cleans up `_pending[user_id]` regardless.
    """
    client = _pending.get(user_id)
    if not client:
        log.warning("telegram_reader verify_phone_code: no pending auth for user=%s", user_id)
        return None

    try:
        from telethon.sessions import StringSession

        await client.sign_in(code=code)
        session_str = client.session.save()
        log.info("telegram_reader: signed in successfully for user=%s", user_id)
        return session_str
    except Exception as e:
        log.warning("telegram_reader verify_phone_code error for user=%s: %s", user_id, e)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        _pending.pop(user_id, None)


async def fetch_recent_messages(
    session_str: str,
    since: datetime,
    keywords: list[str] | None = None,
    max_dialogs: int = 20,
    max_per_dialog: int = 100,
    max_results: int = 60,
) -> list[str]:
    """Connect with an existing StringSession, fetch recent messages, then disconnect.

    Args:
        session_str: A saved Telethon StringSession string.
        since: Earliest datetime for messages (timezone-aware UTC recommended).
        keywords: Filter messages by keywords. None = expense keywords. [] = no filter (all msgs).
        max_dialogs: How many dialogs to scan.
        max_per_dialog: Max messages to read per dialog.
        max_results: Total result cap.

    Returns:
        Up to max_results text snippets like "from {name}: {text[:120]}".
    """
    kws = keywords if keywords is not None else [k.lower() for k in _DEFAULT_KEYWORDS]
    kws = [k.lower() for k in kws]

    client = _make_client(session_str)
    results: list[str] = []

    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        await client.connect()

        dialogs = await client.get_dialogs(limit=max_dialogs)
        for dialog in dialogs:
            if len(results) >= max_results:
                break
            try:
                async for msg in client.iter_messages(dialog.entity, limit=max_per_dialog, offset_date=None):
                    if msg.date and msg.date.replace(tzinfo=timezone.utc) < since:
                        break

                    text = msg.text or ""
                    if not text:
                        continue

                    if kws and not any(kw in text.lower() for kw in kws):
                        continue

                    try:
                        name = dialog.name or "unknown"
                    except Exception:
                        name = "unknown"

                    results.append(f"from {name}: {text[:120]}")
                    if len(results) >= max_results:
                        break
            except Exception as e:
                log.debug("telegram_reader: error reading dialog %s: %s", dialog.name, e)
                continue

    except Exception as e:
        log.warning("telegram_reader fetch_recent_messages error: %s", e)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    log.info("telegram_reader: fetched %d snippets for since=%s", len(results), since.date())
    return results


async def list_chats(
    session_str: str,
    since: datetime | None = None,
    max_dialogs: int = 30,
    skip_bots: bool = True,
) -> list[dict]:
    """Return recent chats sorted by last activity (same order as Telegram sidebar).

    Each entry: {name, type, last_message, last_date}
    `since` is ignored — all top-N dialogs are returned regardless of read status.
    """
    client = _make_client(session_str)
    results: list[dict] = []
    try:
        await client.connect()
        # get_dialogs returns chats sorted by most recent activity — no filtering needed
        dialogs = await client.get_dialogs(limit=max_dialogs)
        for dialog in dialogs:
            try:
                entity = dialog.entity
                if skip_bots and getattr(entity, "bot", False):
                    continue
                chat_type = (
                    "group" if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                    else "channel" if getattr(entity, "broadcast", False)
                    else "dm"
                )
                # dialog.message is the most recent message — no extra API call needed
                last_text = ""
                last_date = ""
                if dialog.message and getattr(dialog.message, "text", None):
                    last_text = dialog.message.text[:100]
                    if dialog.message.date:
                        last_date = dialog.message.date.strftime("%m-%d %H:%M")
                results.append({
                    "name": dialog.name or "unknown",
                    "type": chat_type,
                    "last_message": last_text,
                    "last_date": last_date,
                })
            except Exception:
                continue
    except Exception as e:
        log.warning("telegram_reader list_chats error: %s", e)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return results


async def fetch_chat_messages(
    session_str: str,
    contact: str,
    since: datetime,
    max_messages: int = 50,
) -> list[str]:
    """Fetch messages from a specific chat matching `contact` (case-insensitive partial match).

    Returns list of "from {sender}: {text}" strings, or raises if not found.
    """
    client = _make_client(session_str)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    contact_lower = contact.lower()
    results: list[str] = []

    try:
        await client.connect()
        dialogs = await client.get_dialogs(limit=100)

        # Find best matching dialog
        matched = None
        for dialog in dialogs:
            name = (dialog.name or "").lower()
            if contact_lower in name:
                matched = dialog
                break

        if not matched:
            return [f"No chat found matching '{contact}'. Use list_telegram_chats to see available chats."]

        async for msg in client.iter_messages(matched.entity, limit=max_messages):
            if msg.date and msg.date.replace(tzinfo=timezone.utc) < since:
                break
            if not msg.text:
                continue
            # Resolve sender name
            try:
                sender = await msg.get_sender()
                sender_name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "unknown"
            except Exception:
                sender_name = matched.name or "unknown"
            results.append(f"[{msg.date.strftime('%Y-%m-%d %H:%M')}] {sender_name}: {msg.text[:200]}")

    except Exception as e:
        log.warning("telegram_reader fetch_chat_messages error: %s", e)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    results.reverse()  # oldest first
    log.info("telegram_reader: fetched %d msgs from chat '%s'", len(results), contact)
    return results
