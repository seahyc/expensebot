"""Tests for the schema module — uses the real Glints schema we captured."""

from datetime import date

from omnihr_client.schema import FormSchema


GLINTS_TRAVEL_INTL_RAW = {
    "form": {
        "reimburse_amount_field_id": 1685,
        "fields": [
            {"is_mandatory": True, "ordering": 1, "label": "Business Trip Destination",
             "field_type": "SHORT_TEXT", "form_data_type": "CUSTOM",
             "is_deletable": None, "options": None, "field_id": 1975},
            {"is_mandatory": True, "ordering": 2, "label": "Business Trip Start",
             "field_type": "DATE", "form_data_type": "CUSTOM",
             "is_deletable": None, "options": None, "field_id": 1973},
            {"is_mandatory": True, "ordering": 3, "label": "Business Trip End",
             "field_type": "DATE", "form_data_type": "CUSTOM",
             "is_deletable": None, "options": None, "field_id": 1974},
            {"is_mandatory": True, "ordering": 4, "label": "Travel - International Sub-Category",
             "field_type": "SINGLE_SELECT", "form_data_type": "CUSTOM",
             "is_deletable": None,
             "options": [
                 {"label": "Accommodations", "ordering": 0, "is_deletable": None, "id": 2078},
                 {"label": "Flight Ticket", "ordering": 1, "is_deletable": None, "id": 2079},
                 {"label": "Transportation (Airport)", "ordering": 4, "is_deletable": None, "id": 2082},
             ],
             "field_id": 1971},
            {"is_mandatory": True, "ordering": 5, "label": "Claim Amount",
             "field_type": "AMOUNT", "form_data_type": "AMOUNT",
             "is_deletable": None, "options": None, "field_id": 1685},
            {"is_mandatory": True, "ordering": 6, "label": "Amount in Payroll Currency",
             "field_type": "AMOUNT", "form_data_type": "CUSTOM",
             "is_deletable": None, "options": None, "field_id": 2060},
            {"is_mandatory": False, "ordering": 7, "label": "Merchant",
             "field_type": "SHORT_TEXT", "form_data_type": "MERCHANT",
             "is_deletable": None, "options": None, "field_id": 1686},
            {"is_mandatory": True, "ordering": 8, "label": "Receipt Date",
             "field_type": "DATE", "form_data_type": "RECEIPT_DATE",
             "is_deletable": None, "options": None, "field_id": 1687},
            {"is_mandatory": True, "ordering": 9, "label": "Description",
             "field_type": "SHORT_TEXT", "form_data_type": "DESCRIPTION",
             "is_deletable": None, "options": None, "field_id": 1688},
            {"is_mandatory": True, "ordering": 10, "label": "Upload Receipt(s)",
             "field_type": "ATTACHMENT", "form_data_type": "RECEIPTS",
             "is_deletable": None, "options": None, "field_id": 1689},
        ],
    }
}


def test_schema_parses_glints_travel_intl():
    s = FormSchema.from_api(
        tenant_id="glints",
        policy_id=3712,
        receipt_date=date(2026, 4, 9),
        api_response=GLINTS_TRAVEL_INTL_RAW,
    )
    assert s.tenant_id == "glints"
    assert s.policy_id == 3712
    assert s.receipt_date_bucket == "2026-04"

    amount = s.field_by_fdt("AMOUNT")
    assert amount and amount.field_id == 1685 and amount.is_mandatory

    receipts = s.field_by_fdt("RECEIPTS")
    assert receipts and receipts.field_id == 1689

    customs = s.custom_fields()
    assert {c.field_id for c in customs} == {1971, 1973, 1974, 1975, 2060}

    sub_cat = next(c for c in customs if c.field_id == 1971)
    assert sub_cat.field_type == "SINGLE_SELECT"
    assert any(o.label == "Flight Ticket" and o.id == 2079 for o in sub_cat.options)


def test_payload_resolves_subcategory_label_to_id():
    """The client's _build_payload should turn 'Flight Ticket' → 2079."""
    from omnihr_client.client import OmniHRClient

    s = FormSchema.from_api(
        tenant_id="glints",
        policy_id=3712,
        receipt_date=date(2026, 4, 9),
        api_response=GLINTS_TRAVEL_INTL_RAW,
    )
    sub_cat = next(c for c in s.custom_fields() if c.field_id == 1971)
    coerced = OmniHRClient._coerce_value(sub_cat, "Flight Ticket")
    assert coerced == 2079

    coerced_int = OmniHRClient._coerce_value(sub_cat, 2079)
    assert coerced_int == 2079
