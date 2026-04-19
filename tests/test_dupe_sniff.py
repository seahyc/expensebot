from datetime import date

from bot.common.parser import ParsedReceipt
from bot.common.pipeline import DupeHint, format_dupe_warning, match_dupes


def _parsed(merchant="Grab", amount="42.00", dt="2026-04-18"):
    # Adapted from the spec: dataclass uses `suggested_sub_category_label`
    # (not `_id`) and `description_draft` (not `description`). Per Task 3
    # instructions, adapt the test rather than the dataclass.
    return ParsedReceipt(
        raw={},
        merchant=merchant,
        amount=amount,
        receipt_date=date.fromisoformat(dt),
        currency="SGD",
        is_receipt=True,
        suggested_policy_id=1,
        suggested_sub_category_label=None,
        description_draft="",
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
