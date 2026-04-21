"""Receipt parser using Claude Agent SDK (subscription auth, no API key needed).

Saves the receipt to a temp file, asks Claude Code to parse it via the agent SDK,
extracts structured JSON from the response.

Falls back to the direct API parser (parser.py) if the agent SDK fails.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


async def parse_receipt_via_agent(
    *,
    file_bytes: bytes,
    media_type: str,
    filename: str,
    tenant_md: str,
    user_md: str,
    recent_claims_summary: str,
    active_trip: str | None = None,
) -> dict[str, Any] | None:
    """Parse a receipt using the Claude Agent SDK (subscription auth).

    Returns the parsed dict (same shape as ParsedReceipt.raw) or None on failure.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock

    # Save file to temp location for Claude Code to read
    ext = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(media_type, ".bin")
    if file_bytes[:5] == b"%PDF-":
        ext = ".pdf"

    with tempfile.NamedTemporaryFile(suffix=ext, prefix="receipt_", delete=False) as f:
        f.write(file_bytes)
        tmp_path = f.name

    try:
        prompt = _build_prompt(tmp_path, filename, tenant_md, user_md, recent_claims_summary, active_trip)

        options = ClaudeAgentOptions(
            max_turns=1,
            system_prompt=(
                "You parse expense receipts. Read the attached file and return ONLY a JSON object "
                "(no markdown fencing, no explanation) with these fields:\n"
                "is_receipt, confidence (object with per-field 0-1), merchant, receipt_date (YYYY-MM-DD), "
                "amount (decimal string), currency (ISO 4217), suggested_policy_id (int or null), "
                "suggested_sub_category_label (string or null), custom_fields (object keyed by field label), "
                "description_draft (string), duplicate_likelihood (low/medium/high), anomalies (list of strings).\n\n"
                "Be conservative with confidence. If you can't read a field clearly, set confidence below 0.7."
            ),
            permission_mode="acceptEdits",  # non-interactive
        )

        result_text = ""
        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for event in client.receive_response():
                if isinstance(event, ResultMessage):
                    for block in event.content or []:
                        if isinstance(block, TextBlock):
                            result_text += block.text

        if not result_text.strip():
            log.warning("Agent SDK returned empty result")
            return None

        # Extract JSON from response (might be wrapped in markdown code block)
        parsed = _extract_json(result_text)
        if parsed:
            log.info("Agent SDK parsed receipt: merchant=%s amount=%s", parsed.get("merchant"), parsed.get("amount"))
        return parsed

    except Exception as e:
        log.warning("Agent SDK parse failed: %s", e)
        return None
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass


def _build_prompt(
    file_path: str,
    filename: str,
    tenant_md: str,
    user_md: str,
    recent_claims_summary: str,
    active_trip: str | None,
) -> str:
    parts = [
        f"Parse the receipt file at: {file_path} (original name: {filename})",
        "",
        "## Tenant classification rules",
        tenant_md[:2000],
        "",
        "## User preferences",
        user_md[:500],
        "",
        "## Recent claims (for context + dupe detection)",
        recent_claims_summary[:1000],
    ]
    if active_trip:
        parts.extend(["", f"## Active trip context: {active_trip}"])
    parts.extend([
        "",
        "Read the file and return ONLY the JSON object. No explanation.",
    ])
    return "\n".join(parts)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from text that might have markdown fencing."""
    # Try direct parse first
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Try extracting from code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Last resort: find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    log.warning("Could not extract JSON from agent response: %s", text[:200])
    return None
