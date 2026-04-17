"""Tiny SQLite storage layer for the local-test / single-host deployment.

Secrets (anth_key, refresh_jwt, access_jwt) are encrypted via bot.crypto before
touching the DB. Decrypt only at use time. Never log raw values.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import crypto

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
"""


_ADD_COLS = [
    ("receipts", "omnihr_file_path", "TEXT"),
    ("receipts", "omnihr_file_name", "TEXT"),
    ("receipts", "omnihr_file_mime", "TEXT"),
    ("receipts", "tg_file_id", "TEXT"),
    ("receipts", "tg_file_type", "TEXT"),  # "photo" or "document"
]


def init_db(path: Path = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        # idempotent column adds for already-created DBs
        for table, col, typ in _ADD_COLS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass


@contextmanager
def db(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
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
    with db() as conn:
        conn.execute("UPDATE users SET anth_key=? WHERE id=?", (crypto.encrypt(key), user_id))


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
               omnihr_employee_id=?, omnihr_full_name=?, omnihr_email=?, tenant_id=?
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
