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


def test_record_merchant_choice_none_sub_category_increments(tmp_db):
    """NULL sub_category must still de-dup on repeat insert (coerce to '')."""
    uid = storage.upsert_user("telegram", "u1")
    storage.record_merchant_choice(uid, "Fairprice", "POL_MEALS", None)
    storage.record_merchant_choice(uid, "Fairprice", "POL_MEALS", None)
    rows = storage.get_merchant_history(uid, "fairprice")
    assert len(rows) == 1
    assert rows[0]["count"] == 2


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
    starbucks_line = [l for l in block.splitlines() if "Starbucks" in l][0]
    rare_line = [l for l in block.splitlines() if "Rare" in l][0]
    assert "(confident)" in starbucks_line
    assert "(confident)" not in rare_line
