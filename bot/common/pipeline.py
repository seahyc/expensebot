"""End-to-end receipt-to-draft pipeline. Channel-agnostic.

Flow (with eager parallelism):
  1. SHA256 the file. Already-parsed in cache? → return cached parse + skip Claude.
  2. Concurrently:
       a. parse_receipt() (Claude)
       b. fetch active trip
       c. fetch existing OmniHR drafts/submissions (last 60 days) for dupe check
       d. fetch user's recent corrections context
  3. Resolve schema for the parsed policy (cached).
  4. Build values dict, file as draft (with retry on schema drift).
  5. Optionally cross-check parsed-tuple against OmniHR submissions for dupes.
  6. Return PipelineResult — channel handler formats the user-facing message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from omnihr_client.client import OmniHRClient, create_draft_with_retry
from omnihr_client.exceptions import ValidationError

from .parser import ParsedReceipt, parse_receipt

log = logging.getLogger(__name__)


@dataclass
class DupeHint:
    submission_id: int
    receipt_date: date
    amount: str
    merchant: str | None
    status: int


@dataclass
class PipelineResult:
    parsed: ParsedReceipt
    draft_id: int | None
    draft_response: dict[str, Any] | None
    file_dupes: list[DupeHint]    # exact file SHA matches
    parsed_dupes: list[DupeHint]  # same merchant+date+amount on OmniHR
    needs_user_input: list[str]   # field names below confidence threshold
    error: str | None = None


CONFIDENCE_THRESHOLD = 0.7


async def file_receipt(
    *,
    user_db_id: int,
    omnihr: OmniHRClient,
    anthropic: AsyncAnthropic,
    file_bytes: bytes,
    media_type: str,
    tenant_md: str,
    user_md: str,
    user_note: str = "",
    parse_cache,        # ParseCache protocol — get_by_sha / put
    submissions_cache,  # SubmissionsCache protocol — fresh-list of recent submissions
    trip_store,         # TripStore — active trip lookup
    auto_file: bool = True,
) -> PipelineResult:
    sha = hashlib.sha256(file_bytes).hexdigest()

    # 1. Local SHA cache hit?
    cached_parse = await parse_cache.get_by_sha(user_db_id, sha)

    # 2. Parallel: parse + active trip + recent submissions
    async with asyncio.TaskGroup() as tg:
        if cached_parse is None:
            recent_summary_task = tg.create_task(_recent_claims_summary(submissions_cache, user_db_id))
            trip_task = tg.create_task(trip_store.active(user_db_id))
            recent_subs_task = tg.create_task(submissions_cache.recent(user_db_id, days=60))
        else:
            trip_task = tg.create_task(trip_store.active(user_db_id))
            recent_subs_task = tg.create_task(submissions_cache.recent(user_db_id, days=60))

    if cached_parse is not None:
        parsed = cached_parse
    else:
        parsed = await parse_receipt(
            anthropic=anthropic,
            file_bytes=file_bytes,
            media_type=media_type,
            tenant_md=tenant_md,
            user_md=user_md,
            recent_claims_summary=recent_summary_task.result(),
            active_trip=trip_task.result(),
        )
        await parse_cache.put(user_db_id, sha, parsed)

    # File-SHA dupes (already filed before)
    file_dupes = await parse_cache.dupes(user_db_id, sha)

    # Parsed-tuple dupes (different file, same merchant + date + amount)
    parsed_dupes = match_dupes(parsed, recent_subs_task.result())

    needs_input = _low_confidence_fields(parsed)

    if not parsed.is_receipt:
        return PipelineResult(
            parsed=parsed,
            draft_id=None,
            draft_response=None,
            file_dupes=file_dupes,
            parsed_dupes=parsed_dupes,
            needs_user_input=["not_a_receipt"],
        )

    if not auto_file:
        return PipelineResult(
            parsed=parsed,
            draft_id=None,
            draft_response=None,
            file_dupes=file_dupes,
            parsed_dupes=parsed_dupes,
            needs_user_input=needs_input,
        )

    if needs_input:
        # Don't auto-file uncertain. Bot will prompt user.
        return PipelineResult(
            parsed=parsed,
            draft_id=None,
            draft_response=None,
            file_dupes=file_dupes,
            parsed_dupes=parsed_dupes,
            needs_user_input=needs_input,
        )

    # Build values dict
    if not parsed.suggested_policy_id or not parsed.amount or not parsed.receipt_date:
        return PipelineResult(
            parsed=parsed,
            draft_id=None,
            draft_response=None,
            file_dupes=file_dupes,
            parsed_dupes=parsed_dupes,
            needs_user_input=["policy_or_amount_or_date"],
            error="parser returned None for required fields",
        )

    # Upload PDF first (we need a Path; channel handler should pass via tmp file
    # or we can refactor upload_document to accept bytes — keep it path-based for now)
    raise NotImplementedError(
        "Wire upload_document to accept bytes; then build values dict and "
        "call create_draft_with_retry. See README build phases."
    )


def _low_confidence_fields(p: ParsedReceipt) -> list[str]:
    out = []
    for k in ("amount", "receipt_date", "merchant"):
        c = p.confidence.get(k, 0.0)
        if c < CONFIDENCE_THRESHOLD:
            out.append(k)
    return out


def match_dupes(parsed: ParsedReceipt, recent_subs: list[dict[str, Any]]) -> list[DupeHint]:
    if not parsed.amount or not parsed.receipt_date:
        return []
    out = []
    for s in recent_subs:
        if (
            s.get("amount") == parsed.amount
            and s.get("receipt_date") == parsed.receipt_date.isoformat()
            and (s.get("merchant") or "").lower() == (parsed.merchant or "").lower()
        ):
            out.append(
                DupeHint(
                    submission_id=s["id"],
                    receipt_date=date.fromisoformat(s["receipt_date"]),
                    amount=s["amount"],
                    merchant=s.get("merchant"),
                    status=s.get("status", 0),
                )
            )
    return out


def format_dupe_warning(hints: list[DupeHint]) -> str:
    """Format dupe hints as a prompt-ready warning block for the agent.
    Returned string is empty when there are no dupes — caller can concat
    unconditionally."""
    if not hints:
        return ""
    lines = ["⚠ POSSIBLE DUPLICATE(S) — same amount/date/merchant already on OmniHR:"]
    for h in hints:
        # Merchant is user-controlled data echoed into the agent's tool result.
        # Strip control chars + cap length so a crafted merchant name can't
        # inject a fake SYSTEM line via embedded newlines.
        safe_merchant = (h.merchant or "?").replace("\n", " ").replace("\r", " ")[:80]
        lines.append(
            f"- #{h.submission_id} {h.receipt_date.isoformat()} "
            f"{safe_merchant} {h.amount} (status={h.status})"
        )
    lines.append(
        "If this is the same transaction, warn the user before filing. "
        "If they confirm it's a separate charge, proceed."
    )
    return "\n".join(lines)


async def _recent_claims_summary(submissions_cache, user_db_id: int) -> str:
    subs = await submissions_cache.recent(user_db_id, days=30)
    if not subs:
        return "(no recent claims)"
    lines = []
    for s in subs[:10]:
        lines.append(
            f"- {s.get('receipt_date')} {s.get('merchant') or '?'} "
            f"{s.get('amount')} {s.get('amount_currency','?')} "
            f"({s.get('policy', {}).get('name','?')}) status={s.get('status')}"
        )
    return "\n".join(lines)
