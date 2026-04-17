"""Tool definitions for the expense agent.

These are passed to Claude's tool_use API. Claude decides which to call
based on the user's message. The executor runs them against real APIs.
"""

from __future__ import annotations

TOOLS = [
    {
        "name": "parse_receipt",
        "description": (
            "Parse a receipt image or PDF. Extracts merchant, amount, date, currency, "
            "and suggests the right expense policy + sub-category. Call this when the user "
            "sends a photo or document."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description_hint": {
                    "type": "string",
                    "description": "User's caption or note about the receipt, e.g. 'lunch with client'",
                },
            },
            "required": [],
        },
    },
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
        "name": "submit_claim",
        "description": (
            "Submit a draft expense claim for approval. Requires the claim ID. "
            "Only works on claims in DRAFT status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "integer", "description": "The claim ID to submit"},
            },
            "required": ["claim_id"],
        },
    },
    {
        "name": "delete_claim",
        "description": (
            "Delete an expense claim. Works on drafts and pending claims."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "integer", "description": "The claim ID to delete"},
            },
            "required": ["claim_id"],
        },
    },
    {
        "name": "get_claim_summary",
        "description": (
            "Get a summary of the user's expense history — totals by status, "
            "month, or category. Use for questions like 'how much did I spend in April?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The user's question about their expenses",
                },
            },
            "required": ["question"],
        },
    },
]
