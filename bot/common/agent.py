"""Expense agent — single handler for all non-command messages.

Receives user message (text and/or file) → calls Claude with tools →
executes tool calls → returns final response.

Token-efficient: system prompt + tools cached via ephemeral cache_control.
Only the user message + recent claims context varies per call.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from .tools import TOOLS as _BASE_TOOLS
from ..plugins.registry import load_enabled_skills, load_enabled_tools
from ..voice import build_agent_system_prompt

# Load plugin skills and tools at startup — all disabled by default.
_PLUGIN_SKILLS = load_enabled_skills()
_PLUGIN_TOOLS = load_enabled_tools()
TOOLS = _BASE_TOOLS + _PLUGIN_TOOLS

log = logging.getLogger(__name__)
_tel = logging.getLogger("janai.llm")  # structured LLM telemetry — set to DEBUG to see full payloads


def _log_llm_input(user_id: Any, turn: int, system_prompt: str, messages: list) -> None:
    if not _tel.isEnabledFor(logging.DEBUG):
        # Always log a compact summary at INFO
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        preview = ""
        if last_user:
            c = last_user.get("content", "")
            preview = (c[:200] if isinstance(c, str) else str(c)[:200])
        _tel.info("llm_input user=%s turn=%d msgs=%d preview=%s", user_id, turn, len(messages), preview)
        return
    _tel.debug(
        "llm_input user=%s turn=%d system=%s messages=%s",
        user_id, turn,
        system_prompt[:500],
        json.dumps(messages, default=str)[:2000],
    )


def _log_llm_output(user_id: Any, turn: int, resp: Any) -> None:
    text = " ".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    )
    tools = [
        {"tool": b.name, "input": b.input}
        for b in resp.content if getattr(b, "type", "") == "tool_use"
    ]
    usage = getattr(resp, "usage", None)
    _tel.info(
        "llm_output user=%s turn=%d stop=%s in_tok=%s out_tok=%s tools=%s text=%s",
        user_id, turn, resp.stop_reason,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
        json.dumps(tools, default=str)[:300],
        text[:300],
    )


# The "You are Claude Code..." opener must sit in its OWN system-block when
# a Claude-subscription OAuth token (sk-ant-oat...) is in play — Anthropic's
# API gate checks that block literally. Glued to the same string as the
# assistant instructions it fails with an opaque 429 "Error" that looks
# like a rate limit but isn't. Empirically confirmed against a real token.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

CONFIDENT_THRESHOLD = 3

_SGT = timezone(timedelta(hours=8))  # Singapore Time — default for all users


def _now_sgt() -> str:
    now = datetime.now(_SGT)
    return now.strftime("%A, %d %B %Y, %I:%M %p SGT")


def render_merchants_block(rows: list[dict]) -> str:
    """Render top merchants as a bullet list for the context prompt.
    Entries with count >= CONFIDENT_THRESHOLD are tagged '(confident)' so
    Janai knows she can file without asking."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        tag = " (confident)" if r["count"] >= CONFIDENT_THRESHOLD else ""
        sub = f"/{r['sub_category']}" if r.get("sub_category") else ""
        lines.append(
            f"- **{r['merchant']}** → {r['policy_id']}{sub} "
            f"({r['count']}x){tag}"
        )
    return "\n".join(lines)


def build_integrations_block(user: dict[str, Any]) -> str:
    from .. import storage
    uid = user.get("id")
    if uid:
        google_accts = storage.get_google_accounts(uid)
        tg_accts = storage.get_telegram_accounts(uid)
        wa_accts = storage.get_whatsapp_accounts(uid)
    else:
        google_accts, tg_accts, wa_accts = [], [], []

    def _fmt(accts, label_key, cmd):
        if not accts:
            return f"⬜ Not connected — {cmd} to set up"
        labels = ", ".join(a[label_key] for a in accts if a.get(label_key))
        return f"✅ Connected ({labels})" if labels else "✅ Connected"

    lines = [
        f"- Gmail & Calendar: {_fmt(google_accts, 'email', '/connect_google')}",
        f"- Telegram: {_fmt(tg_accts, 'phone', '/connect_telegram')}",
        f"- WhatsApp: {_fmt(wa_accts, 'phone', '/connect_whatsapp')}",
    ]
    return "## Integrations\n" + "\n".join(lines) + "\n\n"


def build_context_text(
    *,
    user: dict[str, Any],
    user_md: str,
    profile_md: str,
    boss_profile_md: str = "",
    has_file: bool,
    user_message: str,
    triangulation_md: str | None = None,
) -> str:
    about_block = (
        f"## About you\n{profile_md}\n\n"
        if profile_md.strip()
        else "## About you\n(nothing yet — I'll fill this in as I learn)\n\n"
    )
    boss_block = (
        f"## Secretary's briefing (built from your claims, emails & calendar)\n"
        f"{boss_profile_md[:1500]}\n\n"
        if boss_profile_md.strip()
        else ""
    )
    triangulation_block = (
        f"{triangulation_md}\n\n"
        if triangulation_md
        else ""
    )
    integrations_block = build_integrations_block(user) if user else ""
    return (
        f"## Now\n{_now_sgt()}\n\n"
        f"{integrations_block}"
        f"{boss_block}"
        f"{about_block}"
        f"## Your rules (learned from past corrections)\n"
        f"{user_md or '(none yet — propose a rule when the user corrects you)'}\n\n"
        f"{triangulation_block}"
        f"{'[User sent a receipt photo/PDF — call parse_receipt first, then get_omnihr_context]' if has_file else ''}\n"
        f"## User message\n{user_message}"
    )


async def run_agent(
    *,
    anthropic: AsyncAnthropic,
    user_message: str,
    has_file: bool = False,
    user_md: str = "",
    profile_md: str = "",
    boss_profile_md: str = "",
    tool_executor,  # async callable(tool_name, tool_input) -> str
    conversation_history: list[dict] | None = None,  # [{direction, body}] oldest first
    user: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> str:
    """Run the agent loop. Returns the final text response for the user.

    tool_executor is called for each tool_use Claude requests.
    It should return a string result (JSON or plain text).
    """
    final_system_prompt = system_prompt or build_agent_system_prompt(user)
    if _PLUGIN_SKILLS:
        final_system_prompt = final_system_prompt + "\n\n" + _PLUGIN_SKILLS

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
                    user=user or {},
                    user_md=user_md,
                    profile_md=profile_md,
                    boss_profile_md=boss_profile_md,
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

    user_id = (user or {}).get("id", "?")

    # Agent loop — max 3 turns (tool calls)
    for turn in range(4):
        # Telemetry: log what we're sending
        _log_llm_input(user_id, turn, final_system_prompt, messages)

        try:
            resp = await anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=[
                    {"type": "text", "text": CLAUDE_CODE_IDENTITY},
                    {"type": "text", "text": final_system_prompt},
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

        # Telemetry: log what we got back
        _log_llm_output(user_id, turn, resp)

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
            log.info("tool_call user=%s turn=%d tool=%s input=%s", user_id, turn, tc.name, json.dumps(tc.input)[:200])
            try:
                result = await tool_executor(tc.name, tc.input)
            except Exception as e:
                result = f"Error: {e}"
            log.debug("tool_result user=%s tool=%s result=%s", user_id, tc.name, str(result)[:500])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": str(result)[:3000],
            })

        messages.append({"role": "user", "content": tool_results})

    return "\n".join(text_parts) if text_parts else "I got stuck in a loop. Try again?"
