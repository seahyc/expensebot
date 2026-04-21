"""Tests for bot/heartbeat.py and the related storage additions.

Covers:
- Active hours check (inside/outside window)
- Task parsing from HEARTBEAT.md
- _parse_every helper
- was_nudged_recently cooldown suppression
- list_active_users returns only paired users
- HEARTBEAT_OK suppresses Telegram message
- Actionable response sends Telegram message and records nudge
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import storage
from bot.heartbeat import HeartbeatRunner, _parse_every


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """Provide a deterministic fake encryption key so crypto operations work in tests."""
    monkeypatch.setenv("ENCRYPTION_KEY", "test_key_aabbccddeeff00112233445566778899")


@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "test.db"
        monkeypatch.setattr(storage, "DB_PATH", path)
        storage.init_db(path)
        yield path


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def mock_anth():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client


@pytest.fixture
def mock_anthropic_factory(mock_anth):
    async def factory(user):
        return mock_anth
    return factory


@pytest.fixture
def mock_omnihr_factory():
    async def factory(user):
        return MagicMock()
    return factory


@pytest.fixture
def runner(mock_bot, mock_anthropic_factory, mock_omnihr_factory):
    r = HeartbeatRunner(
        telegram_bot=mock_bot,
        anthropic_factory=mock_anthropic_factory,
        omnihr_factory=mock_omnihr_factory,
    )
    return r


# ---------------------------------------------------------------------------
# _parse_every helper
# ---------------------------------------------------------------------------

def test_parse_every_hours():
    assert _parse_every("24h") == 24.0


def test_parse_every_minutes():
    assert _parse_every("30m") == 0.5


def test_parse_every_invalid_defaults_to_24():
    assert _parse_every("invalid") == 24.0


def test_parse_every_4h():
    assert _parse_every("4h") == 4.0


def test_parse_every_12h():
    assert _parse_every("12h") == 12.0


# ---------------------------------------------------------------------------
# Active hours check
# ---------------------------------------------------------------------------

SGT = timezone(timedelta(hours=8))


def test_is_active_hours_inside(runner, monkeypatch):
    # 10am SGT → active
    fake_now = datetime(2026, 4, 19, 10, 0, 0, tzinfo=SGT)
    with patch("bot.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert runner._is_active_hours() is True


def test_is_active_hours_outside_early(runner, monkeypatch):
    # 6am SGT → not active
    fake_now = datetime(2026, 4, 19, 6, 0, 0, tzinfo=SGT)
    with patch("bot.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert runner._is_active_hours() is False


def test_is_active_hours_outside_late(runner, monkeypatch):
    # 11pm SGT → not active
    fake_now = datetime(2026, 4, 19, 23, 0, 0, tzinfo=SGT)
    with patch("bot.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert runner._is_active_hours() is False


def test_is_active_hours_boundary_start(runner):
    # 8am SGT is the boundary — should be active (>=)
    fake_now = datetime(2026, 4, 19, 8, 0, 0, tzinfo=SGT)
    with patch("bot.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert runner._is_active_hours() is True


def test_is_active_hours_boundary_end(runner):
    # 22:00 SGT is the boundary — should NOT be active (<)
    fake_now = datetime(2026, 4, 19, 22, 0, 0, tzinfo=SGT)
    with patch("bot.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        assert runner._is_active_hours() is False


# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------

def test_load_tasks_returns_list(runner):
    tasks = runner._load_tasks()
    assert isinstance(tasks, list)
    assert len(tasks) >= 3


def test_load_tasks_has_required_fields(runner):
    tasks = runner._load_tasks()
    for task in tasks:
        assert "id" in task
        assert "every" in task
        assert "prompt" in task


def test_load_tasks_known_ids(runner):
    tasks = runner._load_tasks()
    ids = {t["id"] for t in tasks}
    assert "claim_status" in ids
    assert "aging_drafts" in ids
    assert "gmail_receipts" in ids


def test_load_tasks_every_values(runner):
    tasks = runner._load_tasks()
    by_id = {t["id"]: t for t in tasks}
    assert by_id["claim_status"]["every"] == "24h"
    assert by_id["aging_drafts"]["every"] == "12h"
    assert by_id["gmail_receipts"]["every"] == "4h"


# ---------------------------------------------------------------------------
# Storage: list_active_users
# ---------------------------------------------------------------------------

def test_list_active_users_empty(tmp_db):
    users = storage.list_active_users()
    assert users == []


def test_list_active_users_returns_paired_user(tmp_db):
    uid = storage.upsert_user("telegram", "tg123")
    future = datetime.now(timezone.utc) + timedelta(days=30)
    storage.set_omnihr_session(
        uid,
        access_jwt="fake_access",
        refresh_jwt="fake_refresh",
        access_expires_at=future,
        refresh_expires_at=future,
        employee_id=42,
        full_name="Test User",
        email="test@example.com",
        tenant_id="testco",
    )
    users = storage.list_active_users()
    assert len(users) == 1
    assert users[0]["id"] == uid


def test_list_active_users_excludes_unpaired(tmp_db):
    # User without access_jwt
    storage.upsert_user("telegram", "tg456")
    users = storage.list_active_users()
    assert users == []


def test_list_active_users_excludes_expired_refresh(tmp_db):
    uid = storage.upsert_user("telegram", "tg789")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    storage.set_omnihr_session(
        uid,
        access_jwt="fake_access",
        refresh_jwt="fake_refresh",
        access_expires_at=future,
        refresh_expires_at=past,  # expired
        employee_id=99,
        full_name="Expired User",
        email="exp@example.com",
        tenant_id="testco",
    )
    users = storage.list_active_users()
    assert users == []


# ---------------------------------------------------------------------------
# Storage: was_nudged_recently
# ---------------------------------------------------------------------------

def test_was_nudged_recently_no_nudge(tmp_db):
    uid = storage.upsert_user("telegram", "tg1")
    assert storage.was_nudged_recently(uid, hook="heartbeat_aging_drafts", within_hours=12) is False


def test_was_nudged_recently_recent_nudge(tmp_db):
    uid = storage.upsert_user("telegram", "tg1")
    storage.log_nudge(uid, hook="heartbeat_aging_drafts", message_preview="reminder")
    assert storage.was_nudged_recently(uid, hook="heartbeat_aging_drafts", within_hours=12) is True


def test_was_nudged_recently_different_hook(tmp_db):
    uid = storage.upsert_user("telegram", "tg1")
    storage.log_nudge(uid, hook="heartbeat_claim_status", message_preview="status")
    # Different hook — should not block
    assert storage.was_nudged_recently(uid, hook="heartbeat_aging_drafts", within_hours=12) is False


def test_record_nudge_alias(tmp_db):
    """record_nudge is an alias for log_nudge — both should work."""
    uid = storage.upsert_user("telegram", "tg1")
    storage.record_nudge(uid, hook="heartbeat_test", message_preview="test msg")
    assert storage.was_nudged_recently(uid, hook="heartbeat_test", within_hours=1) is True


# ---------------------------------------------------------------------------
# _tick_all_users: outside active hours → no work
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_all_users_outside_hours_skips(runner, tmp_db, monkeypatch):
    fake_now = datetime(2026, 4, 19, 3, 0, 0, tzinfo=SGT)
    with patch("bot.heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        # Should return early without calling list_active_users
        with patch.object(storage, "list_active_users", return_value=[]) as mock_list:
            await runner._tick_all_users()
            mock_list.assert_not_called()


# ---------------------------------------------------------------------------
# _tick_user: HEARTBEAT_OK suppresses message
# ---------------------------------------------------------------------------

def _make_text_response(text: str):
    """Build a minimal Anthropic messages response with end_turn."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(stop_reason="end_turn", content=[block])


@pytest.mark.asyncio
async def test_heartbeat_ok_suppresses_message(runner, mock_bot, mock_anth, tmp_db):
    uid = storage.upsert_user("telegram", "9990000")
    future = datetime.now(timezone.utc) + timedelta(days=30)
    storage.set_omnihr_session(
        uid,
        access_jwt="a", refresh_jwt="r",
        access_expires_at=future, refresh_expires_at=future,
        employee_id=1, full_name="Tester", email="t@t.com", tenant_id="t",
    )
    user = storage.get_user(uid)

    mock_anth.messages.create.return_value = _make_text_response("HEARTBEAT_OK")

    await runner._tick_user({**user, "channel": "telegram", "channel_user_id": "9990000"}, runner._load_tasks())

    # No Telegram message should be sent
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_actionable_response_sends_message(runner, mock_bot, mock_anth, tmp_db):
    # Telegram channel_user_id values are always numeric strings in production
    tg_chat_id = "8880000"
    uid = storage.upsert_user("telegram", tg_chat_id)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    storage.set_omnihr_session(
        uid,
        access_jwt="a", refresh_jwt="r",
        access_expires_at=future, refresh_expires_at=future,
        employee_id=2, full_name="Sender", email="s@s.com", tenant_id="s",
    )
    user = storage.get_user(uid)

    mock_anth.messages.create.return_value = _make_text_response(
        "You have 2 drafts older than 3 days. Review and submit them when ready."
    )

    await runner._tick_user({**user, "channel": "telegram", "channel_user_id": tg_chat_id}, runner._load_tasks())

    # Should have sent at least one message (one per task with actionable response)
    assert mock_bot.send_message.called
    call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.kwargs.get("chat_id") == int(tg_chat_id)


@pytest.mark.asyncio
async def test_actionable_response_is_logged_to_messages(runner, mock_bot, mock_anth, tmp_db):
    """Regression: heartbeat nudges must be persisted to the messages table so the
    agent sees its own prior proactive questions in get_recent_messages history.
    Otherwise a user reply like 'yeah can file' gets routed to the wrong pending
    question because the nudge is invisible in the conversation transcript."""
    tg_chat_id = "5550000"
    uid = storage.upsert_user("telegram", tg_chat_id)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    storage.set_omnihr_session(
        uid,
        access_jwt="a", refresh_jwt="r",
        access_expires_at=future, refresh_expires_at=future,
        employee_id=42, full_name="HB", email="hb@h.com", tenant_id="hb",
    )
    user = storage.get_user(uid)

    nudge_text = "Found one unread email from Cloudflare. Want me to file it?"
    mock_anth.messages.create.return_value = _make_text_response(nudge_text)

    before = storage.get_recent_messages(uid, limit=20)
    await runner._tick_user({**user, "channel": "telegram", "channel_user_id": tg_chat_id}, runner._load_tasks())
    after = storage.get_recent_messages(uid, limit=20)

    new_out = [m for m in after if m not in before and m["direction"] == "out"]
    assert new_out, "heartbeat should have written at least one outbound message"
    assert any(nudge_text in (m["body"] or "") for m in new_out)


# ---------------------------------------------------------------------------
# Cooldown: nudged recently → skip task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cooldown_skips_task(runner, mock_bot, mock_anth, tmp_db):
    uid = storage.upsert_user("telegram", "7770000")
    future = datetime.now(timezone.utc) + timedelta(days=30)
    storage.set_omnihr_session(
        uid,
        access_jwt="a", refresh_jwt="r",
        access_expires_at=future, refresh_expires_at=future,
        employee_id=3, full_name="Cooldown", email="c@c.com", tenant_id="c",
    )
    user = storage.get_user(uid)

    # Pre-populate nudges for ALL 3 tasks so they're all on cooldown
    for task_id in ("claim_status", "aging_drafts", "gmail_receipts"):
        storage.log_nudge(uid, hook=f"heartbeat_{task_id}", message_preview="prev")

    await runner._tick_user({**user, "channel": "telegram", "channel_user_id": "7770000"}, runner._load_tasks())

    # Claude should not have been called (all tasks on cooldown)
    mock_anth.messages.create.assert_not_called()
    mock_bot.send_message.assert_not_called()
