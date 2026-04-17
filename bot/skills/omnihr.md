# OmniHR — HRMS Integration Skill

## Overview
OmniHR is a cloud HR platform at `<subdomain>.omnihr.co`. API at `api.omnihr.co/api/v1/`.
Cookie-based JWT auth (access ~15min, refresh ~30d). Google SSO supported.

## Claim lifecycle
```
DRAFT (3) → FOR APPROVAL (4) → APPROVED (7) → REIMBURSED (5)
                                    ↘ REJECTED (6)
```
- Drafts can be edited, submitted, or deleted
- Submitted claims await manager approval
- Approved claims enter payroll for reimbursement
- Rejected claims show a reason — user can fix + resubmit

## Policies & categories
Each tenant defines policies (e.g. Travel-International, Travel-Local, Subscriptions).
Each policy has:
- Standard fields: Claim Amount, Merchant, Receipt Date, Description, Upload Receipt(s)
- Custom fields per policy: Trip Destination, Trip Start/End, Sub-Category, Amount in Payroll Currency
- Sub-categories via SINGLE_SELECT (e.g. Flight Ticket, Transportation, Accommodations)

Field IDs are per-tenant — never hardcode. Discover via:
- `GET /expense/3.0/category/policy-tree/{employee_id}/` → list all policies
- `GET /expense/3.0/user/{uid}/policy/{pid}/expense-form-config/?receipt_date=YYYY-MM-DD` → per-policy schema

## Key API endpoints
| Action | Method | Endpoint |
|--------|--------|----------|
| Who am I | GET | `/auth/details/` |
| List policies | GET | `/expense/3.0/category/policy-tree/{employee_id}/` |
| Form schema | GET | `/expense/3.0/user/{uid}/policy/{pid}/expense-form-config/?receipt_date=YYYY-MM-DD` |
| Upload receipt | POST | `/expense/1.0/document/` (multipart: file, name, owner) |
| Create draft | POST | `/expense/2.0/expense-metadata-v2/draft/` |
| List submissions | GET | `/expense/2.0/expense-metadata/metadata/submissions/?status_filters=...&page=1&page_size=30` |
| Quick action | POST | `/expense/2.0/expense-metadata/{id}/quick-actions/` (action=1 delete) |
| Doc download | GET | `/expense/1.0/document/{doc_id}/` → `{download_url}` |
| Refresh token | POST | `/auth/token/refresh/` |

## Status filter values for listing
- `3` → drafts
- `4` → for approval (submitted, pending)
- `7` → approved
- `5` → reimbursed
- `3,4,5,7` → all active

## Draft creation payload shape
```json
{
  "policy_id": 3770,
  "employee_id": 59430,
  "fields": [
    {"field_id": 1685, "value": {"amount": "26.10", "amount_currency": "SGD"}},
    {"field_id": 1686, "value": "Gojek"},
    {"field_id": 1687, "value": "2026-04-14"},
    {"field_id": 1688, "value": "Gojek for local meeting"},
    {"field_id": 1689, "value": [{"id": 173041, "file_path": "https://s3..."}]},
    {"field_id": 1973, "value": "2026-04-14"},
    {"field_id": 1974, "value": "2026-04-14"},
    {"field_id": 1975, "value": "Singapore"},
    {"field_id": 1988, "value": 2109}
  ]
}
```

Note: field_ids above are Glints-specific examples. Always resolve from schema.

## Common receipt sources
- Grab / Gojek ride receipts (PDF with trip details)
- Traveloka flight bookings (PDF with itinerary + payment)
- Hotel invoices, SaaS subscription receipts
- Restaurant / meal receipts (photos)

## When classifying receipts
- Match merchant + context to the closest policy
- For rides to/from airport → Travel-International / Transportation (Airport)
- For local rides → Travel-Local / Transportation
- For flights → Travel-International / Flight Ticket
- For subscriptions → Subscriptions - General or Development Tools
- When unsure, ask the user rather than guessing wrong
