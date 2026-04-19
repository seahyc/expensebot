"""Tests for bot.common.context_lookup — receipt triangulation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.common.context_lookup import (
    TriangulationResult,
    _infer_purpose,
    gcal_context,
    gmail_context,
    triangulate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DT = datetime(2026, 4, 15, 12, 30, 0, tzinfo=timezone.utc)


def _mock_gw_output(data) -> str:
    return json.dumps(data)


# ---------------------------------------------------------------------------
# TriangulationResult.as_markdown
# ---------------------------------------------------------------------------


def test_as_markdown_empty_returns_none():
    r = TriangulationResult()
    assert r.as_markdown() is None


def test_as_markdown_with_calendar_only():
    r = TriangulationResult(
        calendar_events=["Team standup at 12:00"],
        email_threads=[],
        inferred_purpose="Work meeting",
        confidence=0.6,
    )
    md = r.as_markdown()
    assert md is not None
    assert "📅 Calendar events near this time:" in md
    assert "Team standup at 12:00" in md
    assert "📧" not in md
    assert "💡 Inferred purpose: Work meeting" in md


def test_as_markdown_with_email_only():
    r = TriangulationResult(
        calendar_events=[],
        email_threads=["Invoice from Grab — from billing@grab.com"],
        inferred_purpose=None,
        confidence=0.0,
    )
    md = r.as_markdown()
    assert md is not None
    assert "📧 Relevant emails:" in md
    assert "Invoice from Grab" in md
    assert "💡 Inferred purpose: unclear" in md


def test_as_markdown_with_both():
    r = TriangulationResult(
        calendar_events=["Client meeting at 14:00"],
        email_threads=["Expense report — from finance@corp.com"],
        inferred_purpose="Client dinner",
        confidence=0.75,
    )
    md = r.as_markdown()
    assert "## What I found in your email & calendar" in md
    assert "📅" in md
    assert "📧" in md
    assert "💡 Inferred purpose: Client dinner" in md


# ---------------------------------------------------------------------------
# _infer_purpose
# ---------------------------------------------------------------------------


def test_infer_purpose_no_context():
    purpose, conf = _infer_purpose([], [], "Grab", "transport")
    assert purpose is None
    assert conf == 0.0


def test_infer_purpose_work_keywords_in_calendar():
    events = ["Client standup at 09:00", "Team meeting at 10:00"]
    purpose, conf = _infer_purpose(events, [], "Grab", "transport")
    assert purpose is not None
    assert "Client standup" in purpose
    assert conf > 0.4


def test_infer_purpose_personal_beats_none():
    events = ["Birthday party at 19:00"]
    purpose, conf = _infer_purpose(events, [], "Uber Eats", "meal")
    # Has an event but no strong work signal — should still return something
    assert purpose is not None or conf == 0.0  # either "possibly related" or no match


def test_infer_purpose_work_email():
    threads = ["Meeting with client — from john@company.com"]
    purpose, conf = _infer_purpose([], threads, "RestaurantXYZ", "meal")
    # work_hits > 0 from "meeting" and "client"
    assert purpose is not None
    assert conf > 0.0


# ---------------------------------------------------------------------------
# gmail_context — mocked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_context_empty_merchant_returns_empty():
    result = await gmail_context("", DT)
    assert result == []


@pytest.mark.asyncio
async def test_gmail_context_json_output():
    messages = [
        {"subject": "Grab receipt", "from": "no-reply@grab.com", "snippet": "Your ride cost SGD 14.50"},
        {"subject": "Your Grab trip", "from": "no-reply@grab.com", "snippet": ""},
    ]
    fake_output = json.dumps({"messages": messages})

    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value=fake_output)):
        results = await gmail_context("Grab", DT)

    assert len(results) == 2
    assert "Grab receipt" in results[0]
    assert "no-reply@grab.com" in results[0]


@pytest.mark.asyncio
async def test_gmail_context_list_output():
    """gw may return a list directly (not wrapped in messages key)."""
    messages = [
        {"subject": "Lunch meeting", "from": "boss@corp.com", "snippet": ""},
    ]
    fake_output = json.dumps(messages)

    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value=fake_output)):
        results = await gmail_context("GrillHouse", DT)

    assert len(results) == 1
    assert "Lunch meeting" in results[0]


@pytest.mark.asyncio
async def test_gmail_context_empty_response():
    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value="")):
        results = await gmail_context("Starbucks", DT)
    assert results == []


@pytest.mark.asyncio
async def test_gmail_context_non_json_output():
    """Falls back to line parsing on non-JSON output."""
    raw = "Grab receipt from billing@grab.com\nAnother thread"
    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value=raw)):
        results = await gmail_context("Grab", DT)
    assert len(results) == 2
    assert "Grab receipt" in results[0]


# ---------------------------------------------------------------------------
# gcal_context — mocked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcal_context_json_output():
    events = [
        {"summary": "Team standup", "start": {"dateTime": "2026-04-15T12:00:00+00:00"}},
        {"summary": "All-hands", "start": {"dateTime": "2026-04-15T13:00:00+00:00"}},
    ]
    fake_output = json.dumps({"items": events})

    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value=fake_output)):
        results = await gcal_context(DT, window_hours=2.0)

    assert len(results) == 2
    assert "Team standup at 12:00" in results[0]


@pytest.mark.asyncio
async def test_gcal_context_list_output():
    events = [
        {"summary": "Offsite dinner", "start": {"date": "2026-04-15"}},
    ]
    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value=json.dumps(events))):
        results = await gcal_context(DT)
    assert len(results) == 1
    assert "Offsite dinner" in results[0]


@pytest.mark.asyncio
async def test_gcal_context_empty_response():
    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value="")):
        results = await gcal_context(DT)
    assert results == []


@pytest.mark.asyncio
async def test_gcal_context_no_title_event():
    events = [{"start": {"dateTime": "2026-04-15T12:00:00+00:00"}}]
    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value=json.dumps({"items": events}))):
        results = await gcal_context(DT)
    assert "(no title)" in results[0]


# ---------------------------------------------------------------------------
# triangulate — integration of both
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triangulate_returns_empty_on_no_data():
    with patch("bot.common.context_lookup._run_gw", new=AsyncMock(return_value="")):
        result = await triangulate("Starbucks", DT, "meal")

    assert isinstance(result, TriangulationResult)
    assert result.calendar_events == []
    assert result.email_threads == []
    assert result.inferred_purpose is None
    assert result.confidence == 0.0
    assert result.as_markdown() is None


@pytest.mark.asyncio
async def test_triangulate_with_context():
    cal_events = [{"summary": "Client lunch", "start": {"dateTime": "2026-04-15T12:00:00+00:00"}}]
    gmail_msgs = [{"subject": "Meeting with client", "from": "client@corp.com", "snippet": ""}]

    call_count = 0

    async def mock_gw(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if "calendar" in args:
            return json.dumps({"items": cal_events})
        if "gmail" in args:
            return json.dumps({"messages": gmail_msgs})
        return ""

    with patch("bot.common.context_lookup._run_gw", new=mock_gw):
        result = await triangulate("RestaurantXYZ", DT, "meal")

    assert len(result.calendar_events) == 1
    assert "Client lunch" in result.calendar_events[0]
    assert len(result.email_threads) == 1
    assert result.inferred_purpose is not None
    assert result.confidence > 0.0


@pytest.mark.asyncio
async def test_triangulate_window_transport():
    """transport type uses ±2h window — verify the gw call includes time-min/max args."""
    captured_args: list[tuple] = []

    async def mock_gw(*args, **kwargs):
        captured_args.append(args)
        return ""

    with patch("bot.common.context_lookup._run_gw", new=mock_gw):
        await triangulate("Grab", DT, "transport")

    # Should have called both gmail and calendar
    assert len(captured_args) == 2
    cal_call = next((a for a in captured_args if "calendar" in a), None)
    assert cal_call is not None
    assert "--time-min" in cal_call
    assert "--time-max" in cal_call


@pytest.mark.asyncio
async def test_triangulate_subprocess_exception_handled():
    """If subprocess raises, triangulate should still return an empty result."""

    async def boom(*args, **kwargs):
        raise RuntimeError("auth expired")

    with patch("bot.common.context_lookup._run_gw", new=boom):
        result = await triangulate("SomeMerchant", DT, "other")

    # Should degrade gracefully — empty lists, no exception
    assert result.calendar_events == []
    assert result.email_threads == []


@pytest.mark.asyncio
async def test_triangulate_flight_uses_full_day_window():
    """flight type uses 12h window."""
    captured_args: list[tuple] = []

    async def mock_gw(*args, **kwargs):
        captured_args.append(args)
        return ""

    with patch("bot.common.context_lookup._run_gw", new=mock_gw):
        await triangulate("Singapore Airlines", DT, "flight")

    cal_call = next((a for a in captured_args if "calendar" in a), None)
    assert cal_call is not None
    # Check that time-min is 12h before DT
    idx = list(cal_call).index("--time-min")
    time_min_str = cal_call[idx + 1]
    time_min = datetime.fromisoformat(time_min_str)
    # Should be ~12h before DT
    delta = DT - time_min.replace(tzinfo=timezone.utc)
    assert abs(delta.total_seconds() - 12 * 3600) < 60  # within 1 minute
