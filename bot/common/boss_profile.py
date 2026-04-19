"""Build and refresh the "secretary's briefing" — a persistent summary of
everything Janai knows about a user, synthesised from:
  - Full OmniHR claims history
  - Last 90 days of Gmail threads
  - Last 90 days of Google Calendar events

Stored in users.boss_profile_md. Injected into every agent context block
so Janai always knows who she's talking to.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_BRIEF_PROMPT = """\
You are building a private "secretary's briefing" about a person based on
their expense history, emails, and calendar. This briefing is for an AI
expense assistant (Janai) so she can act like a secretary who really knows
her boss — anticipating needs, remembering patterns, never asking obvious
questions twice.

Write in second person ("You travel to Jakarta monthly…", "Your usual
Grab route is home ↔ Changi…"). Keep it under 1200 characters.

Structure:
- **Role & base**: job title/team if inferable, home city, office location
- **Travel patterns**: frequent destinations, typical trip length, airlines/hotels preferred
- **Expense habits**: top merchants, categories, average spend, any quirks
- **Work rhythm**: recurring meetings, clients, team events visible in calendar
- **Useful context**: anything that would help pre-fill expense forms

Be factual. Only include what's clearly supported by the data. Skip
sections if there's no data for them. Do not mention the data sources."""


async def build_boss_profile(
    *,
    user_id: int,
    omnihr_claims: list[dict],
    first_name: str = "",
    anthropic_client,  # AsyncAnthropic
) -> str:
    """Synthesise a boss profile from all available data. Returns markdown string."""
    from .context_lookup import gmail_context, gcal_context

    now = datetime.now(timezone.utc)
    ninety_days_ago = now - timedelta(days=90)

    # Gather Gmail + Calendar in parallel (best-effort — no merchant filter for gmail)
    gmail_threads: list[str] = []
    cal_events: list[str] = []
    try:
        gmail_threads, cal_events = await asyncio.gather(
            _bulk_gmail(user_id=user_id, since=ninety_days_ago),
            _bulk_gcal(user_id=user_id, since=ninety_days_ago, until=now + timedelta(days=30)),
            return_exceptions=True,
        )
        if isinstance(gmail_threads, Exception):
            log.warning("boss_profile gmail error: %s", gmail_threads)
            gmail_threads = []
        if isinstance(cal_events, Exception):
            log.warning("boss_profile gcal error: %s", cal_events)
            cal_events = []
    except Exception as e:
        log.warning("boss_profile gather error: %s", e)

    # Format claims
    claims_text = _format_claims(omnihr_claims)
    gmail_text = "\n".join(f"- {t}" for t in gmail_threads[:40]) or "(none)"
    cal_text = "\n".join(f"- {e}" for e in cal_events[:40]) or "(none)"

    if not claims_text and gmail_text == "(none)" and cal_text == "(none)":
        return ""

    user_data = f"""
## Expense history ({len(omnihr_claims)} claims)
{claims_text}

## Recent emails (last 90 days, sample)
{gmail_text}

## Calendar events (next 30 days + last 90 days, sample)
{cal_text}
""".strip()

    try:
        resp = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=_BRIEF_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Name: {first_name or 'unknown'}\n\n{user_data}",
                }
            ],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning("boss_profile LLM error: %s", e)
        return ""


async def refresh_boss_profile(
    *,
    user_id: int,
    omnihr_http_client,   # httpx.AsyncClient already authenticated to OmniHR
    tenant_id: str,
    first_name: str = "",
    anthropic_client,
) -> str:
    """Fetch all OmniHR claims, build profile, persist to DB. Returns the new profile."""
    from .. import storage

    claims = await _fetch_all_claims(omnihr_http_client, tenant_id)
    profile = await build_boss_profile(
        user_id=user_id,
        omnihr_claims=claims,
        first_name=first_name,
        anthropic_client=anthropic_client,
    )
    if profile:
        storage.set_boss_profile_md(user_id, profile)
        log.info("boss_profile refreshed for user=%s (%d chars)", user_id, len(profile))
    return profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_all_claims(http_client, tenant_id: str) -> list[dict]:
    """Paginate OmniHR claims — fetch up to 200 most recent."""
    try:
        r = await http_client.get(
            f"/api/v1/expenses/claims/",
            params={"page_size": 200, "status__in": "1,2,3,5,8"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("results", [])
    except Exception as e:
        log.warning("_fetch_all_claims error: %s", e)
        return []


def _format_claims(claims: list[dict]) -> str:
    if not claims:
        return "(no claims on record)"
    lines = []
    for c in claims[:100]:
        date = c.get("receipt_date") or c.get("created_at", "")[:10]
        merchant = c.get("merchant_name") or c.get("merchant") or "?"
        amount = c.get("amount") or ""
        currency = c.get("currency") or ""
        category = c.get("policy_name") or c.get("category") or ""
        lines.append(f"- {date} | {merchant} | {currency}{amount} | {category}")
    return "\n".join(lines)


async def _bulk_gmail(*, user_id: int, since: datetime) -> list[str]:
    """Fetch recent Gmail threads with broad expense-related queries."""
    from .context_lookup import _get_valid_access_token
    import httpx

    access_token = await _get_valid_access_token(user_id)
    if not access_token:
        return []

    queries = [
        "receipt OR invoice OR order confirmation",
        "hotel OR flight OR booking",
        "expense OR reimbursement",
    ]
    seen: set[str] = set()
    results: list[str] = []
    after = since.strftime("%Y/%m/%d")

    async with httpx.AsyncClient() as client:
        for q in queries:
            try:
                r = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/threads",
                    params={"q": f"{q} after:{after}", "maxResults": 20},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=6.0,
                )
                if r.status_code != 200:
                    continue
                for t in r.json().get("threads", []):
                    tid = t.get("id", "")
                    snippet = t.get("snippet", "")[:100]
                    if tid and tid not in seen and snippet:
                        seen.add(tid)
                        results.append(snippet)
            except Exception as e:
                log.warning("_bulk_gmail query '%s' error: %s", q, e)

    return results


async def _bulk_gcal(*, user_id: int, since: datetime, until: datetime) -> list[str]:
    """Fetch calendar events over a wide window."""
    from .context_lookup import _get_valid_access_token, _fmt_dt
    import httpx

    access_token = await _get_valid_access_token(user_id)
    if not access_token:
        return []

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                params={
                    "timeMin": _fmt_dt(since),
                    "timeMax": _fmt_dt(until),
                    "maxResults": 50,
                    "orderBy": "startTime",
                    "singleEvents": "true",
                },
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=8.0,
            )
        if r.status_code != 200:
            return []
        events = r.json().get("items", [])
        results = []
        for ev in events:
            summary = ev.get("summary") or "(no title)"
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date") or ""
            if start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    start = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    start = start[:10]
            results.append(f"{summary} @ {start}")
        return results
    except Exception as e:
        log.warning("_bulk_gcal error: %s", e)
        return []
