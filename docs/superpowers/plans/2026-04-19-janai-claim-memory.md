# Janai Claim Memory Upgrades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Janai from "follows rules you tell her" into "remembers who you are and anticipates your claims" by adding a core-memory profile block, a merchant-choice learning table, and duplicate-receipt warnings at parse time.

**Architecture:** Three independent features that share one retrieval surface (the agent's `context_block` in `bot/common/agent.py`). Feature 1 (profile) is a Letta-style always-in-context block the model rewrites via a new tool. Feature 2 (merchant memory) is an auto-learned table populated when `submit_claim` succeeds, rendered as a "merchants you've filed" section in context. Feature 3 (dupe sniff) wires the existing `_match_dupes` logic from `pipeline.py` into the `parse_receipt` tool path so results carry a warning the agent surfaces. Each feature ships as its own commit and can be deployed independently.

**Tech Stack:** Python 3.11+, SQLite (via `bot/storage.py`), Anthropic Python SDK (tool use), pytest for tests.

---

## File Structure

**New files:**
- `tests/test_profile.py` — profile CRUD + tool executor tests
- `tests/test_merchant_memory.py` — merchant memory CRUD + context rendering
- `tests/test_dupe_sniff.py` — dupe matching + parse_receipt wiring

**Modified files:**
- `bot/storage.py` — add profile column + merchant_choices table + CRUD helpers
- `bot/common/tools.py` — add `update_profile` tool schema
- `bot/common/agent.py` — inject profile + merchants into context_block; update SYSTEM prompt
- `bot/server.py:1043-1141` — add tool executor branch for `update_profile`; add merchant-record hook in `submit_claim` branch; add dupe warnings to `parse_receipt` branch

---

## Task 1: Core Profile Block (Letta-style always-in-context)

Adds a per-user `profile_md` column — a ~1KB freeform markdown block describing who the user is (name, pet names accepted, work patterns, in-jokes that landed, forbidden topics). Janai rewrites it via a new tool when she learns something durable. It sits alongside `user_md` (rules) in the context block — profile = *who*, user_md = *how to classify*.

**Files:**
- Modify: `bot/storage.py` (add migration + `get_profile_md` / `set_profile_md`)
- Modify: `bot/common/tools.py` (add `update_profile` schema)
- Modify: `bot/common/agent.py` (SYSTEM prompt + context_block)
- Modify: `bot/server.py:1097` (new tool branch)
- Create: `tests/test_profile.py`

### Steps

- [ ] **Step 1.1: Write failing test for profile CRUD**

```python
# tests/test_profile.py
import pytest
from pathlib import Path
import tempfile
from bot import storage

@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        monkeypatch.setattr(storage, "DB_PATH", path)
        storage.init_db(path)
        yield path

def test_profile_default_empty(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    assert storage.get_profile_md(uid) == ""

def test_profile_roundtrip(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    storage.set_profile_md(uid, "# About you\n- Prefers 'darling'\n- Travels often\n")
    assert "darling" in storage.get_profile_md(uid)

def test_profile_isolated_per_user(tmp_db):
    a = storage.upsert_user("telegram", "a")
    b = storage.upsert_user("telegram", "b")
    storage.set_profile_md(a, "A's profile")
    storage.set_profile_md(b, "B's profile")
    assert storage.get_profile_md(a) == "A's profile"
    assert storage.get_profile_md(b) == "B's profile"
```

- [ ] **Step 1.2: Run test — expect failure**

Run: `pytest tests/test_profile.py -v`
Expected: FAIL — `AttributeError: module 'bot.storage' has no attribute 'get_profile_md'`

- [ ] **Step 1.3: Add column + CRUD to storage.py**

In `bot/storage.py`, append to `_ADD_COLS` list (around line 105):

```python
    ("users", "profile_md", "TEXT"),
```

Then add below `get_user_md_or_template` (around line 268):

```python
def get_profile_md(user_id: int) -> str:
    """Return the always-in-context 'who is this user' markdown block.
    Empty string for fresh users — the agent fills it as she learns."""
    with db() as conn:
        row = conn.execute("SELECT profile_md FROM users WHERE id=?", (user_id,)).fetchone()
        return (row["profile_md"] if row else "") or ""


def set_profile_md(user_id: int, markdown: str) -> None:
    """Persist the core-memory profile block — called from update_profile tool."""
    with db() as conn:
        conn.execute("UPDATE users SET profile_md=? WHERE id=?", (markdown, user_id))
```

- [ ] **Step 1.4: Run test — expect pass**

Run: `pytest tests/test_profile.py -v`
Expected: PASS (3/3)

- [ ] **Step 1.5: Add update_profile tool schema**

In `bot/common/tools.py`, append to the `TOOLS` list before the closing `]`:

```python
    {
        "name": "update_profile",
        "description": (
            "Rewrite your always-in-context memory of WHO this user is — name, "
            "pet names they respond to, work/travel patterns, in-jokes that landed, "
            "topics to avoid. This is NOT for classification rules (use update_memories "
            "for those). Call when you learn a DURABLE fact about the person, not a one-off. "
            "Takes the full replacement markdown — keep it under ~800 chars, bullet "
            "points, no section headers needed. Merge new facts into the existing "
            "block rather than appending blindly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "new_profile_md": {
                    "type": "string",
                    "description": "The full replacement profile markdown.",
                },
                "change_summary": {
                    "type": "string",
                    "description": "One short line, e.g. 'Added: travels Singapore-Tokyo weekly'.",
                },
            },
            "required": ["new_profile_md", "change_summary"],
        },
    },
```

- [ ] **Step 1.6: Write failing test for context + tool executor**

Add to `tests/test_profile.py`:

```python
def test_context_block_includes_profile(tmp_db, monkeypatch):
    """The agent's context_block must include profile_md when present."""
    from bot.common import agent
    uid = storage.upsert_user("telegram", "u1")
    storage.set_profile_md(uid, "- Name: Ying\n- Always flies SIA")

    # We don't run the full agent; we just check the helper we'll extract.
    block = agent.build_context_text(
        tenant_md="tenant",
        user_md="rules",
        profile_md=storage.get_profile_md(uid),
        recent_claims="",
        has_file=False,
        user_message="hi",
    )
    assert "Ying" in block
    assert "SIA" in block
    assert "## About you" in block
```

- [ ] **Step 1.7: Run test — expect failure**

Run: `pytest tests/test_profile.py::test_context_block_includes_profile -v`
Expected: FAIL — `AttributeError: module 'bot.common.agent' has no attribute 'build_context_text'`

- [ ] **Step 1.8: Extract + update build_context_text in agent.py**

In `bot/common/agent.py`, extract the context-text construction into a helper above `run_agent` (around line 88):

```python
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
```

Then change the `run_agent` signature to accept `profile_md: str = ""` (add after `user_md: str = ""`), and replace the `context_block` assignment (lines ~117-133) with:

```python
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
```

- [ ] **Step 1.9: Run test — expect pass**

Run: `pytest tests/test_profile.py -v`
Expected: PASS (4/4)

- [ ] **Step 1.10: Wire the new tool branch in server.py**

In `bot/server.py`, add a branch inside `execute` (after `update_memories` branch, around line 1119):

```python
        elif tool_name == "update_profile":
            new_profile = tool_input.get("new_profile_md", "")
            change = tool_input.get("change_summary", "")
            if len(new_profile) > 2000:
                return "Refused: profile too long (>2000 chars). Trim before saving."
            storage.set_profile_md(u["id"], new_profile)
            log.info("profile updated for user=%s: %s", u["id"], change)
            return f"Saved. Summary: {change}"
```

Find the `run_agent` call in `on_text` (search for `run_agent(`) and add the `profile_md` kwarg. It will look roughly like:

```python
        resp = await run_agent(
            anthropic=await anthropic_for(u),
            user_message=text,
            tenant_md=load_tenant_md(u.get("tenant_id")),
            user_md=storage.get_user_md_or_template(u["id"]),
            profile_md=storage.get_profile_md(u["id"]),
            recent_claims=recent_claims,
            tool_executor=executor,
            conversation_history=history,
        )
```

Apply the same change to any other `run_agent` call sites (e.g. the file-upload path).

- [ ] **Step 1.11: Update SYSTEM prompt in agent.py**

In `bot/common/agent.py`, add to the SYSTEM prompt right before the "MEMORY — how you learn from the user:" section (around line 50):

```python
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
```

- [ ] **Step 1.12: Commit**

```bash
git add bot/storage.py bot/common/tools.py bot/common/agent.py bot/server.py tests/test_profile.py
git commit -m "Add per-user profile core-memory block

Always-in-context 'who is this user' markdown, rewritten by Janai via
the new update_profile tool. Separate from classification rules — this
is identity, not policy.

Reference design: Letta/MemGPT core memory blocks."
```

---

## Task 2: Merchant Memory Table

Learns each user's filing patterns per merchant. When Janai parses a receipt for Starbucks and the user has filed Starbucks as Meals 5 times, the parse result carries a "this user always files Starbucks as Meals/Coffee" hint. After 3 consistent choices Janai can confidently file without asking.

**Files:**
- Modify: `bot/storage.py` (add `merchant_choices` table + CRUD)
- Modify: `bot/common/agent.py` (render merchant block in context)
- Modify: `bot/server.py:1085` (record hook on submit_claim success)
- Create: `tests/test_merchant_memory.py`

### Steps

- [ ] **Step 2.1: Write failing test for record + retrieve**

```python
# tests/test_merchant_memory.py
import pytest
from pathlib import Path
import tempfile
from bot import storage

@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        monkeypatch.setattr(storage, "DB_PATH", path)
        storage.init_db(path)
        yield path

def test_normalize_merchant():
    assert storage.normalize_merchant("Starbucks Marina") == "starbucks marina"
    assert storage.normalize_merchant("  STARBUCKS  ") == "starbucks"
    assert storage.normalize_merchant("") == ""

def test_record_merchant_choice_new(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    storage.record_merchant_choice(uid, "Starbucks Marina", "POL1", "coffee")
    rows = storage.get_merchant_history(uid, "starbucks marina")
    assert len(rows) == 1
    assert rows[0]["count"] == 1
    assert rows[0]["policy_id"] == "POL1"

def test_record_merchant_choice_increments(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    for _ in range(3):
        storage.record_merchant_choice(uid, "Starbucks", "POL1", "coffee")
    rows = storage.get_merchant_history(uid, "starbucks")
    assert rows[0]["count"] == 3

def test_record_different_classification_is_separate_row(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    storage.record_merchant_choice(uid, "Grab", "POL_TRANS", "taxi")
    storage.record_merchant_choice(uid, "Grab", "POL_TRANS", "taxi")
    storage.record_merchant_choice(uid, "Grab", "POL_MEALS", "delivery")
    rows = storage.get_merchant_history(uid, "grab")
    assert len(rows) == 2
    top = sorted(rows, key=lambda r: -r["count"])[0]
    assert top["policy_id"] == "POL_TRANS"
    assert top["count"] == 2

def test_top_merchants_for_context(tmp_db):
    uid = storage.upsert_user("telegram", "u1")
    storage.record_merchant_choice(uid, "Starbucks", "POL_MEALS", "coffee")
    storage.record_merchant_choice(uid, "Starbucks", "POL_MEALS", "coffee")
    storage.record_merchant_choice(uid, "Grab", "POL_TRANS", "taxi")
    top = storage.top_merchants(uid, limit=10)
    assert len(top) == 2
    assert top[0]["merchant"] == "Starbucks"  # highest count first
    assert top[0]["count"] == 2
```

- [ ] **Step 2.2: Run test — expect failure**

Run: `pytest tests/test_merchant_memory.py -v`
Expected: FAIL — `AttributeError: module 'bot.storage' has no attribute 'normalize_merchant'`

- [ ] **Step 2.3: Implement merchant memory schema + CRUD**

In `bot/storage.py`, add to the `SCHEMA` string (before the closing `"""`, after the `messages` table):

```python
CREATE TABLE IF NOT EXISTS merchant_choices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  merchant_normalized TEXT NOT NULL,
  merchant_display TEXT NOT NULL,
  policy_id TEXT NOT NULL,
  sub_category TEXT,
  count INTEGER NOT NULL DEFAULT 1,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, merchant_normalized, policy_id, sub_category)
);
CREATE INDEX IF NOT EXISTS merchant_choices_user ON merchant_choices(user_id, count DESC);
```

Add these functions at the bottom of the file:

```python
def normalize_merchant(name: str) -> str:
    """Collapse whitespace, lowercase, strip. Good enough for fuzzy-ish match."""
    return " ".join((name or "").lower().split())


def record_merchant_choice(
    user_id: int,
    merchant: str,
    policy_id: str,
    sub_category: str | None,
) -> None:
    """Bump the count for (user, merchant, policy, sub_cat). Insert row if new.
    Called after submit_claim succeeds — proves the user accepted the filing."""
    norm = normalize_merchant(merchant)
    if not norm:
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO merchant_choices
               (user_id, merchant_normalized, merchant_display, policy_id, sub_category)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, merchant_normalized, policy_id, sub_category)
               DO UPDATE SET count=count+1, last_seen=CURRENT_TIMESTAMP""",
            (user_id, norm, merchant, policy_id, sub_category),
        )


def get_merchant_history(user_id: int, merchant_normalized: str) -> list[dict]:
    """Return all (policy, sub_cat, count) rows for this merchant, most-filed first."""
    with db() as conn:
        rows = conn.execute(
            """SELECT policy_id, sub_category, count, last_seen
               FROM merchant_choices
               WHERE user_id=? AND merchant_normalized=?
               ORDER BY count DESC""",
            (user_id, merchant_normalized),
        ).fetchall()
        return [dict(r) for r in rows]


def top_merchants(user_id: int, limit: int = 20) -> list[dict]:
    """Top merchants by total fills across all classifications.
    Used for the context block so Janai can eyeball the pattern."""
    with db() as conn:
        rows = conn.execute(
            """SELECT merchant_display AS merchant,
                      policy_id, sub_category, SUM(count) AS count
               FROM merchant_choices
               WHERE user_id=?
               GROUP BY merchant_normalized, policy_id, sub_category
               ORDER BY count DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 2.4: Run test — expect pass**

Run: `pytest tests/test_merchant_memory.py -v`
Expected: PASS (5/5)

- [ ] **Step 2.5: Write failing test for context block rendering**

Add to `tests/test_merchant_memory.py`:

```python
def test_render_merchants_block_empty():
    from bot.common.agent import render_merchants_block
    assert render_merchants_block([]) == ""

def test_render_merchants_block_has_counts():
    from bot.common.agent import render_merchants_block
    rows = [
        {"merchant": "Starbucks", "policy_id": "POL_MEALS", "sub_category": "coffee", "count": 5},
        {"merchant": "Grab", "policy_id": "POL_TRANS", "sub_category": "taxi", "count": 3},
    ]
    block = render_merchants_block(rows)
    assert "Starbucks" in block
    assert "5x" in block
    assert "POL_MEALS" in block

def test_render_merchants_block_marks_confident():
    """3+ fills means 'file confidently'. Make that visible in the prompt."""
    from bot.common.agent import render_merchants_block
    rows = [
        {"merchant": "Starbucks", "policy_id": "POL_MEALS", "sub_category": "coffee", "count": 5},
        {"merchant": "Rare", "policy_id": "POL_X", "sub_category": None, "count": 1},
    ]
    block = render_merchants_block(rows)
    # Something in the 5x row must signal "confident"
    starbucks_line = [l for l in block.splitlines() if "Starbucks" in l][0]
    rare_line = [l for l in block.splitlines() if "Rare" in l][0]
    assert "(confident)" in starbucks_line
    assert "(confident)" not in rare_line
```

- [ ] **Step 2.6: Run test — expect failure**

Run: `pytest tests/test_merchant_memory.py -v -k render`
Expected: FAIL — `ImportError: cannot import name 'render_merchants_block'`

- [ ] **Step 2.7: Add render_merchants_block to agent.py**

In `bot/common/agent.py`, add above `build_context_text`:

```python
CONFIDENT_THRESHOLD = 3


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
```

Update `build_context_text` to take merchants and render them:

```python
def build_context_text(
    *,
    tenant_md: str,
    user_md: str,
    profile_md: str,
    merchants: list[dict],
    recent_claims: str,
    has_file: bool,
    user_message: str,
) -> str:
    about_block = (
        f"## About you\n{profile_md}\n\n"
        if profile_md.strip()
        else "## About you\n(nothing yet — I'll fill this in as I learn)\n\n"
    )
    merchants_rendered = render_merchants_block(merchants)
    merchants_block = (
        f"## Merchants you've filed before\n{merchants_rendered}\n"
        f"_(confident) = you've filed this merchant the same way 3+ times — "
        f"file without asking._\n\n"
        if merchants_rendered
        else ""
    )
    return (
        f"## Org config\n{tenant_md[:2000]}\n\n"
        f"{about_block}"
        f"## Your rules (learned from past corrections)\n"
        f"{user_md or '(none yet — propose a rule when the user corrects you)'}\n\n"
        f"{merchants_block}"
        f"## Recent claims\n{recent_claims[:1500]}\n\n"
        f"{'[User sent a receipt photo/PDF — call parse_receipt]' if has_file else ''}\n"
        f"## User message\n{user_message}"
    )
```

Also add `merchants: list[dict]` as a parameter to `run_agent` (default `None`) and pass through to `build_context_text` (use `[]` when None).

- [ ] **Step 2.8: Run test — expect pass**

Run: `pytest tests/test_merchant_memory.py -v`
Expected: PASS (8/8). Also run `pytest tests/test_profile.py -v` — expect to update the `test_context_block_includes_profile` call to pass `merchants=[]`. Update that test:

```python
    block = agent.build_context_text(
        tenant_md="tenant",
        user_md="rules",
        profile_md=storage.get_profile_md(uid),
        merchants=[],
        recent_claims="",
        has_file=False,
        user_message="hi",
    )
```

Re-run and confirm all pass.

- [ ] **Step 2.9: Hook merchant recording into submit_claim path**

In `bot/server.py`, the `submit_claim` branch currently (line 1085):

```python
        elif tool_name == "submit_claim":
            cid = tool_input["claim_id"]
            async with client_for(u) as client:
                await client.submit_draft(cid)
            return f"Submitted #{cid} for approval."
```

Replace with:

```python
        elif tool_name == "submit_claim":
            cid = tool_input["claim_id"]
            async with client_for(u) as client:
                await client.submit_draft(cid)
                # Fetch the submitted claim so we can record the merchant pattern.
                try:
                    claim = await client.get_submission(cid)
                    merchant = (claim.get("merchant") or "").strip()
                    policy_id = (claim.get("policy") or {}).get("id") or ""
                    sub_cat = (claim.get("sub_category") or {}).get("name")
                    if merchant and policy_id:
                        storage.record_merchant_choice(u["id"], merchant, str(policy_id), sub_cat)
                except Exception as e:
                    # Recording is best-effort — don't fail the submit.
                    log.warning("record_merchant_choice failed for #%s: %s", cid, e)
            return f"Submitted #{cid} for approval."
```

Note: if `client.get_submission(cid)` doesn't exist, check `omnihr_client/client.py` for the right method name — it may be `get_submission_detail` or similar. If only `list_submissions` exists, filter that by id as a fallback.

- [ ] **Step 2.10: Pass top_merchants into run_agent**

Find every `run_agent(...)` call site in `bot/server.py` and add:

```python
            merchants=storage.top_merchants(u["id"], limit=20),
```

- [ ] **Step 2.11: Update SYSTEM prompt in agent.py**

In `bot/common/agent.py`, add to SYSTEM prompt before the PROFILE section:

```python
MERCHANT MEMORY — the "## Merchants you've filed before" block:

Auto-populated from past successful submit_claim calls. Shows each
merchant with its most-common policy/sub-category and count. Entries
tagged "(confident)" mean the user has filed this merchant the same
way 3+ times — file it that way again without asking. For lower counts,
mention the pattern but let the user confirm: "Starbucks — usually
Meals/Coffee for you, right, darling?"

If the parsed merchant matches a confident entry AND the amount is
within a normal range, auto-file and tell them: "Starbucks again,
$8.50 — filed as Meals/Coffee. Your usual, darling."
```

- [ ] **Step 2.12: Commit**

```bash
git add bot/storage.py bot/common/agent.py bot/server.py tests/test_merchant_memory.py tests/test_profile.py
git commit -m "Add merchant memory table + context injection

Auto-learns per-user merchant → policy/sub-category patterns from
successful submit_claim calls. After 3 consistent fills an entry is
marked '(confident)' so Janai can file without asking.

The '## Merchants you've filed before' block is rendered in every
agent call so suggestions surface at parse time."
```

---

## Task 3: Duplicate Sniff at Parse Time

The pipeline module already has `_match_dupes` that compares parsed receipts to recent OmniHR submissions, but it's not wired into the agent's `parse_receipt` tool path. We surface dupe hints inside the tool result so Janai can warn the user before they file a second copy.

**Files:**
- Modify: `bot/common/pipeline.py` (extract and export `match_dupes` and `fetch_recent_submissions` — promote from private)
- Modify: `bot/server.py:1047` (call dupe check in parse_receipt branch)
- Create: `tests/test_dupe_sniff.py`

### Steps

- [ ] **Step 3.1: Write failing tests for dupe matching**

```python
# tests/test_dupe_sniff.py
from datetime import date
from bot.common.pipeline import match_dupes
from bot.common.parser import ParsedReceipt


def _parsed(merchant="Grab", amount="42.00", dt="2026-04-18"):
    return ParsedReceipt(
        raw={},
        merchant=merchant,
        amount=amount,
        receipt_date=date.fromisoformat(dt),
        currency="SGD",
        is_receipt=True,
        suggested_policy_id="POL_TRANS",
        suggested_sub_category_id=None,
        description=None,
        confidence={"amount": 0.9, "receipt_date": 0.9, "merchant": 0.9},
    )


def test_no_dupes_empty_submissions():
    assert match_dupes(_parsed(), []) == []


def test_exact_match_is_dupe():
    subs = [{
        "id": 99,
        "amount": "42.00",
        "receipt_date": "2026-04-18",
        "merchant": "Grab",
        "status": 1,
    }]
    dupes = match_dupes(_parsed(), subs)
    assert len(dupes) == 1
    assert dupes[0].submission_id == 99


def test_case_insensitive_merchant():
    subs = [{
        "id": 100,
        "amount": "42.00",
        "receipt_date": "2026-04-18",
        "merchant": "GRAB",
        "status": 1,
    }]
    dupes = match_dupes(_parsed(merchant="grab"), subs)
    assert len(dupes) == 1


def test_different_amount_not_dupe():
    subs = [{
        "id": 100,
        "amount": "43.00",
        "receipt_date": "2026-04-18",
        "merchant": "Grab",
        "status": 1,
    }]
    assert match_dupes(_parsed(), subs) == []


def test_different_date_not_dupe():
    subs = [{
        "id": 100,
        "amount": "42.00",
        "receipt_date": "2026-04-17",
        "merchant": "Grab",
        "status": 1,
    }]
    assert match_dupes(_parsed(), subs) == []
```

- [ ] **Step 3.2: Run tests — expect failure**

Run: `pytest tests/test_dupe_sniff.py -v`
Expected: FAIL — `ImportError: cannot import name 'match_dupes' from 'bot.common.pipeline'`

- [ ] **Step 3.3: Promote `_match_dupes` to public `match_dupes`**

In `bot/common/pipeline.py`, rename the private function to public. Change line 171 from:

```python
def _match_dupes(parsed: ParsedReceipt, recent_subs: list[dict[str, Any]]) -> list[DupeHint]:
```

to:

```python
def match_dupes(parsed: ParsedReceipt, recent_subs: list[dict[str, Any]]) -> list[DupeHint]:
```

Update the call site on line 107 from `_match_dupes(...)` to `match_dupes(...)`.

- [ ] **Step 3.4: Run tests — expect pass**

Run: `pytest tests/test_dupe_sniff.py -v`
Expected: PASS (5/5)

- [ ] **Step 3.5: Write failing test for parse_receipt tool result format**

Add to `tests/test_dupe_sniff.py`:

```python
from bot.common.pipeline import format_dupe_warning, DupeHint


def test_format_dupe_warning_empty():
    assert format_dupe_warning([]) == ""


def test_format_dupe_warning_single():
    hints = [DupeHint(
        submission_id=123,
        receipt_date=date.fromisoformat("2026-04-18"),
        amount="42.00",
        merchant="Grab",
        status=1,
    )]
    w = format_dupe_warning(hints)
    assert "DUPLICATE" in w.upper()
    assert "#123" in w
    assert "42.00" in w


def test_format_dupe_warning_multiple():
    hints = [
        DupeHint(submission_id=1, receipt_date=date.fromisoformat("2026-04-18"),
                 amount="42.00", merchant="Grab", status=1),
        DupeHint(submission_id=2, receipt_date=date.fromisoformat("2026-04-18"),
                 amount="42.00", merchant="Grab", status=3),
    ]
    w = format_dupe_warning(hints)
    assert "#1" in w and "#2" in w
```

- [ ] **Step 3.6: Run tests — expect failure**

Run: `pytest tests/test_dupe_sniff.py -v -k format`
Expected: FAIL — `ImportError: cannot import name 'format_dupe_warning'`

- [ ] **Step 3.7: Implement format_dupe_warning**

In `bot/common/pipeline.py`, add below `match_dupes`:

```python
def format_dupe_warning(hints: list[DupeHint]) -> str:
    """Format dupe hints as a prompt-ready warning block for the agent.
    Returned string is empty when there are no dupes — caller can concat
    unconditionally."""
    if not hints:
        return ""
    lines = ["⚠ POSSIBLE DUPLICATE(S) — same amount/date/merchant already on OmniHR:"]
    for h in hints:
        lines.append(
            f"- #{h.submission_id} {h.receipt_date.isoformat()} "
            f"{h.merchant or '?'} {h.amount} (status={h.status})"
        )
    lines.append(
        "If this is the same transaction, warn the user before filing. "
        "If they confirm it's a separate charge, proceed."
    )
    return "\n".join(lines)
```

- [ ] **Step 3.8: Run tests — expect pass**

Run: `pytest tests/test_dupe_sniff.py -v`
Expected: PASS (8/8)

- [ ] **Step 3.9: Wire dupe check into parse_receipt tool executor**

In `bot/server.py`, replace the `parse_receipt` branch (lines ~1047-1064):

```python
        if tool_name == "parse_receipt":
            if not file_bytes:
                return "No receipt file attached. Ask the user to send a photo or PDF."
            tenant_md = load_tenant_md(u.get("tenant_id"))
            user_md = load_user_md(u)
            try:
                parsed = await parse_receipt(
                    anthropic=await anthropic_for(u),
                    file_bytes=file_bytes,
                    media_type=media_type,
                    tenant_md=tenant_md,
                    user_md=user_md,
                    recent_claims_summary="",
                    active_trip=None,
                )
            except Exception as e:
                return f"Parse failed: {e}"

            # Dupe sniff — concurrent with parse would be nicer but keep it simple.
            dupe_warning = ""
            try:
                async with client_for(u) as client:
                    recent = await client.list_submissions(page_size=60)
                hints = match_dupes(parsed, recent.get("results", []))
                dupe_warning = format_dupe_warning(hints)
            except Exception as e:
                log.warning("dupe sniff failed: %s", e)

            parsed_json = json.dumps(parsed.raw, default=str)
            if dupe_warning:
                return f"{dupe_warning}\n\n---\n\n{parsed_json}"
            return parsed_json
```

At the top of `bot/server.py`, add to the imports block (near other `from bot.common...` lines):

```python
from bot.common.pipeline import match_dupes, format_dupe_warning
```

- [ ] **Step 3.10: Update SYSTEM prompt in agent.py**

In `bot/common/agent.py`, add a new rules bullet in the RULES section (around line 41):

```python
- If parse_receipt returns a result starting with "⚠ POSSIBLE DUPLICATE(S)",
  surface that warning to the user BEFORE you file. Quote the dupe claim ID
  and ask if it's the same transaction. Don't auto-file over a dupe, ever.
```

- [ ] **Step 3.11: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all existing tests plus the new ones pass.

- [ ] **Step 3.12: Commit**

```bash
git add bot/common/pipeline.py bot/common/agent.py bot/server.py tests/test_dupe_sniff.py
git commit -m "Wire duplicate-receipt sniff into parse_receipt tool

The pipeline module already had _match_dupes but it was unused — the
agent's parse_receipt tool went straight from parse to response without
checking OmniHR for existing same-amount/date/merchant claims.

Now parse_receipt fetches the last 60 submissions, runs match_dupes,
and prepends a warning block to the tool result so Janai surfaces it
before filing. System prompt teaches her to never auto-file over a
dupe."
```

---

## Post-Implementation Sanity Check

After all three tasks are committed, run the bot locally and confirm the wow moments surface:

1. **Profile**: send a message like "call me darling, I'm based in Singapore" — within a turn or two Janai should call `update_profile` and the next conversation should reference it.
2. **Merchant memory**: file the same merchant (e.g. Starbucks) 3 times via submit_claim. On the 4th parse, she should say "your usual" / "filed as Meals/Coffee like last 3 times" without asking.
3. **Dupe sniff**: submit a claim, then re-upload the same receipt bytes via a different file (re-photo). She should warn about the dupe before filing.

If any of these feel wrong, file issues rather than patching inline — each feature is independently rollback-able via `git revert`.

---

## Self-Review

**Spec coverage:** All three features from the recommendation shortlist (profile block, merchant memory, dupe sniff) have dedicated tasks with TDD cycles.

**Placeholder scan:** No TBDs, no "similar to Task N," no "add error handling" — every step has explicit code or commands.

**Type consistency:** `record_merchant_choice`, `get_merchant_history`, `top_merchants`, `normalize_merchant` used consistently. `match_dupes` and `format_dupe_warning` named consistently. `build_context_text` signature updated in Task 1 and extended in Task 2 — Step 2.8 explicitly calls out updating the Task 1 test to match the new signature.

**Deploy note:** each task's commit is independently deployable to the Oracle VM — profile + merchant memory are additive DB migrations; dupe sniff is a pure wiring change. Deploy after each commit to smoke-test in prod before moving to the next task.
