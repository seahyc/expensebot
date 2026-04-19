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
        "name": "update_memories",
        "description": (
            "Replace the user's memory markdown — the file shown by /memories. "
            "Call this ONLY after the user has explicitly confirmed a proposed "
            "change in the same conversation turn. Takes the FULL new markdown, "
            "not a diff. You MUST preserve the five section headers "
            "(Classification rules, Merchant shortcuts, Defaults, "
            "Description style, Don't ask me about) and their italic blurbs. "
            "Entries go under the right section as one-liner bullets in the "
            "format: **Short rule** — why (YYYY-MM-DD). Remove '- (none yet)' "
            "placeholders once a section has a real entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "new_markdown": {
                    "type": "string",
                    "description": "The full replacement markdown — all five sections, headers intact.",
                },
                "change_summary": {
                    "type": "string",
                    "description": "One short line, e.g. 'Added to Classification rules: Grab after 10pm → Personal'.",
                },
            },
            "required": ["new_markdown", "change_summary"],
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
    {
        "name": "search_email_context",
        "description": (
            "Search the user's Gmail. Use for any email-related question: "
            "finding threads about a topic, checking if something was confirmed, "
            "verifying a business purpose, or summarising recent email activity. "
            "Not just for receipts — call whenever the user asks about their email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {
                    "type": "string",
                    "description": "Topic, sender, or merchant to search for",
                },
                "date_hint": {
                    "type": "string",
                    "description": "Date to search around, e.g. '2026-04-15'. Use today's date if not specified.",
                },
                "time_hint": {
                    "type": "string",
                    "description": "Optional time to narrow the search, e.g. '14:30'",
                },
            },
            "required": ["merchant", "date_hint"],
        },
    },
    {
        "name": "search_calendar_context",
        "description": (
            "Search the user's Google Calendar. Use for any calendar question: "
            "what meetings are coming up, what happened on a given day, "
            "finding a specific event, or summarising the week ahead. "
            "Not just for receipts — call whenever the user asks about their schedule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_hint": {
                    "type": "string",
                    "description": "Date to search around, e.g. '2026-04-15'. Use today's date if not specified.",
                },
                "time_hint": {
                    "type": "string",
                    "description": "Optional time to narrow the search, e.g. '14:30'",
                },
            },
            "required": ["date_hint"],
        },
    },
    {
        "name": "get_omnihr_context",
        "description": (
            "Load the OmniHR expense context: org config, expense policy, recent claims, "
            "and merchant memory. Call this FIRST whenever the user's request involves "
            "expenses, receipts, claims, policy, or anything OmniHR-related. "
            "Returns everything you need to classify and file claims correctly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
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
]
