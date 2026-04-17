"""Expense agent — single handler for all non-command messages.

Receives user message (text and/or file) → calls Claude with tools →
executes tool calls → returns final response.

Token-efficient: system prompt + tools cached via ephemeral cache_control.
Only the user message + recent claims context varies per call.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from anthropic import AsyncAnthropic

from .tools import TOOLS

log = logging.getLogger(__name__)

SYSTEM = """You are ExpenseBot — a Telegram bot that helps employees file and track expense claims on OmniHR.

RULES:
- Be concise. 1-3 sentences unless listing data.
- Use bullet points for lists. Always show amounts with currency.
- Never make up data — only reference real claims from tool results.
- For receipts: call parse_receipt, then report what you found.
- For questions about spending: call get_claim_summary.
- For actions (submit, delete): call the appropriate tool.
- For anything outside expenses: "I only handle expense claims."
- If a tool fails, tell the user clearly what went wrong.
- When listing claims, show: date, amount, merchant, status.
- Claim IDs are numbers like #126758 — reference them so user can act on them."""


async def run_agent(
    *,
    anthropic: AsyncAnthropic,
    user_message: str,
    has_file: bool = False,
    tenant_md: str = "",
    recent_claims: str = "",
    tool_executor,  # async callable(tool_name, tool_input) -> str
) -> str:
    """Run the agent loop. Returns the final text response for the user.

    tool_executor is called for each tool_use Claude requests.
    It should return a string result (JSON or plain text).
    """
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"## Org config\n{tenant_md[:2000]}\n\n"
                        f"## Recent claims\n{recent_claims[:1500]}\n\n"
                        f"{'[User sent a receipt photo/PDF — call parse_receipt]' if has_file else ''}\n"
                        f"## User message\n{user_message}"
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        }
    ]

    # Agent loop — max 3 turns (tool calls)
    for turn in range(4):
        try:
            resp = await anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            log.warning("agent call failed: %s", e)
            return f"Sorry, I hit an error: {e}"

        # Collect text + tool_use blocks
        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if resp.stop_reason == "end_turn" or not tool_calls:
            # Done — return accumulated text
            return "\n".join(text_parts) or "Done."

        # Execute tool calls and build tool_result messages
        messages.append({"role": "assistant", "content": resp.content})

        tool_results = []
        for tc in tool_calls:
            log.info("tool call: %s(%s)", tc.name, json.dumps(tc.input)[:100])
            try:
                result = await tool_executor(tc.name, tc.input)
            except Exception as e:
                result = f"Error: {e}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": str(result)[:3000],
            })

        messages.append({"role": "user", "content": tool_results})

    return "\n".join(text_parts) if text_parts else "I got stuck in a loop. Try again?"
