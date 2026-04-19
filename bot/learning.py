"""Self-learning harness: extracts classification patterns from conversations
and persists them to user_md (learned memory) after every N turns or M submits.

Design:
- Non-blocking: fires AFTER the response is sent, never delays the user.
- Isolated: spawns a separate AsyncAnthropic call (not the main agent).
- Capped: max 4 Claude calls per review session.
- Writes via storage.set_user_md() — same format as update_memories tool.
- Turn counter: in-memory dict (resets on restart, intentional).
- Submit counter: persisted in DB (users.submit_count column).
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger("expensebot.learning")

# In-memory turn counter (reset on restart is intentional)
_turn_counts: dict[int, int] = {}

LEARNING_TURN_THRESHOLD = int(os.getenv("LEARNING_TURN_THRESHOLD", "10"))
LEARNING_SUBMIT_THRESHOLD = int(os.getenv("LEARNING_SUBMIT_THRESHOLD", "5"))

# The five section headers that must survive any update
_REQUIRED_HEADERS = [
    "## Classification rules",
    "## Merchant shortcuts",
    "## Defaults",
    "## Description style",
    "## Don't ask me about",
]

_REVIEW_PROMPT_TEMPLATE = """\
You are reviewing recent expense conversations to extract classification patterns worth persisting.

Current memory:
{current_user_md}

Recent conversation:
{formatted_recent_messages}

Task: Identify any NEW classification patterns revealed in this conversation that aren't already in memory. Examples:
- "Grab rides to Changi airport → Travel-International/Transportation"
- "Starbucks in the morning → Meals/Coffee"
- "Don't ask about GST for hawker food receipts"

Rules:
1. Only add patterns that appeared in THIS conversation and aren't already captured.
2. Preserve the exact 5-section structure of the memory (Classification rules, Merchant shortcuts, Defaults, Description style, Don't ask me about).
3. Keep each entry concise: "**Pattern** — why (YYYY-MM-DD)"
4. If there's nothing new to add, reply with exactly: NOTHING_NEW

Reply with either "NOTHING_NEW" or the complete updated memory markdown (all 5 sections).\
"""


def _format_messages(recent_messages: list[dict]) -> str:
    """Format recent messages list into a readable conversation string."""
    if not recent_messages:
        return "(no recent conversation available)"
    lines = []
    for m in recent_messages:
        direction = m.get("direction", "?")
        body = m.get("body", "")
        prefix = "User" if direction == "in" else "Janai"
        lines.append(f"{prefix}: {body}")
    return "\n".join(lines)


def _validate_user_md(markdown: str) -> list[str]:
    """Return list of missing required section headers (empty = valid)."""
    return [h for h in _REQUIRED_HEADERS if h not in markdown]


async def maybe_trigger_review(
    user_id: int,
    db: ModuleType,  # the storage module
    anthropic_client: "AsyncAnthropic",
    recent_messages: list[dict],
    trigger: str = "turn",  # "turn" or "submit"
) -> None:
    """Increment counter and fire background review if threshold reached.

    This function is non-blocking: if threshold is not reached it returns
    immediately. If threshold IS reached it spawns an asyncio task and returns.
    """
    global _turn_counts

    if trigger == "submit":
        # Submit counter is DB-persisted; just check it
        count = db.get_submit_count(user_id)
        if count > 0 and count % LEARNING_SUBMIT_THRESHOLD == 0:
            log.info(
                "learning: submit threshold reached for user=%s (count=%s), spawning review",
                user_id, count,
            )
            asyncio.create_task(
                run_review(user_id, db, anthropic_client, recent_messages)
            )
    else:
        # Turn counter is in-memory
        _turn_counts[user_id] = _turn_counts.get(user_id, 0) + 1
        count = _turn_counts[user_id]
        if count >= LEARNING_TURN_THRESHOLD:
            _turn_counts[user_id] = 0
            log.info(
                "learning: turn threshold reached for user=%s (count=%s), spawning review",
                user_id, count,
            )
            asyncio.create_task(
                run_review(user_id, db, anthropic_client, recent_messages)
            )


async def run_review(
    user_id: int,
    db: ModuleType,
    anthropic_client: "AsyncAnthropic",
    recent_messages: list[dict],
) -> None:
    """Background review: extract new classification patterns, update user_md if changed.

    Steps:
    1. Load current user_md from storage.
    2. Build review prompt.
    3. Call Claude Haiku (cheap and fast).
    4. Parse response: if "NOTHING_NEW" return; otherwise validate + write new user_md.
    5. Max 4 Claude calls total.
    """
    log.info("learning: starting review for user=%s", user_id)
    try:
        current_user_md = db.get_user_md_or_template(user_id)
        formatted = _format_messages(recent_messages)
        prompt = _REVIEW_PROMPT_TEMPLATE.format(
            current_user_md=current_user_md,
            formatted_recent_messages=formatted,
        )

        for attempt in range(4):
            try:
                response = await anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as e:
                log.warning("learning: Claude call failed (attempt %d): %s", attempt + 1, e)
                return

            text = response.content[0].text.strip() if response.content else ""

            if text == "NOTHING_NEW":
                log.info("learning: no new patterns for user=%s (attempt %d)", user_id, attempt + 1)
                return

            if not text:
                log.warning("learning: empty response for user=%s (attempt %d)", user_id, attempt + 1)
                return

            # Validate the proposed update
            missing = _validate_user_md(text)
            if missing:
                log.warning(
                    "learning: proposed user_md missing headers %s for user=%s (attempt %d) — skipping",
                    missing, user_id, attempt + 1,
                )
                # Don't retry on validation failure — the model made a structural error
                return

            # Write the new user_md
            db.set_user_md(user_id, text)
            log.info("learning: user_md updated for user=%s (attempt %d)", user_id, attempt + 1)
            return

        log.warning("learning: exhausted 4 attempts for user=%s without completing", user_id)

    except Exception as e:
        log.exception("learning: unhandled error in run_review for user=%s: %s", user_id, e)
