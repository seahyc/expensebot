"""Periodic heartbeat runner for Janai.

A background tick fires every HEARTBEAT_INTERVAL minutes during active hours
(HEARTBEAT_ACTIVE_START–HEARTBEAT_ACTIVE_END SGT). Each tick loads task
definitions from HEARTBEAT.md, checks which tasks are due per-user (via the
nudges cooldown table), and runs a lightweight isolated Claude call for each
due task. If Claude's response is not "HEARTBEAT_OK", a Telegram message is
sent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import json

from . import storage
from .voice import voice_for_user

log = logging.getLogger(__name__)

SGT = timezone(timedelta(hours=8))
HEARTBEAT_MD = Path(__file__).parent / "skills" / "HEARTBEAT.md"

ACTIVE_HOURS_START = int(os.getenv("HEARTBEAT_ACTIVE_START", "8"))    # 8am SGT
ACTIVE_HOURS_END   = int(os.getenv("HEARTBEAT_ACTIVE_END", "22"))     # 10pm SGT
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "30"))

# Tool subset available to heartbeat tasks
_HEARTBEAT_TOOLS = [
    {
        "name": "list_claims",
        "description": (
            "List the user's expense claims from OmniHR. Can filter by status "
            "(draft, submitted, approved, reimbursed, all) and/or date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["all", "draft", "submitted", "approved", "reimbursed"],
                    "description": "Filter by claim status. Default: all",
                },
                "month": {
                    "type": "string",
                    "description": "Filter by month, e.g. 'apr', 'march', '2026-04'. Optional.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_email_context",
        "description": (
            "Search Gmail for emails matching a query. Use this for the gmail_receipts "
            "task. Pass the full Gmail search query in the 'query' field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query, e.g. 'is:unread subject:receipt newer_than:1d'",
                },
            },
            "required": ["query"],
        },
    },
]

HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_TURNS = 2


class HeartbeatRunner:
    def __init__(self, telegram_bot, anthropic_factory, omnihr_factory):
        """
        telegram_bot: python-telegram-bot Bot instance
        anthropic_factory: async callable(user_dict) -> AsyncAnthropic client
        omnihr_factory: async callable(user_dict) -> OmniHRClient (unused for now,
                        list_claims goes through the tool executor which uses client_for)
        """
        self.bot = telegram_bot
        self.anthropic_factory = anthropic_factory
        self.omnihr_factory = omnihr_factory
        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self.scheduler.add_job(
            self._tick_all_users,
            "interval",
            minutes=HEARTBEAT_INTERVAL,
            id="heartbeat",
            misfire_grace_time=60,
        )
        self.scheduler.start()
        log.info(
            "Heartbeat started (every %dm, active %d-%d SGT)",
            HEARTBEAT_INTERVAL, ACTIVE_HOURS_START, ACTIVE_HOURS_END,
        )

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_active_hours(self) -> bool:
        now = datetime.now(SGT)
        return ACTIVE_HOURS_START <= now.hour < ACTIVE_HOURS_END

    def _load_tasks(self) -> list[dict]:
        """Parse HEARTBEAT.md YAML tasks block."""
        text = HEARTBEAT_MD.read_text()
        data = yaml.safe_load(text)
        return data.get("tasks", [])

    async def _tick_all_users(self) -> None:
        if not self._is_active_hours():
            log.debug("Heartbeat tick skipped — outside active hours")
            return
        try:
            tasks = self._load_tasks()
        except Exception:
            log.exception("Failed to load HEARTBEAT tasks — skipping tick")
            return
        users = storage.list_active_users()
        log.info("Heartbeat tick: %d active user(s)", len(users))
        for user in users:
            try:
                await self._tick_user(user, tasks)
            except Exception:
                log.exception("Heartbeat tick failed for user %s", user["id"])

    async def _tick_user(self, user: dict, tasks: list[dict]) -> None:
        for task in tasks:
            task_id = task["id"]
            every_hours = _parse_every(task["every"])
            if storage.was_nudged_recently(user["id"], hook=f"heartbeat_{task_id}", within_hours=every_hours):
                log.debug("Heartbeat task %s skipped for user %s (cooldown)", task_id, user["id"])
                continue
            response = await self._run_task(user, task)
            if response is None:
                continue
            response = response.strip()
            if response == "HEARTBEAT_OK":
                log.info("HEARTBEAT_OK user=%s task=%s", user["id"], task_id)
                continue
            # Actionable — send Telegram message
            tg_id: str | None = None
            if user.get("channel") == "telegram":
                tg_id = user.get("channel_user_id")
            if tg_id:
                try:
                    await self.bot.send_message(
                        chat_id=int(tg_id),
                        text=response,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                    storage.record_nudge(
                        user["id"],
                        hook=f"heartbeat_{task_id}",
                        message_preview=response[:100],
                    )
                    log.info("Heartbeat message sent: user=%s task=%s", user["id"], task_id)
                except Exception:
                    log.exception("Failed to send heartbeat message for user=%s task=%s", user["id"], task_id)
            else:
                log.info(
                    "Heartbeat task %s actionable for user %s but no Telegram channel",
                    task_id, user["id"],
                )

    async def _run_task(self, user: dict, task: dict) -> str | None:
        """Run a single heartbeat task as an isolated Claude call.

        Returns Claude's text response, or None on error.
        """
        profile_md = storage.get_profile_md(user["id"])
        system = (
            f"{voice_for_user(user).agent_system}\n\n"
            "You are running a background health check — not a conversation.\n"
            "- Call the appropriate tool(s) to check the requested condition.\n"
            "- If something needs the user's attention, reply with a concise actionable message.\n"
            "- If nothing needs attention, reply with exactly: HEARTBEAT_OK\n"
            "Do NOT add preamble. HEARTBEAT_OK means silence — the user will not see it."
        )
        if profile_md:
            system = f"{system}\n\n## User profile\n{profile_md}"

        try:
            anth = await self.anthropic_factory(user)
        except Exception:
            log.exception("Could not build Anthropic client for user=%s", user["id"])
            return None

        messages: list[dict] = [{"role": "user", "content": task["prompt"]}]

        tool_executor = _build_heartbeat_tool_executor(user, self.omnihr_factory)

        for _turn in range(MAX_TOOL_TURNS + 1):
            try:
                resp = await anth.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=512,
                    system=system,
                    tools=_HEARTBEAT_TOOLS,
                    messages=messages,
                )
            except Exception:
                log.exception("Heartbeat Claude call failed for user=%s task=%s", user["id"], task["id"])
                return None

            # Check stop reason
            if resp.stop_reason == "end_turn":
                # Extract text response
                for block in resp.content:
                    if hasattr(block, "text"):
                        return block.text
                return "HEARTBEAT_OK"

            if resp.stop_reason == "tool_use":
                # Append assistant turn
                messages.append({"role": "assistant", "content": resp.content})
                # Execute tools
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        result = await tool_executor(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                else:
                    # No tools to run — shouldn't happen, but break safely
                    break
            else:
                # Unexpected stop reason
                log.warning(
                    "Unexpected stop_reason=%s for heartbeat user=%s task=%s",
                    resp.stop_reason, user["id"], task["id"],
                )
                break

        # Final pass: extract text from last assistant message if any
        if messages and messages[-1]["role"] == "assistant":
            content = messages[-1]["content"]
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "text"):
                        return block.text
        return "HEARTBEAT_OK"


# ------------------------------------------------------------------
# Tool executor for heartbeat (subset: list_claims + search_email_context)
# ------------------------------------------------------------------

def _build_heartbeat_tool_executor(user: dict, omnihr_factory):
    """Build a lightweight tool executor for heartbeat tasks."""

    async def execute(tool_name: str, tool_input: dict) -> str:
        if tool_name == "list_claims":
            # Import here to avoid circular import at module load
            from .server import client_for
            from omnihr_client.client import (
                ACTIVE_STATUS_FILTERS, FILTER_SHORTCUTS, STATUS_LABELS,
            )
            status_key = tool_input.get("status", "all")
            filters = FILTER_SHORTCUTS.get(status_key, ACTIVE_STATUS_FILTERS)
            try:
                async with client_for(user) as client:
                    data = await client.list_submissions(status_filters=filters, page_size=15)
                rows = data.get("results", [])
                if not rows:
                    return f"No {status_key} claims found."
                lines = []
                for r in rows:
                    sl = STATUS_LABELS.get(r.get("status", 0), "?")
                    lines.append(
                        f"#{r['id']} {r.get('receipt_date','?')} "
                        f"{r.get('amount_currency','?')} {r.get('amount','?')} "
                        f"{r.get('merchant') or '?'} [{sl}] "
                        f"{(r.get('description') or '')[:50]}"
                    )
                return "\n".join(lines)
            except Exception as e:
                log.warning("list_claims failed in heartbeat for user=%s: %s", user["id"], e)
                return f"Error listing claims: {e}"

        elif tool_name == "search_email_context":
            query = tool_input.get("query", "")
            if not query:
                return "No query provided."
            try:
                # Use _run_gw directly for a free-form Gmail query
                from .common.context_lookup import _run_gw
                raw = await _run_gw("gmail", "search", query, "--max-results", "5")
                if not raw.strip():
                    return "No matching emails found."
                try:
                    data = json.loads(raw)
                    messages = data if isinstance(data, list) else data.get("messages", [])
                    results = []
                    for msg in messages[:5]:
                        subject = msg.get("subject") or msg.get("Subject") or ""
                        sender = msg.get("from") or msg.get("From") or ""
                        if subject:
                            entry = subject
                            if sender:
                                entry += f" (from {sender})"
                            results.append(entry)
                    return "\n".join(results) if results else "No matching emails found."
                except (json.JSONDecodeError, TypeError):
                    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
                    return "\n".join(lines[:5]) if lines else "No matching emails found."
            except Exception as e:
                log.warning("search_email_context failed in heartbeat for user=%s: %s", user["id"], e)
                return f"Error searching Gmail: {e}"

        return f"Unknown tool: {tool_name}"

    return execute


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _parse_every(every_str: str) -> float:
    """Parse '24h', '30m' etc into hours (float)."""
    m = re.match(r"(\d+)([hm])", every_str)
    if not m:
        return 24.0
    n, unit = int(m.group(1)), m.group(2)
    return float(n) if unit == "h" else n / 60.0
