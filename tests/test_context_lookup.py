"""Tests for bot.common.context_lookup — receipt triangulation."""

from __future__ import annotations

import asyncio
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

DT = datetime(2026, 4, 15, 12, 30, 0, tzinfo=timezone.utc)
USER_ID = 42
FAKE_TOKEN = "ya29.fake-access-token"


def _mock_token():
    """Patch _get_valid_access_token to return a fake token."""
    return patch(
        "bot.common.context_lookup._get_valid_access_token",
        new=AsyncMock(return_value=FAKE_TOKEN),
    )


def _mock_no_token():
    return patch(
        "bot.common.context_lookup._get_valid_access_token",
        new=AsyncMock(return_value=None),
    )


def _make_http_response(status_code: int, json_data: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


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
    assert purpose is not None or conf == 0.0


def test_infer_purpose_work_email():
    threads = ["Meeting with client — from john@company.com"]
    purpose, conf = _infer_purpose([], threads, "RestaurantXYZ", "meal")
    assert purpose is not None
    assert conf > 0.0


# ---------------------------------------------------------------------------
# gmail_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_context_empty_merchant_returns_empty():
    result = await gmail_context("", DT, user_id=USER_ID)
    assert result == []


@pytest.mark.asyncio
async def test_gmail_context_no_user_id_returns_empty():
    result = await gmail_context("Grab", DT, user_id=None)
    assert result == []


@pytest.mark.asyncio
async def test_gmail_context_no_token_returns_empty():
    with _mock_no_token():
        result = await gmail_context("Grab", DT, user_id=USER_ID)
    assert result == []


@pytest.mark.asyncio
async def test_gmail_context_returns_thread_snippets():
    threads = [
        {"id": "abc", "snippet": "Grab receipt SGD 14.50 from billing@grab.com"},
        {"id": "def", "snippet": "Your Grab trip is complete"},
    ]
    mock_resp = _make_http_response(200, {"threads": threads})

    with _mock_token():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            results = await gmail_context("Grab", DT, user_id=USER_ID)

    assert len(results) == 2
    assert "Grab receipt" in results[0]


@pytest.mark.asyncio
async def test_gmail_context_api_error_returns_empty():
    mock_resp = _make_http_response(401, {})

    with _mock_token():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            results = await gmail_context("Grab", DT, user_id=USER_ID)

    assert results == []


# ---------------------------------------------------------------------------
# gcal_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcal_context_no_user_id_returns_empty():
    result = await gcal_context(DT, user_id=None)
    assert result == []


@pytest.mark.asyncio
async def test_gcal_context_no_token_returns_empty():
    with _mock_no_token():
        result = await gcal_context(DT, user_id=USER_ID)
    assert result == []


@pytest.mark.asyncio
async def test_gcal_context_returns_events():
    events = [
        {"summary": "Team standup", "start": {"dateTime": "2026-04-15T12:00:00+00:00"}},
        {"summary": "All-hands", "start": {"dateTime": "2026-04-15T13:00:00+00:00"}},
    ]
    mock_resp = _make_http_response(200, {"items": events})

    with _mock_token():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            results = await gcal_context(DT, user_id=USER_ID, window_hours=2.0)

    assert len(results) == 2
    assert "Team standup at 12:00" in results[0]


@pytest.mark.asyncio
async def test_gcal_context_no_title_event():
    events = [{"start": {"dateTime": "2026-04-15T12:00:00+00:00"}}]
    mock_resp = _make_http_response(200, {"items": events})

    with _mock_token():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            results = await gcal_context(DT, user_id=USER_ID)

    assert "(no title)" in results[0]


# ---------------------------------------------------------------------------
# triangulate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triangulate_returns_empty_when_no_token():
    with _mock_no_token():
        result = await triangulate("Starbucks", DT, "meal", user_id=USER_ID)

    assert isinstance(result, TriangulationResult)
    assert result.calendar_events == []
    assert result.email_threads == []
    assert result.inferred_purpose is None
    assert result.as_markdown() is None


@pytest.mark.asyncio
async def test_triangulate_with_context():
    cal_resp = _make_http_response(200, {
        "items": [{"summary": "Client lunch", "start": {"dateTime": "2026-04-15T12:00:00+00:00"}}]
    })
    gmail_resp = _make_http_response(200, {
        "threads": [{"id": "x", "snippet": "Meeting with client — agenda attached"}]
    })

    responses = [cal_resp, gmail_resp]
    call_idx = 0

    async def _get(*args, **kwargs):
        nonlocal call_idx
        resp = responses[call_idx % len(responses)]
        call_idx += 1
        return resp

    with _mock_token():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = _get
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await triangulate("RestaurantXYZ", DT, "meal", user_id=USER_ID)

    assert len(result.calendar_events) == 1
    assert "Client lunch" in result.calendar_events[0]


@pytest.mark.asyncio
async def test_triangulate_exception_handled():
    """If an API call raises, triangulate should still return an empty result."""
    with _mock_token():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=RuntimeError("network error"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await triangulate("SomeMerchant", DT, "other", user_id=USER_ID)

    assert result.calendar_events == []
    assert result.email_threads == []
