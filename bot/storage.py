"""Tiny SQLite storage layer for the local-test / single-host deployment.

Secrets (anth_key, refresh_jwt, access_jwt) are encrypted via bot.crypto before
touching the DB. Decrypt only at use time. Never log raw values.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import crypto
from .voice import memory_template

DB_PATH = Path(__file__).parent.parent / "expensebot.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  channel_user_id TEXT NOT NULL,
  tenant_id TEXT,
  omnihr_employee_id INTEGER,
  omnihr_full_name TEXT,
  omnihr_email TEXT,
  anth_key TEXT,                  -- TODO encrypt
  refresh_jwt TEXT,               -- TODO encrypt
  access_jwt TEXT,                -- TODO encrypt
  access_expires_at TEXT,
  refresh_expires_at TEXT,
  tier TEXT NOT NULL DEFAULT 'byok',
  user_md TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (channel, channel_user_id)
);

CREATE TABLE IF NOT EXISTS pairing_codes (
  code TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  file_sha256 TEXT NOT NULL,
  parsed_json TEXT NOT NULL,
  parsed_merchant TEXT,
  parsed_date TEXT,
  parsed_amount TEXT,
  parsed_currency TEXT,
  omnihr_doc_id INTEGER,
  omnihr_submission_id INTEGER,
  omnihr_file_path TEXT,      -- signed S3 URL from /document/ response, valid ~7d
  omnihr_file_name TEXT,
  omnihr_file_mime TEXT,
  status INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One-off migrations for existing installs (idempotent)
CREATE TABLE IF NOT EXISTS __migrations (name TEXT PRIMARY KEY);

CREATE INDEX IF NOT EXISTS receipts_sha_idx ON receipts(user_id, file_sha256);
CREATE INDEX IF NOT EXISTS receipts_sub_idx ON receipts(user_id, omnihr_submission_id);

CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  destination TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS nudges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  hook TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  message_preview TEXT
);
CREATE INDEX IF NOT EXISTS nudges_user_sent ON nudges(user_id, sent_at);
CREATE INDEX IF NOT EXISTS nudges_user_hook_sent ON nudges(user_id, hook, sent_at);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
  body TEXT,
  has_file INTEGER NOT NULL DEFAULT 0,
  file_type TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS messages_user_created ON messages(user_id, created_at);

CREATE TABLE IF NOT EXISTS merchant_choices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  merchant_normalized TEXT NOT NULL,
  merchant_display TEXT NOT NULL,
  policy_id TEXT NOT NULL,
  sub_category TEXT,
  count INTEGER NOT NULL DEFAULT 1,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, merchant_normalized, policy_id, sub_category)
);
CREATE INDEX IF NOT EXISTS merchant_choices_user ON merchant_choices(user_id, count DESC);

CREATE TABLE IF NOT EXISTS google_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    access_token TEXT,
    refresh_token TEXT,
    token_expiry TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, email)
);

CREATE TABLE IF NOT EXISTS telegram_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    session_str TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, phone)
);

CREATE TABLE IF NOT EXISTS whatsapp_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    phone TEXT,
    session_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, session_id)
);
"""


_ADD_COLS = [
    ("receipts", "omnihr_file_path", "TEXT"),
    ("receipts", "omnihr_file_name", "TEXT"),
    ("receipts", "omnihr_file_mime", "TEXT"),
    ("receipts", "tg_file_id", "TEXT"),
    ("receipts", "tg_file_type", "TEXT"),  # "photo" or "document"
    ("users", "session_expired_notified_at", "TEXT"),
    ("users", "anth_refresh_token", "TEXT"),   # encrypted; for Claude OAuth
    ("users", "anth_expires_at", "TEXT"),      # ISO UTC; for Claude OAuth
    ("users", "last_inbound_at", "TEXT"),      # ISO UTC; bumped on any inbound msg
    ("users", "profile_md", "TEXT"),
    ("users", "submit_count", "INTEGER DEFAULT 0"),
    ("users", "google_access_token", "TEXT"),  # encrypted; Google OAuth
    ("users", "google_refresh_token", "TEXT"), # encrypted; Google OAuth
    ("users", "google_token_expiry", "TEXT"),  # ISO UTC
    ("users", "google_email", "TEXT"),         # connected Google account email
    ("users", "boss_profile_md", "TEXT"),      # secretary's briefing — built from claims+gmail+gcal
    ("users", "boss_profile_updated_at", "TEXT"),  # ISO UTC; last full rebuild
    ("users", "telegram_session", "TEXT"),     # encrypted Telethon StringSession
    ("users", "telegram_phone", "TEXT"),       # E.164 phone for display
    ("users", "whatsapp_phone", "TEXT"),       # E.164 for display
    ("users", "whatsapp_connected", "INTEGER DEFAULT 0"),
    ("users", "ext_session", "TEXT"),          # UUID token for extension status API
]


def init_db(path: Path | None = None) -> None:
    # Resolve DB_PATH lazily (not as a default-arg value frozen at import)
    # so tests can monkeypatch storage.DB_PATH to redirect writes.
    target = path if path is not None else DB_PATH
    with sqlite3.connect(target) as conn:
        conn.executescript(SCHEMA)
        # idempotent column adds for already-created DBs
        for table, col, typ in _ADD_COLS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass


@contextmanager
def db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    # Resolve DB_PATH lazily (not as a default-arg value frozen at import)
    # so tests can monkeypatch storage.DB_PATH to redirect writes.
    conn = sqlite3.connect(path if path is not None else DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- Users ---

def upsert_user(channel: str, channel_user_id: str) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE channel=? AND channel_user_id=?",
            (channel, str(channel_user_id)),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (channel, channel_user_id) VALUES (?, ?)",
            (channel, str(channel_user_id)),
        )
        return cur.lastrowid


def get_user(user_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_channel(channel: str, channel_user_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE channel=? AND channel_user_id=?",
            (channel, str(channel_user_id)),
        ).fetchone()
        return dict(row) if row else None


def set_anth_key(user_id: int, key: str) -> None:
    """Store a bare credential (API key or OAuth access token) without
    refresh/expiry metadata. Used by /setkey and the legacy OAuth path."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET anth_key=?, anth_refresh_token=NULL, anth_expires_at=NULL WHERE id=?",
            (crypto.encrypt(key), user_id),
        )


def set_anth_oauth(
    user_id: int,
    *,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
) -> None:
    """Store a Claude-subscription OAuth credential set so we can
    auto-refresh when the access token nears expiry."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET anth_key=?, anth_refresh_token=?, anth_expires_at=? WHERE id=?",
            (
                crypto.encrypt(access_token),
                crypto.encrypt(refresh_token) if refresh_token else None,
                expires_at.isoformat() if expires_at else None,
                user_id,
            ),
        )


def get_anth_oauth(user_id: int) -> tuple[str | None, str | None, datetime | None]:
    """Return (access_token, refresh_token, expires_at) for Claude OAuth.
    All fields may be None if the user hasn't logged in via OAuth or the
    flow didn't return a refresh token."""
    with db() as conn:
        row = conn.execute(
            "SELECT anth_key, anth_refresh_token, anth_expires_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return None, None, None
        access = crypto.decrypt(row["anth_key"]) if row["anth_key"] else None
        refresh = crypto.decrypt(row["anth_refresh_token"]) if row["anth_refresh_token"] else None
        exp = datetime.fromisoformat(row["anth_expires_at"]) if row["anth_expires_at"] else None
        return access, refresh, exp


def set_google_tokens(
    user_id: int,
    *,
    access_token: str,
    refresh_token: str | None,
    expiry: datetime | None,
    email: str | None,
) -> None:
    with db() as conn:
        conn.execute(
            """UPDATE users SET
               google_access_token=?, google_refresh_token=?,
               google_token_expiry=?, google_email=?
               WHERE id=?""",
            (
                crypto.encrypt(access_token),
                crypto.encrypt(refresh_token) if refresh_token else None,
                expiry.isoformat() if expiry else None,
                email,
                user_id,
            ),
        )


def get_google_tokens(user_id: int) -> tuple[str | None, str | None, datetime | None, str | None]:
    """Return (access_token, refresh_token, expiry, email). All may be None."""
    with db() as conn:
        row = conn.execute(
            "SELECT google_access_token, google_refresh_token, google_token_expiry, google_email "
            "FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return None, None, None, None
        access = crypto.decrypt(row["google_access_token"]) if row["google_access_token"] else None
        refresh = crypto.decrypt(row["google_refresh_token"]) if row["google_refresh_token"] else None
        expiry = datetime.fromisoformat(row["google_token_expiry"]) if row["google_token_expiry"] else None
        return access, refresh, expiry, row["google_email"]


# Memory structure borrowed from Claude Code's auto-memory system:
#   - Categorical sections (like Claude Code's user/feedback/project/reference types)
#   - Each entry is ONE line: **Bold rule** — short reason (date)
#   - Stale entries can be updated in place by date
#   - "Don't ask" section is the escape hatch for one-offs
# Why this shape (vs free-form markdown): predictable slots for the agent to
# append to, and human-skimmable when the user hits /memories.
DEFAULT_MEMORY_TEMPLATE = memory_template()


def set_user_md(user_id: int, markdown: str) -> None:
    """Persist the user's learned rules/preferences markdown."""
    with db() as conn:
        conn.execute("UPDATE users SET user_md=? WHERE id=?", (markdown, user_id))


def get_user_md(user_id: int) -> str:
    """Return the stored memory verbatim — may be empty for a fresh user."""
    with db() as conn:
        row = conn.execute("SELECT user_md FROM users WHERE id=?", (user_id,)).fetchone()
        return (row["user_md"] if row else "") or ""


def get_user_md_or_template(user_id: int) -> str:
    """Return stored memory, falling back to the empty-template scaffold.
    Used by the agent + /memories so the user always sees a structured file."""
    stored = get_user_md(user_id)
    return stored if stored.strip() else DEFAULT_MEMORY_TEMPLATE


def get_profile_md(user_id: int) -> str:
    """Return the always-in-context 'who is this user' markdown block.
    Empty string for fresh users — the agent fills it as she learns."""
    with db() as conn:
        row = conn.execute("SELECT profile_md FROM users WHERE id=?", (user_id,)).fetchone()
        return (row["profile_md"] if row else "") or ""


def set_profile_md(user_id: int, markdown: str) -> None:
    """Persist the core-memory profile block — called from update_profile tool."""
    with db() as conn:
        conn.execute("UPDATE users SET profile_md=? WHERE id=?", (markdown, user_id))


def get_boss_profile_md(user_id: int) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT boss_profile_md FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return (row["boss_profile_md"] if row else "") or ""


def set_boss_profile_md(user_id: int, markdown: str) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE users SET boss_profile_md=?, boss_profile_updated_at=? WHERE id=?",
            (markdown, now, user_id),
        )


def get_boss_profile_updated_at(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT boss_profile_updated_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return (row["boss_profile_updated_at"] if row else None)


# --- Submit count (for self-learning harness) ---

def increment_submit_count(user_id: int) -> None:
    """Increment submit_count for this user."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET submit_count = submit_count + 1 WHERE id = ?",
            (user_id,),
        )


def get_submit_count(user_id: int) -> int:
    """Return current submit_count for this user (0 if not found)."""
    with db() as conn:
        row = conn.execute(
            "SELECT submit_count FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["submit_count"] if row else 0


def get_anth_key(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT anth_key FROM users WHERE id=?", (user_id,)).fetchone()
        return crypto.decrypt(row["anth_key"]) if row and row["anth_key"] else None


def get_omnihr_tokens(user_id: int) -> tuple[str | None, str | None]:
    """Returns (access_jwt, refresh_jwt), decrypted."""
    with db() as conn:
        row = conn.execute(
            "SELECT access_jwt, refresh_jwt FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            return None, None
        return crypto.decrypt(row["access_jwt"]), crypto.decrypt(row["refresh_jwt"])


def set_omnihr_tokens(
    user_id: int,
    *,
    access_jwt: str,
    refresh_jwt: str,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
) -> None:
    """Persist refreshed JWTs. Narrower than set_omnihr_session — used by the
    in-request refresh path and the 6h sweeper, which don't touch identity."""
    with db() as conn:
        conn.execute(
            """UPDATE users SET
               access_jwt=?, refresh_jwt=?,
               access_expires_at=?, refresh_expires_at=?
               WHERE id=?""",
            (
                crypto.encrypt(access_jwt),
                crypto.encrypt(refresh_jwt),
                access_expires_at.isoformat(),
                refresh_expires_at.isoformat(),
                user_id,
            ),
        )


def users_with_expired_session(*, renotify_after: timedelta) -> list[dict]:
    """Users whose refresh token has already expired and who haven't been told
    in the last `renotify_after`. Used by the sweeper to DM a one-shot
    'session expired, run /pair' prompt.
    """
    now = datetime.now(timezone.utc)
    cutoff_notified = (now - renotify_after).isoformat()
    now_iso = now.isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT id, channel, channel_user_id, session_expired_notified_at
               FROM users
               WHERE refresh_jwt IS NOT NULL
                 AND refresh_expires_at <= ?
                 AND (session_expired_notified_at IS NULL
                      OR session_expired_notified_at < ?)""",
            (now_iso, cutoff_notified),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_refresh_dead(user_id: int) -> None:
    """Mark the refresh JWT as no longer usable — called when OmniHR rejected
    a refresh attempt. Sets refresh_expires_at to now so the next expired-user
    query picks the user up for notification.
    """
    with db() as conn:
        conn.execute(
            "UPDATE users SET refresh_expires_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


def mark_session_expired_notified(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET session_expired_notified_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


def users_needing_refresh(*, within: timedelta) -> list[dict]:
    """Users whose access JWT expires within `within` AND whose refresh JWT is
    still live. Returned rows are decryption-free dicts — tokens are fetched
    separately via get_omnihr_tokens to keep the decrypt surface small.
    """
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) + within).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT id, access_expires_at, refresh_expires_at
               FROM users
               WHERE access_jwt IS NOT NULL
                 AND refresh_jwt IS NOT NULL
                 AND access_expires_at < ?
                 AND refresh_expires_at > ?""",
            (cutoff, now),
        ).fetchall()
        return [dict(r) for r in rows]


def set_omnihr_session(
    user_id: int,
    *,
    access_jwt: str,
    refresh_jwt: str,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
    employee_id: int,
    full_name: str | None,
    email: str | None,
    tenant_id: str | None,
) -> None:
    with db() as conn:
        conn.execute(
            """UPDATE users SET
               access_jwt=?, refresh_jwt=?,
               access_expires_at=?, refresh_expires_at=?,
               omnihr_employee_id=?, omnihr_full_name=?, omnihr_email=?, tenant_id=?,
               session_expired_notified_at=NULL
               WHERE id=?""",
            (
                crypto.encrypt(access_jwt),
                crypto.encrypt(refresh_jwt),
                access_expires_at.isoformat(),
                refresh_expires_at.isoformat(),
                employee_id,
                full_name,
                email,
                tenant_id,
                user_id,
            ),
        )


def export_user_data(user_id: int) -> dict:
    """GDPR-ish export. Decrypts nothing — secrets are never returned."""
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            return {}
        rec = conn.execute(
            "SELECT id, file_sha256, parsed_merchant, parsed_date, parsed_amount, "
            "parsed_currency, omnihr_doc_id, omnihr_submission_id, status, created_at "
            "FROM receipts WHERE user_id=?",
            (user_id,),
        ).fetchall()
        trips = conn.execute(
            "SELECT id, name, destination, start_date, end_date, active, created_at "
            "FROM trips WHERE user_id=?",
            (user_id,),
        ).fetchall()
    out = {
        "user": {
            k: v
            for k, v in dict(u).items()
            if k not in ("anth_key", "refresh_jwt", "access_jwt")
        },
        "receipts": [dict(r) for r in rec],
        "trips": [dict(t) for t in trips],
        "secrets_note": "API key + OmniHR tokens are encrypted at rest and never exported.",
    }
    return out


def delete_user(user_id: int) -> None:
    """Purge all rows for this user."""
    with db() as conn:
        conn.execute("DELETE FROM receipts WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM trips WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM pairing_codes WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


# --- Pairing codes ---

def create_pairing_code(user_id: int, code: str, ttl_seconds: int = 300) -> None:
    with db() as conn:
        # purge expired
        conn.execute("DELETE FROM pairing_codes WHERE expires_at < datetime('now')")
        # purge any prior code for this user
        conn.execute("DELETE FROM pairing_codes WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT INTO pairing_codes (code, user_id, expires_at) "
            "VALUES (?, ?, datetime('now', ?))",
            (code, user_id, f"+{ttl_seconds} seconds"),
        )


def consume_pairing_code(code: str) -> int | None:
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM pairing_codes WHERE code=? AND expires_at >= datetime('now')",
            (code,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pairing_codes WHERE code=?", (code,))
        return row["user_id"]


# --- Receipts ---

def find_receipt_by_sha(user_id: int, sha: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM receipts WHERE user_id=? AND file_sha256=? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, sha),
        ).fetchone()
        return dict(row) if row else None


def insert_receipt(
    user_id: int,
    *,
    file_sha256: str,
    parsed: dict,
    omnihr_doc_id: int | None = None,
    omnihr_submission_id: int | None = None,
    omnihr_file_path: str | None = None,
    omnihr_file_name: str | None = None,
    omnihr_file_mime: str | None = None,
    tg_file_id: str | None = None,
    tg_file_type: str | None = None,
    status: int | None = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO receipts (user_id, file_sha256, parsed_json,
               parsed_merchant, parsed_date, parsed_amount, parsed_currency,
               omnihr_doc_id, omnihr_submission_id,
               omnihr_file_path, omnihr_file_name, omnihr_file_mime,
               tg_file_id, tg_file_type, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                file_sha256,
                json.dumps(parsed, default=str),
                parsed.get("merchant"),
                parsed.get("receipt_date"),
                parsed.get("amount"),
                parsed.get("currency"),
                omnihr_doc_id,
                omnihr_submission_id,
                omnihr_file_path,
                omnihr_file_name,
                omnihr_file_mime,
                tg_file_id,
                tg_file_type,
                status,
            ),
        )
        return cur.lastrowid


def find_receipt_by_submission(user_id: int, sub_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM receipts WHERE user_id=? AND omnihr_submission_id=? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, sub_id),
        ).fetchone()
        return dict(row) if row else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Nudges (proactive outbound messages) ---

def bump_last_inbound_at(user_id: int) -> None:
    """Record that the user just said something to us. Sweeper uses this to
    avoid nudging right after a real conversation."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_inbound_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


def users_eligible_for_nudges() -> list[dict]:
    """Paired telegram users with a live-ish refresh token. We skip users who
    never paired (nothing to nudge about) and users whose session is dead
    (refresh_sweeper owns that channel)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM users
               WHERE channel='telegram'
                 AND omnihr_employee_id IS NOT NULL
                 AND refresh_expires_at > ?""",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def aging_drafts_for_user(user_id: int, older_than_days: int) -> list[dict]:
    """Drafts (status=3) older than N days for one user, oldest first.
    Uses SQLite's datetime() to normalize the compare — receipts.created_at
    is CURRENT_TIMESTAMP (space-separated, no offset) but our cutoff comes
    from Python as an isoformat ('T' + offset). Without datetime() the
    lexicographic compare silently returns wrong results."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT id, parsed_merchant, parsed_amount, parsed_currency, created_at
               FROM receipts
               WHERE user_id=? AND status=3
                 AND datetime(created_at) < datetime(?)
               ORDER BY datetime(created_at) ASC""",
            (user_id, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def month_drafts_for_user(user_id: int, year: int, month: int) -> list[dict]:
    """Drafts (status=3) filed during a given year-month for one user.
    `created_at` is SQLite CURRENT_TIMESTAMP, so bounds use datetime()."""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    with db() as conn:
        rows = conn.execute(
            """SELECT id, parsed_merchant, parsed_amount, parsed_currency, created_at
               FROM receipts
               WHERE user_id=? AND status=3
                 AND datetime(created_at) >= datetime(?)
                 AND datetime(created_at) <  datetime(?)
               ORDER BY datetime(created_at) ASC""",
            (user_id, start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def log_nudge(user_id: int, hook: str, message_preview: str) -> None:
    """Record an outbound nudge so rate limits + per-hook suppression work."""
    with db() as conn:
        conn.execute(
            "INSERT INTO nudges (user_id, hook, message_preview) VALUES (?, ?, ?)",
            (user_id, hook, message_preview[:200]),
        )


# Alias used by heartbeat runner
record_nudge = log_nudge


def list_active_users() -> list[dict]:
    """Return all users who have a valid OmniHR session (access_jwt not null).

    'Active' here means paired — the access token may be stale (the refresh
    sweeper handles re-minting), but the user has at least gone through the
    pairing flow. We exclude users whose refresh token is already dead,
    users without a paired OmniHR employee ID, and non-Telegram users
    (heartbeat only sends Telegram messages).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM users
               WHERE access_jwt IS NOT NULL
                 AND refresh_expires_at > ?
                 AND omnihr_employee_id IS NOT NULL
                 AND channel = 'telegram'""",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def was_nudged_recently(user_id: int, hook: str, within_hours: float) -> bool:
    """Return True if a nudge with this hook was sent to user within `within_hours` hours.

    Uses datetime() normalisation on both sides (same reason as
    aging_drafts_for_user — CURRENT_TIMESTAMP lacks a timezone offset).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    with db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM nudges
               WHERE user_id=? AND hook=?
                 AND datetime(sent_at) > datetime(?)""",
            (user_id, hook, cutoff),
        ).fetchone()
        return int(row["n"] or 0) > 0


def log_message(
    user_id: int,
    direction: str,
    body: str | None = None,
    *,
    has_file: bool = False,
    file_type: str | None = None,
) -> None:
    """Persist every inbound/outbound message for debugging. direction: 'in'|'out'."""
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, direction, body, has_file, file_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, direction, body, int(has_file), file_type),
        )


def get_recent_messages(user_id: int, limit: int = 10) -> list[dict]:
    """Return the last `limit` text messages for user, oldest first, for conversation history."""
    with db() as conn:
        rows = conn.execute(
            "SELECT direction, body FROM messages "
            "WHERE user_id=? AND body IS NOT NULL AND body != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"direction": r["direction"], "body": r["body"]} for r in reversed(rows)]


def count_nudges_since(user_id: int, since: datetime, *, hook: str | None = None) -> int:
    """Count nudges sent to user since `since`. If `hook` is given, only that
    kind counts — used for per-hook spacing rules.
    Uses datetime() on both sides (same reason as aging_drafts_for_user)."""
    sql = (
        "SELECT COUNT(*) AS n FROM nudges "
        "WHERE user_id=? AND datetime(sent_at) > datetime(?)"
    )
    args: list = [user_id, since.isoformat()]
    if hook is not None:
        sql += " AND hook=?"
        args.append(hook)
    with db() as conn:
        row = conn.execute(sql, args).fetchone()
        return int(row["n"] or 0)


# --- Merchant memory ---
# Each row = "this user filed merchant M under policy P / sub_cat S N times".
# After 3 consistent fills the agent files without asking. See
# render_merchants_block() in bot/common/agent.py for the context-prompt side.

def normalize_merchant(name: str) -> str:
    """Collapse whitespace, lowercase, strip. Good enough for fuzzy-ish match."""
    return " ".join((name or "").lower().split())


def record_merchant_choice(
    user_id: int,
    merchant: str,
    policy_id: str,
    sub_category: str | None,
) -> None:
    """Bump the count for (user, merchant, policy, sub_cat). Insert row if new.
    Called after submit_claim succeeds — proves the user accepted the filing.

    NOTE: SQLite treats NULL as distinct in UNIQUE constraints, so a NULL
    sub_category would never collide on ON CONFLICT and we'd get dup rows.
    We coerce None → "" before the INSERT to make the constraint work."""
    norm = normalize_merchant(merchant)
    if not norm:
        return
    sub_key = sub_category if sub_category is not None else ""
    with db() as conn:
        conn.execute(
            """INSERT INTO merchant_choices
               (user_id, merchant_normalized, merchant_display, policy_id, sub_category)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, merchant_normalized, policy_id, sub_category)
               DO UPDATE SET count=count+1, last_seen=CURRENT_TIMESTAMP""",
            (user_id, norm, merchant, policy_id, sub_key),
        )


def get_merchant_history(user_id: int, merchant_normalized: str) -> list[dict]:
    """Return all (policy, sub_cat, count) rows for this merchant, most-filed first.
    sub_category is '' (never None) — see record_merchant_choice's NULL coercion."""
    with db() as conn:
        rows = conn.execute(
            """SELECT policy_id, sub_category, count, last_seen
               FROM merchant_choices
               WHERE user_id=? AND merchant_normalized=?
               ORDER BY count DESC""",
            (user_id, merchant_normalized),
        ).fetchall()
        return [dict(r) for r in rows]


def top_merchants(user_id: int, limit: int = 20) -> list[dict]:
    """Top merchants by fill count across all classifications.
    Used for the context block so Janai can eyeball the pattern.

    Note: sub_category is '' (never None) for rows stored without one —
    record_merchant_choice coerces NULL to empty string so ON CONFLICT
    dedups correctly."""
    with db() as conn:
        rows = conn.execute(
            """SELECT merchant_display AS merchant, policy_id, sub_category, count
               FROM merchant_choices
               WHERE user_id=?
               ORDER BY count DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Telegram session ---

def set_telegram_session(user_id: int, session_str: str, phone: str) -> None:
    """Encrypt and store a Telethon StringSession for a user."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET telegram_session=?, telegram_phone=? WHERE id=?",
            (crypto.encrypt(session_str), phone, user_id),
        )


def get_telegram_session(user_id: int) -> str | None:
    """Return the decrypted Telethon StringSession, or None if not connected."""
    with db() as conn:
        row = conn.execute(
            "SELECT telegram_session FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row or not row["telegram_session"]:
            return None
        return crypto.decrypt(row["telegram_session"])


# --- WhatsApp connection ---

def set_whatsapp_connected(user_id: int, phone: str) -> None:
    """Mark WhatsApp as connected for a user and store their E.164 phone."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET whatsapp_phone=?, whatsapp_connected=1 WHERE id=?",
            (phone, user_id),
        )


def get_whatsapp_connected(user_id: int) -> bool:
    """Return True if the user has a connected WhatsApp session."""
    with db() as conn:
        row = conn.execute(
            "SELECT whatsapp_connected FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return bool(row and row["whatsapp_connected"])


def set_ext_session(user_id: int, token: str) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET ext_session=? WHERE id=?", (token, user_id))


def get_user_by_ext_session(token: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE ext_session=?", (token,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Multi-account Google
# ---------------------------------------------------------------------------

def add_google_account(
    user_id: int,
    *,
    email: str,
    access_token: str,
    refresh_token: str | None,
    expiry: "datetime | None",
) -> None:
    enc_access = crypto.encrypt(access_token) if access_token else None
    enc_refresh = crypto.encrypt(refresh_token) if refresh_token else None
    expiry_str = expiry.isoformat() if expiry else None
    with db() as conn:
        conn.execute(
            """INSERT INTO google_accounts (user_id, email, access_token, refresh_token, token_expiry)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, email) DO UPDATE SET
                 access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token,
                 token_expiry=excluded.token_expiry""",
            (user_id, email, enc_access, enc_refresh, expiry_str),
        )


def get_google_accounts(user_id: int) -> list[dict]:
    """Return all connected Google accounts. Falls back to users table for legacy single-account."""
    with db() as conn:
        rows = conn.execute(
            "SELECT email, access_token, refresh_token, token_expiry FROM google_accounts WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    if rows:
        result = []
        for r in rows:
            result.append({
                "email": r["email"],
                "access_token": crypto.decrypt(r["access_token"]) if r["access_token"] else None,
                "refresh_token": crypto.decrypt(r["refresh_token"]) if r["refresh_token"] else None,
                "expiry": datetime.fromisoformat(r["token_expiry"]) if r["token_expiry"] else None,
            })
        return result
    # Fallback: legacy single-account in users table
    access, refresh, expiry, email = get_google_tokens(user_id)
    if access:
        return [{"email": email, "access_token": access, "refresh_token": refresh, "expiry": expiry}]
    return []


def remove_google_account(user_id: int, email: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM google_accounts WHERE user_id=? AND email=?", (user_id, email))


def update_google_account_token(user_id: int, email: str, access_token: str, expiry: "datetime | None") -> None:
    enc_access = crypto.encrypt(access_token) if access_token else None
    expiry_str = expiry.isoformat() if expiry else None
    with db() as conn:
        conn.execute(
            "UPDATE google_accounts SET access_token=?, token_expiry=? WHERE user_id=? AND email=?",
            (enc_access, expiry_str, user_id, email),
        )


# ---------------------------------------------------------------------------
# Multi-account Telegram
# ---------------------------------------------------------------------------

def add_telegram_account(user_id: int, phone: str, session_str: str) -> None:
    enc = crypto.encrypt(session_str) if session_str else None
    with db() as conn:
        conn.execute(
            """INSERT INTO telegram_accounts (user_id, phone, session_str)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, phone) DO UPDATE SET session_str=excluded.session_str""",
            (user_id, phone, enc),
        )


def get_telegram_accounts(user_id: int) -> list[dict]:
    """Return all connected Telegram accounts. Falls back to users table for legacy."""
    with db() as conn:
        rows = conn.execute(
            "SELECT phone, session_str FROM telegram_accounts WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    if rows:
        return [
            {
                "phone": r["phone"],
                "session_str": crypto.decrypt(r["session_str"]) if r["session_str"] else None,
            }
            for r in rows
        ]
    # Fallback: legacy single session in users table
    session = get_telegram_session(user_id)
    if session:
        with db() as conn:
            row = conn.execute("SELECT telegram_phone FROM users WHERE id=?", (user_id,)).fetchone()
        phone = row["telegram_phone"] if row else ""
        return [{"phone": phone or "", "session_str": session}]
    return []


def remove_telegram_account(user_id: int, phone: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM telegram_accounts WHERE user_id=? AND phone=?", (user_id, phone))


# ---------------------------------------------------------------------------
# Multi-account WhatsApp
# ---------------------------------------------------------------------------

def add_whatsapp_account(user_id: int, phone: str, session_id: str) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO whatsapp_accounts (user_id, phone, session_id)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, session_id) DO UPDATE SET phone=excluded.phone""",
            (user_id, phone, session_id),
        )


def get_whatsapp_accounts(user_id: int) -> list[dict]:
    """Return all connected WhatsApp accounts. Falls back to users table for legacy."""
    with db() as conn:
        rows = conn.execute(
            "SELECT phone, session_id FROM whatsapp_accounts WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    if rows:
        return [{"phone": r["phone"] or "", "session_id": r["session_id"]} for r in rows]
    # Fallback: legacy single account in users table
    if get_whatsapp_connected(user_id):
        with db() as conn:
            row = conn.execute("SELECT whatsapp_phone FROM users WHERE id=?", (user_id,)).fetchone()
        phone = row["whatsapp_phone"] if row else ""
        return [{"phone": phone or "", "session_id": str(user_id)}]
    return []


def remove_whatsapp_account(user_id: int, session_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM whatsapp_accounts WHERE user_id=? AND session_id=?", (user_id, session_id))
    # If no accounts left, clear legacy flag too
    with db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) as c FROM whatsapp_accounts WHERE user_id=?", (user_id,)
        ).fetchone()["c"]
    if remaining == 0:
        with db() as conn:
            conn.execute("UPDATE users SET whatsapp_connected=0, whatsapp_phone=NULL WHERE id=?", (user_id,))


def get_all_users_with_whatsapp() -> list[int]:
    """Return user_ids that have at least one connected WhatsApp account."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM whatsapp_accounts"
        ).fetchall()
    ids = [r["user_id"] for r in rows]
    if not ids:
        # Fallback: legacy users table
        with db() as conn:
            rows = conn.execute(
                "SELECT id FROM users WHERE whatsapp_connected=1"
            ).fetchall()
        ids = [r["id"] for r in rows]
    return ids
