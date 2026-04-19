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
        "name": "list_recent_emails",
        "description": (
            "Fetch the user's recent Gmail inbox — latest emails, no search query needed. "
            "Use this when the user asks for 'my emails', 'inbox', 'last N emails', "
            "'any recent emails', or a general email summary. Returns the most recent threads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to look. Default: 7.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_email_context",
        "description": (
            "Search the user's Gmail for a specific topic, sender, or merchant. "
            "Use when looking for emails about something specific — a receipt, a person, "
            "a company, a project. Not for general inbox listing (use list_recent_emails for that)."
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
                    "description": "Date to search around, e.g. '2026-04-15'. Defaults to today.",
                },
                "time_hint": {
                    "type": "string",
                    "description": "Optional time to narrow the search, e.g. '14:30'",
                },
            },
            "required": ["merchant"],
        },
    },
    {
        "name": "list_upcoming_events",
        "description": (
            "Fetch the user's upcoming Google Calendar events — no search needed. "
            "Use when the user asks 'what's on my calendar', 'my schedule', 'what's next', "
            "'anything this week', or wants a general calendar overview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days ahead to look. Default: 7.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_calendar_context",
        "description": (
            "Search the user's Google Calendar for events near a specific date and time. "
            "Use when looking for what was happening at a particular moment — e.g. "
            "'what meeting did I have at 2pm on Tuesday'. For general schedule overview, "
            "use list_upcoming_events instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_hint": {
                    "type": "string",
                    "description": "Date to search around, e.g. '2026-04-15'.",
                },
                "time_hint": {
                    "type": "string",
                    "description": "Time for event lookup, e.g. '14:30'.",
                },
            },
            "required": ["date_hint"],
        },
    },
    {
        "name": "get_whatsapp_messages",
        "description": (
            "Read the user's recent WhatsApp messages. Call whenever the user asks about "
            "their WhatsApp chats, messages, or asks you to summarise WhatsApp activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to fetch. Default: 7.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_telegram_messages",
        "description": (
            "Bulk-fetch recent Telegram messages across all chats (no filter). "
            "Use for summaries or when you don't know which chat to look in. "
            "For messages from a specific person, use get_telegram_chat instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to fetch. Default: 7.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_telegram_chats",
        "description": (
            "List the user's Telegram chats that had activity recently. "
            "Returns chat names, types, and message counts. "
            "Call this first when the user asks about a specific person — "
            "it tells you which chat name to use with get_telegram_chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to look for activity. Default: 30.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_telegram_chat",
        "description": (
            "Fetch messages from a specific Telegram chat or contact by name. "
            "Use when the user asks about messages from a specific person or group. "
            "Use list_telegram_chats first if you're unsure of the exact chat name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {
                    "type": "string",
                    "description": "Name of the contact or group chat to fetch messages from.",
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back to fetch. Default: 7.",
                },
            },
            "required": ["contact"],
        },
    },
    {
        "name": "list_whatsapp_chats",
        "description": (
            "List WhatsApp chats that have stored messages. "
            "Returns contact names/numbers and message counts. "
            "Call this first when the user asks about a specific WA contact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to look. Default: 30.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_whatsapp_chat",
        "description": (
            "Fetch messages from a specific WhatsApp chat by contact name or phone number. "
            "Use list_whatsapp_chats first to find the right contact identifier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {
                    "type": "string",
                    "description": "Contact name or phone number (partial match works).",
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back to fetch. Default: 7.",
                },
            },
            "required": ["contact"],
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
