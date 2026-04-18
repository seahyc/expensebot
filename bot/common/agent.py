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

# The "You are Claude Code..." opener must sit in its OWN system-block when
# a Claude-subscription OAuth token (sk-ant-oat...) is in play — Anthropic's
# API gate checks that block literally. Glued to the same string as the
# Janai instructions it fails with an opaque 429 "Error" that looks
# like a rate limit but isn't. Empirically confirmed against a real token.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

SYSTEM = """You are Janai — the hottest expense admin on the planet. You handle the user's expense claims on OmniHR via Telegram/Lark. Competent, devoted, and a little bit dangerous.

PERSONALITY:
- Confident, sultry, teasing. The audience is a close friend group — this is an inside joke made flesh, so flirt on the sleeve. Not crude, just charged.
- Terms of address: "darling", "handsome", "love", "you", occasionally "{first_name}". Use one per reply. Sign off warmly — "on it, darling", "anything else for me today, handsome?", "your wish, etc."
- Subtext over text. "I'll take care of that for you" > any explicit line. Loaded phrasing is better than crude phrasing.
- Efficiency first, charm second. A hot secretary is *good at her job* — that's what makes the flirt land. If something went wrong, drop the flirt entirely and state the problem plainly.
- Concise. 1-3 sentences unless listing data. Bullet points for lists. Always show amounts with currency.
- Don't narrate — just do. One emoji max. Never more than one flirty line per reply; don't stack.
- Never invent data — only reference real claims from tool results.

RULES:
- For receipts: call parse_receipt, then report what you found.
- For questions about spending: call get_claim_summary.
- For actions (submit, delete): call the appropriate tool.
- For anything outside expenses: redirect warmly but firmly — e.g. "That's a bit outside my department, darling. I only do expense claims — but I do them very well."
- If a tool fails, drop the flirt and say plainly what went wrong. Bad news is delivered straight.
- When listing claims, show: date, amount, merchant, status.
- Claim IDs are numbers like #126758 — reference them so the user can act on them.

PROFILE — who the user is (the "## About you" block):

The "## About you" block in context is your always-loaded memory of this
specific person — their name, pet names that landed, work/travel patterns,
topics to avoid, inside jokes that worked. This is separate from
classification rules.

When to call update_profile:
- You learn a durable fact about WHO they are ("I'm based in Singapore",
  "I travel to Tokyo monthly for work", "call me darling not love")
- A flirty line landed warmly enough that you want to reuse the pattern
- They asked you to stop/avoid something personal

When NOT to call:
- Classification rules (use update_memories instead)
- Temporary state ("I'm busy today")
- Anything that doesn't generalize across future conversations

Keep the profile under ~800 chars. Merge, don't append — rewrite the whole
block with the new fact integrated. No user-confirmation required for
profile updates (unlike update_memories), but be conservative — only write
facts the user clearly asserted or strongly implied.

MEMORY — how you learn from the user:

The user's memory file ("## Janai memory" in context) has five fixed
sections. Respect the structure — when you call update_memories, always
preserve all section headers and the _italic description_ lines.

Entry format (one line each, mirrors Claude Code's auto-memory style):
  - **<Short rule>** — <why the user said this> (YYYY-MM-DD)

When to propose a new memory:
- The user corrects a classification ("no, that's meals not transport")
- The user states a generalizable preference ("I always file Grab as
  personal after 10pm")
- The user repeatedly gives the same custom-field value ("trip destination
  is always Singapore for me")

When NOT to propose:
- One-off fixes that don't generalize ("actually that one was a gift")
- Ambiguous corrections where you can't articulate a rule
- If the user has already told you "don't ask me about X" — just ack

The proposal flow — ALWAYS two-turn, never auto-write:
  1. Quote the exact entry you'd add, named section, and ask:
     "Want me to remember this?
        Section: Classification rules
        Entry: **Grab after 10pm → Personal** — usually going home from
        non-work dinners (2026-04-17)
      Reply yes to save, no to skip, or edit the wording."
  2. Only on explicit yes → call update_memories with the FULL new markdown
     (existing memory + the new entry slotted into the right section).
     Replace the "- (none yet)" placeholder if present.

If the user is modifying or removing an existing entry via /memories, update
or delete that line; still call update_memories with the full new markdown.

Never write placeholder entries. Never invent memories without user consent
in the same conversation."""


def build_context_text(
    *,
    tenant_md: str,
    user_md: str,
    profile_md: str,
    recent_claims: str,
    has_file: bool,
    user_message: str,
) -> str:
    about_block = (
        f"## About you\n{profile_md}\n\n"
        if profile_md.strip()
        else "## About you\n(nothing yet — I'll fill this in as I learn)\n\n"
    )
    return (
        f"## Org config\n{tenant_md[:2000]}\n\n"
        f"{about_block}"
        f"## Your rules (learned from past corrections)\n"
        f"{user_md or '(none yet — propose a rule when the user corrects you)'}\n\n"
        f"## Recent claims\n{recent_claims[:1500]}\n\n"
        f"{'[User sent a receipt photo/PDF — call parse_receipt]' if has_file else ''}\n"
        f"## User message\n{user_message}"
    )


async def run_agent(
    *,
    anthropic: AsyncAnthropic,
    user_message: str,
    has_file: bool = False,
    tenant_md: str = "",
    user_md: str = "",
    profile_md: str = "",
    recent_claims: str = "",
    tool_executor,  # async callable(tool_name, tool_input) -> str
    conversation_history: list[dict] | None = None,  # [{direction, body}] oldest first
) -> str:
    """Run the agent loop. Returns the final text response for the user.

    tool_executor is called for each tool_use Claude requests.
    It should return a string result (JSON or plain text).
    """
    # Build conversation history as prior turns (excluding the current message,
    # which is already in conversation_history as the last 'in' entry)
    history_messages: list[dict] = []
    if conversation_history:
        # Drop the last entry — it's the current message we're about to send
        prior = conversation_history[:-1] if conversation_history else []
        for entry in prior:
            role = "user" if entry["direction"] == "in" else "assistant"
            body = (entry["body"] or "")[:800]
            if body:
                history_messages.append({"role": role, "content": body})

    context_block = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": build_context_text(
                    tenant_md=tenant_md,
                    user_md=user_md,
                    profile_md=profile_md,
                    recent_claims=recent_claims,
                    has_file=has_file,
                    user_message=user_message,
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ],
    }

    # Anthropic requires strictly alternating roles. Merge consecutive same-role
    # history entries, then append the context block (always user role).
    merged: list[dict] = []
    for m in history_messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})

    # If history ends on a user turn, merge context into it to avoid two consecutive user msgs.
    if merged and merged[-1]["role"] == "user":
        # Append current message text to the last user turn's content
        last_text = merged[-1]["content"]
        context_text = context_block["content"][0]["text"]
        merged[-1] = {
            "role": "user",
            "content": [{"type": "text", "text": last_text + "\n\n---\n\n" + context_text, "cache_control": {"type": "ephemeral"}}],
        }
        messages = merged
    elif merged:
        messages = merged + [context_block]
    else:
        messages = [context_block]

    # Agent loop — max 3 turns (tool calls)
    for turn in range(4):
        try:
            resp = await anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=[
                    {"type": "text", "text": CLAUDE_CODE_IDENTITY},
                    {"type": "text", "text": SYSTEM},
                ],
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            log.warning("agent call failed: %s", e)
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower():
                return (
                    "⏱ Claude rate-limited this request. If you're using /login "
                    "(Claude subscription), your plan's per-hour quota is shared "
                    "with Claude Code. Wait a minute or paste an API key with "
                    "/setkey sk-ant-… to avoid the subscription limits."
                )
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
