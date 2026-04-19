"""Receipt triangulation via Gmail + Google Calendar context lookups.

Runs parallel lookups after parsing a receipt to infer business purpose
from calendar events and email threads near the receipt's transaction time.

Uses the `gw` CLI (google-workspace) via subprocess with a hard 2-second
timeout so it never blocks the filing flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Path to gw binary — resolved once at import time.
_GW_PATH: str | None = shutil.which("gw") or "/Users/yingcong/.local/bin/gw"

_SUBPROCESS_TIMEOUT = 2.0  # seconds


@dataclass
class TriangulationResult:
    calendar_events: list[str] = field(default_factory=list)  # event summaries near receipt time
    email_threads: list[str] = field(default_factory=list)    # email subject lines + snippets
    inferred_purpose: str | None = None                        # best guess from context
    confidence: float = 0.0                                    # 0-1

    def as_markdown(self) -> str | None:
        """Return formatted markdown block, or None if nothing was found."""
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


async def _run_gw(*args: str, timeout: float = _SUBPROCESS_TIMEOUT) -> str:
    """Run `gw <args>` as a subprocess. Returns stdout or empty string on any error."""
    if not _GW_PATH:
        log.warning("gw binary not found — skipping context lookup")
        return ""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                _GW_PATH,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("gw subprocess timed out: gw %s", " ".join(args))
            return ""
        if proc.returncode != 0:
            log.warning("gw exited %d: %s", proc.returncode, (stderr or b"").decode()[:200])
            return ""
        return stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        log.warning("gw subprocess start timed out: gw %s", " ".join(args))
        return ""
    except FileNotFoundError:
        log.warning("gw binary not found at %s", _GW_PATH)
        return ""
    except Exception as e:
        log.warning("gw subprocess error: %s", e)
        return ""


def _fmt_dt(dt: datetime) -> str:
    """Format datetime as RFC3339 for gw calendar."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _fmt_date_for_gmail(dt: datetime) -> str:
    """Format date as YYYY/MM/DD for Gmail search."""
    return dt.strftime("%Y/%m/%d")


async def gmail_context(merchant: str, dt: datetime, window_days: int = 3) -> list[str]:
    """Search Gmail for threads mentioning the merchant near the receipt date.

    Returns a list of subject/snippet strings (max 5).
    """
    if not merchant:
        return []
    after = dt - timedelta(days=window_days)
    before = dt + timedelta(days=window_days)
    query = (
        f"{merchant} "
        f"after:{_fmt_date_for_gmail(after)} "
        f"before:{_fmt_date_for_gmail(before)}"
    )
    raw = await _run_gw("gmail", "search", query, "--max-results", "5")
    if not raw.strip():
        return []
    results: list[str] = []
    try:
        data = json.loads(raw)
        messages = data if isinstance(data, list) else data.get("messages", [])
        for msg in messages[:5]:
            subject = msg.get("subject") or msg.get("Subject") or ""
            sender = msg.get("from") or msg.get("From") or ""
            snippet = msg.get("snippet") or ""
            if subject or snippet:
                parts = []
                if subject:
                    parts.append(subject)
                if sender:
                    parts.append(f"from {sender}")
                if snippet and not subject:
                    parts.append(snippet[:80])
                results.append(" — ".join(parts))
    except (json.JSONDecodeError, TypeError):
        # Non-JSON output: parse line by line
        for line in raw.strip().splitlines():
            line = line.strip()
            if line:
                results.append(line[:120])
        results = results[:5]
    return results


async def gcal_context(dt: datetime, window_hours: float = 2.0) -> list[str]:
    """Fetch Google Calendar events within ±window_hours of the receipt time.

    Returns a list of event summary strings (max 5).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    time_min = _fmt_dt(dt - timedelta(hours=window_hours))
    time_max = _fmt_dt(dt + timedelta(hours=window_hours))
    raw = await _run_gw(
        "calendar", "events",
        "--time-min", time_min,
        "--time-max", time_max,
        "--max-results", "5",
    )
    if not raw.strip():
        return []
    results: list[str] = []
    try:
        data = json.loads(raw)
        events = data if isinstance(data, list) else data.get("items", data.get("events", []))
        for ev in events[:5]:
            summary = ev.get("summary") or ev.get("title") or "(no title)"
            start = ev.get("start", {})
            start_time = start.get("dateTime") or start.get("date") or ""
            if start_time:
                # Trim to HH:MM if it's a full ISO datetime
                try:
                    parsed_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    start_time = parsed_start.strftime("%H:%M")
                except Exception:
                    start_time = start_time[:10]
                results.append(f"{summary} at {start_time}")
            else:
                results.append(summary)
    except (json.JSONDecodeError, TypeError):
        for line in raw.strip().splitlines():
            line = line.strip()
            if line:
                results.append(line[:120])
        results = results[:5]
    return results


def _infer_purpose(
    calendar_events: list[str],
    email_threads: list[str],
    merchant: str,
    receipt_type: str,
) -> tuple[str | None, float]:
    """Simple heuristic to infer business purpose from found context."""
    if not calendar_events and not email_threads:
        return None, 0.0

    # Look for work-related keywords in calendar events
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
        # Use first calendar event as the purpose hint
        event_title = calendar_events[0].split(" at ")[0]
        confidence = min(0.4 + 0.1 * work_hits, 0.85)
        return f"{receipt_type.title()} for: {event_title}", confidence
    elif email_threads and work_hits > 0:
        confidence = min(0.3 + 0.1 * work_hits, 0.7)
        return "Work-related (see email context)", confidence
    elif calendar_events:
        # Has events but no strong signal
        event_title = calendar_events[0].split(" at ")[0]
        return f"Possibly related to: {event_title}", 0.3
    return None, 0.0


async def triangulate(
    merchant: str,
    dt: datetime,
    receipt_type: str = "other",
) -> TriangulationResult:
    """Run Gmail + Calendar lookups in parallel and return a TriangulationResult.

    receipt_type adjusts the calendar window:
      - "transport" → ±2h
      - "flight"    → full-day (12h each side)
      - "meal"      → same-day (8h each side)
      - "hotel"     → same-day (12h each side, check-in biased)
      - default     → ±2h
    """
    window_map: dict[str, float] = {
        "transport": 2.0,
        "flight": 12.0,
        "meal": 8.0,
        "hotel": 12.0,
    }
    window_hours = window_map.get(receipt_type, 2.0)

    cal_task = asyncio.create_task(gcal_context(dt, window_hours=window_hours))
    gmail_task = asyncio.create_task(gmail_context(merchant, dt, window_days=3))

    calendar_events, email_threads = await asyncio.gather(
        cal_task, gmail_task, return_exceptions=True
    )

    # Exceptions from gather return as exception objects — treat as empty
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
