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


def test_context_block_includes_profile(tmp_db, monkeypatch):
    """The agent's context_block must include profile_md when present."""
    from bot.common import agent
    uid = storage.upsert_user("telegram", "u1")
    storage.set_profile_md(uid, "- Name: Ying\n- Always flies SIA")

    block = agent.build_context_text(
        tenant_md="tenant",
        user_md="rules",
        profile_md=storage.get_profile_md(uid),
        merchants=[],
        recent_claims="",
        has_file=False,
        user_message="hi",
    )
    assert "Ying" in block
    assert "SIA" in block
    assert "## About you" in block


def test_context_block_empty_profile_shows_fallback(tmp_db):
    """Fresh users (empty profile_md) see the 'nothing yet' placeholder so
    the agent knows to fill it in rather than silently skipping the block."""
    from bot.common import agent
    uid = storage.upsert_user("telegram", "u1")

    block = agent.build_context_text(
        tenant_md="tenant",
        user_md="",
        profile_md=storage.get_profile_md(uid),
        merchants=[],
        recent_claims="",
        has_file=False,
        user_message="hi",
    )
    assert "## About you" in block
    assert "nothing yet" in block
