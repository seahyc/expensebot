"""Tests for tool-turn capture/replay in bot/common/agent.py.

Covers the memory fix: run_agent must (a) return captured tool_use/tool_result
blocks so callers can persist them, and (b) replay them on the next turn so
the LLM sees facts it already pulled via tools instead of re-calling.

Edge cases exercised:
- no-tool turn returns (text, None)
- single tool-call turn captures one assistant + one user block
- multi-tool round captures both sides
- history with prior tool_turns injects blocks into messages sent to the API
- history entry with corrupt tool_turns JSON falls back gracefully (no crash)
- history ending on 'in' entry merges context into the user turn
- same-role history entries get merged (block-form when mixed with text)
- max-turn exhaustion still returns the captured turns
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.common import agent as agent_mod
from bot.common.agent import run_agent


# ---------------------------------------------------------------------------
# Anthropic response builders
# ---------------------------------------------------------------------------

def _text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(tool_id: str, name: str, tool_input: dict):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def _resp(blocks, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


class _AnthStub:
    """Scriptable Anthropic mock: returns queued responses in order,
    and records every messages.create call for assertion."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=AsyncMock(side_effect=self._next))

    async def _next(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Minimal fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def user():
    return {"id": 1, "channel": "telegram", "channel_user_id": "111"}


@pytest.fixture
def dummy_executor():
    async def _exec(name, tool_input):
        return f"result-for-{name}"
    return _exec


# ---------------------------------------------------------------------------
# Return-signature tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_tool_call_returns_none_tool_turns(user, dummy_executor):
    """Simplest turn: immediate end_turn with no tool calls → tool_turns is None."""
    anth = _AnthStub([_resp([_text_block("hello darling")])])

    reply, tool_turns = await run_agent(
        anthropic=anth,
        user_message="hi",
        tool_executor=dummy_executor,
        user=user,
        system_prompt="stub",
    )

    assert reply == "hello darling"
    assert tool_turns is None


@pytest.mark.asyncio
async def test_single_tool_call_captures_assistant_and_user_blocks(user, dummy_executor):
    """One tool call then end_turn → tool_turns JSON holds [assistant(tool_use), user(tool_result)]."""
    anth = _AnthStub([
        _resp([_tool_use_block("tu1", "search_email_context", {"merchant": "Ryde"})],
              stop_reason="tool_use"),
        _resp([_text_block("Found SGD 15.25")]),
    ])

    reply, tool_turns = await run_agent(
        anthropic=anth,
        user_message="ryde receipt?",
        tool_executor=dummy_executor,
        user=user,
        system_prompt="stub",
    )

    assert reply == "Found SGD 15.25"
    assert tool_turns is not None
    blocks = json.loads(tool_turns)
    assert len(blocks) == 2
    assert blocks[0]["role"] == "assistant"
    assert blocks[0]["content"][0]["type"] == "tool_use"
    assert blocks[0]["content"][0]["name"] == "search_email_context"
    assert blocks[0]["content"][0]["input"] == {"merchant": "Ryde"}
    assert blocks[1]["role"] == "user"
    assert blocks[1]["content"][0]["type"] == "tool_result"
    assert blocks[1]["content"][0]["tool_use_id"] == "tu1"
    assert "search_email_context" in blocks[1]["content"][0]["content"]


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_turn_captured(user, dummy_executor):
    """If the model issues two tool_use blocks in one assistant turn, both tool_results
    live in a single user block — captured_turns must keep that shape."""
    anth = _AnthStub([
        _resp([
            _tool_use_block("a", "get_omnihr_context", {}),
            _tool_use_block("b", "search_email_context", {"merchant": "Ryde"}),
        ], stop_reason="tool_use"),
        _resp([_text_block("done")]),
    ])

    reply, tool_turns = await run_agent(
        anthropic=anth, user_message="go", tool_executor=dummy_executor,
        user=user, system_prompt="stub",
    )

    assert reply == "done"
    blocks = json.loads(tool_turns)
    assert len(blocks) == 2
    assert len(blocks[0]["content"]) == 2  # two tool_use blocks
    assert len(blocks[1]["content"]) == 2  # two tool_results


# ---------------------------------------------------------------------------
# Replay-into-messages tests (the actual memory-fix behavior)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prior_tool_turns_injected_into_history(user, dummy_executor):
    """Key behavior: a prior 'out' entry with tool_turns must be spliced into the
    messages list sent to Anthropic, so the LLM sees the full tool_use/tool_result
    trail from the previous turn — not just the summary text."""
    prior_tool_turns = json.dumps([
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu0", "name": "search_email_context",
             "input": {"merchant": "Ryde"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu0",
             "content": "From: ryde@ryde.sg, amount SGD 15.25, msg_id=abc123"},
        ]},
    ])
    history = [
        {"direction": "in", "body": "how about ryde?", "tool_turns": None},
        {"direction": "out", "body": "SGD 15.25, Ryde", "tool_turns": prior_tool_turns},
        {"direction": "in", "body": "yeah file it", "tool_turns": None},
    ]

    anth = _AnthStub([_resp([_text_block("filed")])])

    await run_agent(
        anthropic=anth, user_message="yeah file it", tool_executor=dummy_executor,
        user=user, system_prompt="stub", conversation_history=history,
    )

    sent_messages = anth.calls[0]["messages"]
    # Flatten all content — the tool_result content string from the prior turn
    # must be present somewhere so the LLM can see "msg_id=abc123".
    flat = json.dumps(sent_messages)
    assert "msg_id=abc123" in flat
    assert "tu0" in flat
    assert "search_email_context" in flat


@pytest.mark.asyncio
async def test_corrupt_tool_turns_json_does_not_crash(user, dummy_executor, caplog):
    """If the stored tool_turns JSON is malformed (e.g. schema drift), the agent
    must degrade gracefully to text-only history rather than hard-failing a turn."""
    history = [
        {"direction": "in", "body": "prior", "tool_turns": None},
        {"direction": "out", "body": "hi", "tool_turns": "this is not json {["},
        {"direction": "in", "body": "now", "tool_turns": None},
    ]

    anth = _AnthStub([_resp([_text_block("ok")])])
    reply, _ = await run_agent(
        anthropic=anth, user_message="now", tool_executor=dummy_executor,
        user=user, system_prompt="stub", conversation_history=history,
    )

    assert reply == "ok"
    # Text body should still be in history sent to the API
    sent = json.dumps(anth.calls[0]["messages"])
    assert "hi" in sent


@pytest.mark.asyncio
async def test_tool_turns_json_array_but_wrong_shape_ignored(user, dummy_executor):
    """tool_turns that's valid JSON but not a list (e.g. object) should be ignored,
    not spliced in as garbage content."""
    history = [
        {"direction": "in", "body": "prior", "tool_turns": None},
        {"direction": "out", "body": "hi", "tool_turns": '{"not": "a list"}'},
        {"direction": "in", "body": "now", "tool_turns": None},
    ]

    anth = _AnthStub([_resp([_text_block("ok")])])
    reply, _ = await run_agent(
        anthropic=anth, user_message="now", tool_executor=dummy_executor,
        user=user, system_prompt="stub", conversation_history=history,
    )

    assert reply == "ok"
    sent = json.dumps(anth.calls[0]["messages"])
    assert '"not": "a list"' not in sent


# ---------------------------------------------------------------------------
# Merge / alternation edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_ending_on_in_turn_merges_context_into_user(user, dummy_executor):
    """When the last prior entry is 'in' text (no intervening assistant reply),
    the current context must be merged into that same user turn to preserve
    strict user/assistant alternation Anthropic requires."""
    # Note: conversation_history includes the CURRENT message as the last 'in' entry,
    # which run_agent drops. So to end on 'in' in the PRIOR slice, we need two
    # consecutive 'in' entries at the tail.
    history = [
        {"direction": "out", "body": "hi", "tool_turns": None},
        {"direction": "in", "body": "orphaned user msg", "tool_turns": None},
        {"direction": "in", "body": "current", "tool_turns": None},
    ]

    anth = _AnthStub([_resp([_text_block("ok")])])
    await run_agent(
        anthropic=anth, user_message="current", tool_executor=dummy_executor,
        user=user, system_prompt="stub", conversation_history=history,
    )

    sent = anth.calls[0]["messages"]
    roles = [m["role"] for m in sent]
    # Strict alternation required
    for i in range(1, len(roles)):
        assert roles[i] != roles[i - 1], f"adjacent same-role at {i}: {roles}"
    # Last message must be user (carries the current context block)
    assert roles[-1] == "user"


@pytest.mark.asyncio
async def test_consecutive_in_with_replayed_tool_turns_maintains_alternation(user, dummy_executor):
    """Two consecutive 'out' entries where one carries replayed tool blocks — the
    merge logic must upgrade text-string content to block form and concatenate,
    not throw TypeError."""
    prior_tool_turns = json.dumps([
        {"role": "assistant", "content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "id": "x", "name": "noop", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "ok"},
        ]},
    ])
    history = [
        {"direction": "in", "body": "first", "tool_turns": None},
        {"direction": "out", "body": "reply A", "tool_turns": prior_tool_turns},
        {"direction": "out", "body": "reply B (no tools)", "tool_turns": None},
        {"direction": "in", "body": "current", "tool_turns": None},
    ]

    anth = _AnthStub([_resp([_text_block("ok")])])
    # Should not raise
    await run_agent(
        anthropic=anth, user_message="current", tool_executor=dummy_executor,
        user=user, system_prompt="stub", conversation_history=history,
    )

    sent = anth.calls[0]["messages"]
    roles = [m["role"] for m in sent]
    for i in range(1, len(roles)):
        assert roles[i] != roles[i - 1], f"adjacent same-role at {i}: {roles}"


# ---------------------------------------------------------------------------
# Max-turn exhaustion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_exhaustion_still_returns_captured_turns(user, dummy_executor):
    """If the agent loop hits its 4-turn cap without end_turn, it must still
    return the captured tool_turns so they get persisted (otherwise rerunning
    the same prompt would start from zero context)."""
    # 4 consecutive tool_use responses — loop never reaches end_turn
    tool_turn_response = lambda i: _resp(
        [_tool_use_block(f"t{i}", "noop", {})], stop_reason="tool_use"
    )
    anth = _AnthStub([tool_turn_response(i) for i in range(4)])

    reply, tool_turns = await run_agent(
        anthropic=anth, user_message="go", tool_executor=dummy_executor,
        user=user, system_prompt="stub",
    )

    # Loop-exhaustion returns fallback text, but captured turns should be populated
    assert tool_turns is not None
    blocks = json.loads(tool_turns)
    assert len(blocks) == 8  # 4 × (assistant + user)


# ---------------------------------------------------------------------------
# Backwards-compat: missing tool_turns key in history rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_without_tool_turns_key_still_works(user, dummy_executor):
    """Old messages logged before the column existed won't have tool_turns in
    their dict. Replay must tolerate missing key (not just None value)."""
    history = [
        {"direction": "in", "body": "old"},  # no tool_turns key at all
        {"direction": "out", "body": "old reply"},
        {"direction": "in", "body": "current"},
    ]

    anth = _AnthStub([_resp([_text_block("ok")])])
    reply, _ = await run_agent(
        anthropic=anth, user_message="current", tool_executor=dummy_executor,
        user=user, system_prompt="stub", conversation_history=history,
    )
    assert reply == "ok"
