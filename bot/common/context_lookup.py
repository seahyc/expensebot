"""Receipt triangulation via Gmail + Google Calendar context lookups.

Runs parallel lookups after parsing a receipt to infer business purpose
from calendar events and email threads near the receipt's transaction time.

Uses the Google REST APIs directly with per-user OAuth tokens stored in the DB.
Falls back gracefully (empty results) when a user hasn't connected their Google
account yet.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

_GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

log = logging.getLogger(__name__)

_API_TIMEOUT = 4.0  # seconds per Google API call


@dataclass
class TriangulationResult:
    calendar_events: list[str] = field(default_factory=list)
    email_threads: list[str] = field(default_factory=list)
    inferred_purpose: str | None = None
    confidence: float = 0.0

    def as_markdown(self) -> str | None:
        if not self.calendar_events and not self.email_threads:
            return None
        lines = ["## What I found in your email & calendar", ""]
        if self.calendar_events:
            lines.append("📅 Calendar events near this time:")
            for ev in self.calendar_events:
                lines.append(f"- {ev}")
            lines.append("")
        if self.email_threads:
            lines.append("📧 Relevant emails:")
            for em in self.email_threads:
                lines.append(f"- {em}")
            lines.append("")
        purpose = self.inferred_purpose or "unclear"
        lines.append(f"💡 Inferred purpose: {purpose}")
        return "\n".join(lines)


async def _get_valid_access_token(user_id: int) -> str | None:
    """Return a live access token for user_id, refreshing if needed. None if not connected."""
    from .. import storage

    access, refresh, expiry, _ = storage.get_google_tokens(user_id)
    if not access:
        return None

    now = datetime.now(timezone.utc)
    needs_refresh = expiry is None or expiry <= now + timedelta(minutes=2)

    if needs_refresh and refresh:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": _GOOGLE_CLIENT_ID,
                        "client_secret": _GOOGLE_CLIENT_SECRET,
                        "refresh_token": refresh,
                        "grant_type": "refresh_token",
                    },
                    timeout=_API_TIMEOUT,
                )
            if r.status_code == 200:
                data = r.json()
                new_access = data["access_token"]
                new_expiry = now + timedelta(seconds=data.get("expires_in", 3600))
                storage.set_google_tokens(
                    user_id,
                    access_token=new_access,
                    refresh_token=refresh,
                    expiry=new_expiry,
                    email=storage.get_google_tokens(user_id)[3],
                )
                return new_access
            else:
                log.warning("Google token refresh failed: %s", r.status_code)
                return None
        except Exception as e:
            log.warning("Google token refresh error: %s", e)
            return None

    return access if not needs_refresh else None


async def _get_all_valid_access_tokens(user_id: int) -> list[tuple[str, str]]:
    """Return live (email, access_token) for all connected Google accounts."""
    from .. import storage
    accounts = storage.get_google_accounts(user_id)
    if not accounts:
        return []

    now = datetime.now(timezone.utc)
    results: list[tuple[str, str]] = []

    async with httpx.AsyncClient() as client:
        for acct in accounts:
            email = acct["email"] or ""
            access = acct["access_token"]
            refresh = acct["refresh_token"]
            expiry = acct["expiry"]

            needs_refresh = not access or expiry is None or expiry <= now + timedelta(minutes=2)

            if needs_refresh and refresh:
                try:
                    r = await client.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "client_id": _GOOGLE_CLIENT_ID,
                            "client_secret": _GOOGLE_CLIENT_SECRET,
                            "refresh_token": refresh,
                            "grant_type": "refresh_token",
                        },
                        timeout=_API_TIMEOUT,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        access = data["access_token"]
                        new_expiry = now + timedelta(seconds=data.get("expires_in", 3600))
                        storage.update_google_account_token(user_id, email, access, new_expiry)
                    else:
                        log.warning("Token refresh failed for %s: %s", email, r.status_code)
                        access = None
                except Exception as e:
                    log.warning("Token refresh error for %s: %s", email, e)
                    access = None

            if access:
                results.append((email, access))

    return results


def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _fmt_date_for_gmail(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d")


async def gmail_context(
    merchant: str,
    dt: datetime,
    user_id: int | None = None,
    window_days: int = 3,
) -> list[str]:
    """Search Gmail across all connected Google accounts."""
    if user_id is None:
        return []

    tokens = await _get_all_valid_access_tokens(user_id)
    if not tokens:
        return []

    if merchant:
        after = dt - timedelta(days=window_days)
        before = dt + timedelta(days=window_days)
        query = (
            f"{merchant} "
            f"after:{_fmt_date_for_gmail(after)} "
            f"before:{_fmt_date_for_gmail(before)}"
        )
        max_results = 5
    else:
        after = dt - timedelta(days=window_days if window_days > 3 else 7)
        query = f"after:{_fmt_date_for_gmail(after)}"
        max_results = 10

    seen: set[str] = set()
    results: list[str] = []

    async with httpx.AsyncClient() as client:
        for email, access_token in tokens:
            try:
                r = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/threads",
                    params={"q": query, "maxResults": max_results},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=_API_TIMEOUT,
                )
                if r.status_code != 200:
                    log.warning("Gmail API error for %s: %s", email, r.status_code)
                    continue
                for t in r.json().get("threads", []):
                    tid = t.get("id", "")
                    snippet = t.get("snippet", "")[:120]
                    if tid and tid not in seen and snippet:
                        seen.add(tid)
                        results.append(snippet)
            except Exception as e:
                log.warning("gmail_context error for %s: %s", email, e)

    return results[:max_results * len(tokens)]


async def fetch_gmail_attachment(
    query: str,
    user_id: int,
    preferred_types: tuple[str, ...] = ("application/pdf", "image/"),
) -> tuple[bytes, str, str] | None:
    """Search Gmail for `query`, return the first attachment as (bytes, filename, mime_type).

    Tries messages matching `query`, walks their MIME parts for a file attachment
    whose mime type starts with one of `preferred_types`. Returns None if nothing found.
    """
    import base64

    tokens = await _get_all_valid_access_tokens(user_id)
    if not tokens:
        return None

    async with httpx.AsyncClient() as client:
        for _email, access_token in tokens:
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                # 1. Find messages
                r = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                    params={"q": query, "maxResults": 10},
                    headers=headers,
                    timeout=8.0,
                )
                if r.status_code != 200:
                    continue
                messages = r.json().get("messages", [])
                if not messages:
                    continue

                # 2. Walk each message for an attachment
                for msg_ref in messages:
                    msg_id = msg_ref["id"]
                    mr = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                        params={"format": "full"},
                        headers=headers,
                        timeout=8.0,
                    )
                    if mr.status_code != 200:
                        continue
                    msg = mr.json()

                    # Flatten all MIME parts
                    def _parts(payload: dict) -> list[dict]:
                        parts = []
                        if payload.get("body", {}).get("attachmentId"):
                            parts.append(payload)
                        for p in payload.get("parts", []):
                            parts.extend(_parts(p))
                        return parts

                    for part in _parts(msg.get("payload", {})):
                        mime = part.get("mimeType", "")
                        if not any(mime.startswith(t) for t in preferred_types):
                            continue
                        attach_id = part["body"]["attachmentId"]
                        filename = part.get("filename") or "receipt"
                        ar = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/attachments/{attach_id}",
                            headers=headers,
                            timeout=10.0,
                        )
                        if ar.status_code != 200:
                            continue
                        raw = ar.json().get("data", "")
                        file_bytes = base64.urlsafe_b64decode(raw + "==")
                        return file_bytes, filename, mime
            except Exception as e:
                log.warning("fetch_gmail_attachment error for %s: %s", _email, e)

    return None


async def gcal_context(
    dt: datetime,
    user_id: int | None = None,
    window_hours: float = 2.0,
    broad: bool = False,
) -> list[str]:
    """Fetch Google Calendar events across all connected Google accounts."""
    if user_id is None:
        return []

    tokens = await _get_all_valid_access_tokens(user_id)
    if not tokens:
        return []

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if broad:
        days = max(1, int(window_hours / 24)) if window_hours > 24 else 7
        time_min = _fmt_dt(dt.replace(hour=0, minute=0, second=0, microsecond=0))
        time_max = _fmt_dt(dt + timedelta(days=days))
    else:
        time_min = _fmt_dt(dt - timedelta(hours=window_hours))
        time_max = _fmt_dt(dt + timedelta(hours=window_hours))

    max_results = 20 if broad else 5
    results: list[str] = []

    async with httpx.AsyncClient() as client:
        for email, access_token in tokens:
            try:
                r = await client.get(
                    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                    params={
                        "timeMin": time_min,
                        "timeMax": time_max,
                        "maxResults": max_results,
                        "orderBy": "startTime",
                        "singleEvents": "true",
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=_API_TIMEOUT,
                )
                if r.status_code != 200:
                    log.warning("Calendar API error for %s: %s", email, r.status_code)
                    continue
                for ev in r.json().get("items", []):
                    summary = ev.get("summary") or "(no title)"
                    start = ev.get("start", {})
                    start_time = start.get("dateTime") or start.get("date") or ""
                    if start_time:
                        try:
                            parsed_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                            start_time = parsed_start.strftime("%H:%M")
                        except Exception:
                            start_time = start_time[:10]
                        results.append(f"{summary} at {start_time}")
                    else:
                        results.append(summary)
            except Exception as e:
                log.warning("gcal_context error for %s: %s", email, e)

    return results


def _infer_purpose(
    calendar_events: list[str],
    email_threads: list[str],
    merchant: str,
    receipt_type: str,
) -> tuple[str | None, float]:
    if not calendar_events and not email_threads:
        return None, 0.0

    work_keywords = {
        "meeting", "call", "standup", "sync", "review", "interview",
        "conference", "summit", "workshop", "client", "customer",
        "presentation", "demo", "pitch", "offsite", "team",
    }
    personal_keywords = {"birthday", "anniversary", "vacation", "holiday", "personal"}

    all_text = " ".join(calendar_events + email_threads).lower()
    work_hits = sum(1 for kw in work_keywords if kw in all_text)
    personal_hits = sum(1 for kw in personal_keywords if kw in all_text)

    if work_hits > personal_hits and calendar_events:
        event_title = calendar_events[0].split(" at ")[0]
        confidence = min(0.4 + 0.1 * work_hits, 0.85)
        return f"{receipt_type.title()} for: {event_title}", confidence
    elif email_threads and work_hits > 0:
        confidence = min(0.3 + 0.1 * work_hits, 0.7)
        return "Work-related (see email context)", confidence
    elif calendar_events:
        event_title = calendar_events[0].split(" at ")[0]
        return f"Possibly related to: {event_title}", 0.3
    return None, 0.0


async def triangulate(
    merchant: str,
    dt: datetime,
    receipt_type: str = "other",
    user_id: int | None = None,
) -> TriangulationResult:
    """Run Gmail + Calendar lookups in parallel and return a TriangulationResult."""
    import asyncio

    window_map: dict[str, float] = {
        "transport": 2.0,
        "flight": 12.0,
        "meal": 8.0,
        "hotel": 12.0,
    }
    window_hours = window_map.get(receipt_type, 2.0)

    calendar_events, email_threads = await asyncio.gather(
        gcal_context(dt, user_id=user_id, window_hours=window_hours),
        gmail_context(merchant, dt, user_id=user_id, window_days=3),
        return_exceptions=True,
    )

    if isinstance(calendar_events, Exception):
        log.warning("gcal_context raised: %s", calendar_events)
        calendar_events = []
    if isinstance(email_threads, Exception):
        log.warning("gmail_context raised: %s", email_threads)
        email_threads = []

    inferred_purpose, confidence = _infer_purpose(
        calendar_events, email_threads, merchant, receipt_type
    )

    return TriangulationResult(
        calendar_events=calendar_events,
        email_threads=email_threads,
        inferred_purpose=inferred_purpose,
        confidence=confidence,
    )
