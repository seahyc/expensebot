"""Receipt parser. Single Claude call, structured output via tool use.

Inputs:
  - file bytes (image or PDF)
  - tenant.md (curated rules)
  - user.md (per-user rules)
  - last N claims for context
  - active trip context (if any)

Output (one ParsedReceipt):
  - is_receipt, confidence_per_field
  - merchant, date, amount, currency
  - suggested policy_id, suggested sub_category_id
  - custom_fields dict (label -> value)
  - description draft
  - duplicate_likelihood: low | medium | high
  - anomalies: list[str]
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)


@dataclass
class ParsedReceipt:
    is_receipt: bool
    confidence: dict[str, float]  # per-field, 0-1
    merchant: str | None = None
    receipt_date: date | None = None
    amount: str | None = None  # decimal string e.g. "27.80"
    currency: str | None = None  # ISO 4217 e.g. "SGD"
    suggested_policy_id: int | None = None
    suggested_sub_category_id: int | None = None
    suggested_sub_category_label: str | None = None
    custom_fields: dict[str, Any] = field(default_factory=dict)
    description_draft: str = ""
    duplicate_likelihood: str = "low"
    anomalies: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


PARSE_TOOL = {
    "name": "file_receipt",
    "description": (
        "Extract structured data from a receipt and classify it into the user's "
        "OmniHR tenant policies. Be conservative with confidence."
    ),
    "input_schema": {
        "type": "object",
        "required": ["is_receipt", "confidence"],
        "properties": {
            "is_receipt": {"type": "boolean"},
            "confidence": {
                "type": "object",
                "description": "0-1 confidence per field.",
                "additionalProperties": {"type": "number"},
            },
            "merchant": {"type": ["string", "null"]},
            "receipt_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "amount": {"type": ["string", "null"], "description": "decimal string"},
            "currency": {"type": ["string", "null"], "description": "ISO 4217"},
            "suggested_policy_id": {"type": ["integer", "null"]},
            "suggested_sub_category_label": {"type": ["string", "null"]},
            "custom_fields": {
                "type": "object",
                "additionalProperties": True,
                "description": "Keyed by custom-field label (case-insensitive).",
            },
            "description_draft": {"type": "string"},
            "duplicate_likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
            "anomalies": {"type": "array", "items": {"type": "string"}},
        },
    },
}


SYSTEM_PROMPT = """You parse expense receipts and classify them for filing.

Return a single tool call with structured fields. Rules:
- Be conservative on confidence: below 0.7 = bot will ask user to confirm
- Respect the tenant's classification rules and user's preferences
- When a sub-category fits a heuristic in the rules, use it
- If uncertain, pick the most plausible match and note it in anomalies
- For non-receipts (screenshots, selfies, irrelevant photos): is_receipt=false
- Never make up data — if you can't read a field, leave it null with low confidence"""


async def parse_receipt(
    *,
    anthropic: AsyncAnthropic,
    file_bytes: bytes,
    media_type: str,  # "image/jpeg", "image/png", "application/pdf"
    tenant_md: str,
    user_md: str,
    recent_claims_summary: str,
    active_trip: str | None = None,
) -> ParsedReceipt:
    """Single inference. Cached prompt prefix (tenant.md + user.md + recent claims).

    Pricing: with prompt cache, marginal cost ~$0.005 per receipt at Sonnet 4.5.
    """
    cached_context = (
        "## Tenant rules\n\n"
        + tenant_md
        + "\n\n## Your preferences\n\n"
        + user_md
        + "\n\n## Recent claims (for context + dupe hints)\n\n"
        + recent_claims_summary
    )

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": cached_context,
                    "cache_control": {"type": "ephemeral"},
                },
                _file_block(file_bytes, media_type),
                {
                    "type": "text",
                    "text": (
                        f"Active trip context: {active_trip}\n"
                        if active_trip
                        else "No active trip.\n"
                    )
                    + "Parse this receipt. Use the tool.",
                },
            ],
        }
    ]

    # Retry with backoff for 429 (Claude subscription tokens have tight rate limits)
    import asyncio as _aio
    last_err = None
    for attempt in range(4):
        try:
            resp = await anthropic.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[PARSE_TOOL],
                tool_choice={"type": "tool", "name": "file_receipt"},
                messages=messages,
            )
            break
        except Exception as e:
            last_err = e
            if "429" in str(e) or "rate_limit" in str(e).lower():
                wait = (2 ** attempt) * 5  # 5, 10, 20, 40s
                log.warning("rate limited, retry %d in %ds", attempt + 1, wait)
                await _aio.sleep(wait)
            else:
                raise
    else:
        raise last_err  # type: ignore[misc]

    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_use:
        raise RuntimeError(f"Model did not call file_receipt tool: {resp}")
    raw = tool_use.input

    return _to_parsed_receipt(raw)


def _file_block(file_bytes: bytes, media_type: str) -> dict[str, Any]:
    b64 = base64.b64encode(file_bytes).decode()
    # Detect PDF by magic bytes — MIME from Telegram is unreliable
    is_pdf = file_bytes[:5] == b"%PDF-" or media_type == "application/pdf"
    if is_pdf:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    # Default to image. Normalize common MIME variants Claude accepts.
    img_mime = media_type if media_type in ("image/jpeg", "image/png", "image/gif", "image/webp") else "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": img_mime, "data": b64},
    }


def _to_parsed_receipt(raw: dict[str, Any]) -> ParsedReceipt:
    rd = raw.get("receipt_date")
    parsed_date = None
    if rd:
        try:
            parsed_date = date.fromisoformat(rd)
        except ValueError:
            log.warning("Bad receipt_date from model: %r", rd)
    return ParsedReceipt(
        is_receipt=bool(raw.get("is_receipt")),
        confidence=raw.get("confidence") or {},
        merchant=raw.get("merchant"),
        receipt_date=parsed_date,
        amount=raw.get("amount"),
        currency=raw.get("currency"),
        suggested_policy_id=raw.get("suggested_policy_id"),
        suggested_sub_category_label=raw.get("suggested_sub_category_label"),
        custom_fields=raw.get("custom_fields") or {},
        description_draft=raw.get("description_draft") or "",
        duplicate_likelihood=raw.get("duplicate_likelihood") or "low",
        anomalies=raw.get("anomalies") or [],
        raw=raw,
    )
