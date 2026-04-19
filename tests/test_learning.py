"""Tests for the self-learning harness (bot/learning.py).

Covers:
- Turn threshold logic (no fire before threshold, fire at threshold, reset)
- Submit threshold logic (mirrors DB-persisted counter)
- NOTHING_NEW path (user_md unchanged)
- Valid update path (user_md written with all 5 headers)
- Validation failure path (proposed md missing headers → no write)
- Storage functions: increment_submit_count, get_submit_count
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import learning, storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_turn_counts():
    """Reset the in-memory turn counter before each test."""
    learning._turn_counts.clear()
    yield
    learning._turn_counts.clear()


@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        monkeypatch.setattr(storage, "DB_PATH", path)
        storage.init_db(path)
        yield path


@pytest.fixture
def mock_anthropic():
    """Return a mock AsyncAnthropic client."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client


def _make_response(text: str):
    """Build a minimal Anthropic messages response object."""
    content_block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[content_block])


VALID_USER_MD = """\
# Janai memory

## Classification rules
- (none yet)

## Merchant shortcuts
- (none yet)

## Defaults
- (none yet)

## Description style
- (none yet)

## Don't ask me about
- (none yet)
"""

UPDATED_USER_MD = """\
# Janai memory

## Classification rules
- **Grab to Changi** — Travel-International/Transportation (2026-04-19)

## Merchant shortcuts
- (none yet)

## Defaults
- (none yet)

## Description style
- (none yet)

## Don't ask me about
- (none yet)
"""


# ---------------------------------------------------------------------------
# Storage: increment_submit_count / get_submit_count
# ---------------------------------------------------------------------------

def test_get_submit_count_fresh_user(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    assert storage.get_submit_count(uid) == 0


def test_increment_submit_count(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    storage.increment_submit_count(uid)
    assert storage.get_submit_count(uid) == 1
    storage.increment_submit_count(uid)
    assert storage.get_submit_count(uid) == 2


def test_increment_submit_count_multiple_users(tmp_db):
    uid1 = storage.upsert_user("telegram", "u1")
    uid2 = storage.upsert_user("telegram", "u2")
    storage.increment_submit_count(uid1)
    storage.increment_submit_count(uid1)
    storage.increment_submit_count(uid2)
    assert storage.get_submit_count(uid1) == 2
    assert storage.get_submit_count(uid2) == 1


# ---------------------------------------------------------------------------
# _validate_user_md
# ---------------------------------------------------------------------------

def test_validate_user_md_valid():
    assert learning._validate_user_md(VALID_USER_MD) == []


def test_validate_user_md_missing_one():
    broken = VALID_USER_MD.replace("## Merchant shortcuts", "## Removed")
    missing = learning._validate_user_md(broken)
    assert "## Merchant shortcuts" in missing
    assert len(missing) == 1


def test_validate_user_md_missing_all():
    missing = learning._validate_user_md("# No sections here")
    assert len(missing) == 5


# ---------------------------------------------------------------------------
# _format_messages
# ---------------------------------------------------------------------------

def test_format_messages_empty():
    result = learning._format_messages([])
    assert "no recent conversation" in result


def test_format_messages_formats_correctly():
    msgs = [
        {"direction": "in", "body": "Hello"},
        {"direction": "out", "body": "Hi there"},
    ]
    result = learning._format_messages(msgs)
    assert "User: Hello" in result
    assert "Janai: Hi there" in result


# ---------------------------------------------------------------------------
# Turn threshold logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_threshold_not_reached(mock_anthropic, tmp_db, monkeypatch):
    """Calls below threshold should NOT spawn any review."""
    monkeypatch.setattr(learning, "LEARNING_TURN_THRESHOLD", 3)
    uid = storage.upsert_user("telegram", "u1")

    spawned_tasks = []
    original_create_task = asyncio.create_task

    def capture_task(coro):
        spawned_tasks.append(coro)
        # We need to actually run the coroutine to avoid warnings
        return original_create_task(coro)

    with patch("bot.learning.asyncio.create_task", side_effect=capture_task):
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")

    assert len(spawned_tasks) == 0
    assert learning._turn_counts[uid] == 2


@pytest.mark.asyncio
async def test_turn_threshold_reached_fires_review(mock_anthropic, tmp_db, monkeypatch):
    """At the threshold, a background task should be spawned and counter reset."""
    monkeypatch.setattr(learning, "LEARNING_TURN_THRESHOLD", 2)
    uid = storage.upsert_user("telegram", "u1")

    spawned_tasks = []

    def capture_task(coro):
        spawned_tasks.append(coro)
        coro.close()  # avoid 'coroutine never awaited' warning
        return MagicMock()

    with patch("bot.learning.asyncio.create_task", side_effect=capture_task):
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")

    assert len(spawned_tasks) == 1
    # Counter resets to 0 after firing
    assert learning._turn_counts.get(uid, 0) == 0


@pytest.mark.asyncio
async def test_turn_counter_resets_and_counts_again(mock_anthropic, tmp_db, monkeypatch):
    """After threshold resets, the next N turns accumulate again."""
    monkeypatch.setattr(learning, "LEARNING_TURN_THRESHOLD", 2)
    uid = storage.upsert_user("telegram", "u1")

    spawned_tasks = []

    def capture_task(coro):
        spawned_tasks.append(coro)
        coro.close()
        return MagicMock()

    with patch("bot.learning.asyncio.create_task", side_effect=capture_task):
        # First cycle
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")
        # Second cycle — one more turn, no fire yet
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "turn")

    assert len(spawned_tasks) == 1  # only first cycle fired
    assert learning._turn_counts[uid] == 1


# ---------------------------------------------------------------------------
# Submit threshold logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_threshold_not_reached(mock_anthropic, tmp_db, monkeypatch):
    """Submit count below threshold should not fire a review."""
    monkeypatch.setattr(learning, "LEARNING_SUBMIT_THRESHOLD", 5)
    uid = storage.upsert_user("telegram", "u1")
    # set count to 3 (not a multiple of 5)
    for _ in range(3):
        storage.increment_submit_count(uid)

    spawned_tasks = []

    def capture_task(coro):
        spawned_tasks.append(coro)
        coro.close()
        return MagicMock()

    with patch("bot.learning.asyncio.create_task", side_effect=capture_task):
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "submit")

    assert len(spawned_tasks) == 0


@pytest.mark.asyncio
async def test_submit_threshold_reached_fires(mock_anthropic, tmp_db, monkeypatch):
    """Submit count that is a multiple of threshold should fire a review."""
    monkeypatch.setattr(learning, "LEARNING_SUBMIT_THRESHOLD", 5)
    uid = storage.upsert_user("telegram", "u1")
    for _ in range(5):
        storage.increment_submit_count(uid)

    spawned_tasks = []

    def capture_task(coro):
        spawned_tasks.append(coro)
        coro.close()
        return MagicMock()

    with patch("bot.learning.asyncio.create_task", side_effect=capture_task):
        await learning.maybe_trigger_review(uid, storage, mock_anthropic, [], "submit")

    assert len(spawned_tasks) == 1


# ---------------------------------------------------------------------------
# run_review: NOTHING_NEW path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_review_nothing_new(mock_anthropic, tmp_db):
    """When Claude returns NOTHING_NEW, user_md must not be changed."""
    uid = storage.upsert_user("telegram", "u1")
    storage.set_user_md(uid, VALID_USER_MD)

    mock_anthropic.messages.create.return_value = _make_response("NOTHING_NEW")

    await learning.run_review(uid, storage, mock_anthropic, [])

    # user_md should be unchanged
    assert storage.get_user_md(uid) == VALID_USER_MD
    mock_anthropic.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# run_review: valid update path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_review_valid_update(mock_anthropic, tmp_db):
    """When Claude returns a valid updated markdown, user_md should be updated."""
    uid = storage.upsert_user("telegram", "u1")
    storage.set_user_md(uid, VALID_USER_MD)

    mock_anthropic.messages.create.return_value = _make_response(UPDATED_USER_MD)

    await learning.run_review(uid, storage, mock_anthropic, [
        {"direction": "in", "body": "Grab to Changi airport, $25"},
        {"direction": "out", "body": "Filed as Travel-International/Transportation"},
    ])

    saved = storage.get_user_md(uid)
    # run_review strips the response before writing, so compare stripped forms
    assert saved.strip() == UPDATED_USER_MD.strip()
    assert "Grab to Changi" in saved


# ---------------------------------------------------------------------------
# run_review: validation failure path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_review_validation_failure(mock_anthropic, tmp_db):
    """When Claude returns markdown missing required headers, user_md must NOT be updated."""
    uid = storage.upsert_user("telegram", "u1")
    storage.set_user_md(uid, VALID_USER_MD)

    bad_md = "# Janai memory\n\n## Only one section\n- something\n"
    mock_anthropic.messages.create.return_value = _make_response(bad_md)

    await learning.run_review(uid, storage, mock_anthropic, [])

    # user_md should still be the original
    assert storage.get_user_md(uid) == VALID_USER_MD


# ---------------------------------------------------------------------------
# run_review: Claude call failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_review_claude_error(mock_anthropic, tmp_db):
    """If the Claude call raises an exception, run_review should swallow it gracefully."""
    uid = storage.upsert_user("telegram", "u1")
    storage.set_user_md(uid, VALID_USER_MD)

    mock_anthropic.messages.create.side_effect = Exception("API error")

    # Should not raise
    await learning.run_review(uid, storage, mock_anthropic, [])

    # user_md unchanged
    assert storage.get_user_md(uid) == VALID_USER_MD


# ---------------------------------------------------------------------------
# run_review: max 4 iterations cap (shouldn't loop more than 4 times)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_review_max_4_calls(mock_anthropic, tmp_db):
    """run_review should call Claude at most 4 times total (currently it stops
    on first result, but the cap is there as a safety net)."""
    uid = storage.upsert_user("telegram", "u1")

    # Return something that looks valid but actually goes through the loop once
    mock_anthropic.messages.create.return_value = _make_response("NOTHING_NEW")

    await learning.run_review(uid, storage, mock_anthropic, [])

    # Should have stopped after 1 call (NOTHING_NEW returned immediately)
    assert mock_anthropic.messages.create.call_count == 1
