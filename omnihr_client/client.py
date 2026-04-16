"""OmniHRClient — schema-driven client. One per (user, tenant)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .auth import Tokens, refresh_access_token
from .exceptions import AuthError, OmniHRError, SchemaDriftError, ValidationError
from .schema import FieldOption, FormField, FormSchema, get_schema, invalidate_schema

log = logging.getLogger(__name__)

# Status codes observed on OmniHR (Glints tenant).
# OmniHR lifecycle:  Draft → Submitted (For Approval) → Approved → Reimbursed
#
# Proven values:
#   3 = DRAFT          (created via /draft/ returns this)
#   7 = FOR APPROVAL   (after submit — "Submitted" tab, "For Approval" filter)
#   8 = DELETED        (quick_action action=1 returns this)
# Likely (from UI dropdown, need to verify when a claim transitions):
#   1 = APPROVED
#   2 = REIMBURSED
#   4, 5, 6 = other/rejected flavors (TBD)
STATUS_DRAFT = 3
STATUS_FOR_APPROVAL = 7
STATUS_APPROVED = 1  # tentative
STATUS_REIMBURSED = 2  # tentative
STATUS_DELETED = 8

STATUS_LABELS: dict[int, str] = {
    1: "APPROVED",
    2: "REIMBURSED",
    3: "DRAFT",
    4: "?",
    5: "?",
    6: "REJECTED",
    7: "FOR APPROVAL",
    8: "DELETED",
}

# All non-deleted states.
ACTIVE_STATUS_FILTERS = "1,2,3,4,5,6,7"

# Named filter shortcuts for /list <filter>
FILTER_SHORTCUTS: dict[str, str] = {
    "all": "1,2,3,4,5,6,7",
    "draft": "3",
    "drafts": "3",
    "submitted": "7",
    "pending": "7",
    "approval": "7",
    "approved": "1",
    "reimbursed": "2",
    "paid": "2",
}

# Quick-action codes (POST /expense-metadata/{id}/quick-actions/ {action: N})
# action=1 → delete (proven)
# action=2/3 → submit/approve/etc (TBD — probe next time you click Submit on a draft)
QUICK_ACTION_DELETE = 1
QUICK_ACTION_SUBMIT = 2  # TENTATIVE — verify before relying on this


class OmniHRClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.omnihr.co/api/v1",
        tokens: Tokens,
        employee_id: int,
        tenant_id: str,
    ):
        self.tokens = tokens
        self.employee_id = employee_id
        self.tenant_id = tenant_id
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30)

    async def __aenter__(self) -> "OmniHRClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _ensure_fresh(self) -> None:
        if self.tokens.access_expired:
            if self.tokens.refresh_expired:
                raise AuthError("Refresh token expired — user must re-pair")
            self.tokens = await refresh_access_token(self._http, self.tokens.refresh_token)

    def _cookies(self) -> dict[str, str]:
        return {
            "access_token": self.tokens.access_token,
            "refresh_token": self.tokens.refresh_token,
        }

    # --- discovery ---

    async def auth_details(self) -> dict[str, Any]:
        """GET /auth/details/ — the canonical 'who am I + which org' call."""
        await self._ensure_fresh()
        resp = await self._http.get("/auth/details/", cookies=self._cookies())
        if resp.status_code in (401, 403):
            raise AuthError(resp.text)
        resp.raise_for_status()
        return resp.json()

    async def policy_tree(self) -> list[dict[str, Any]]:
        """GET /expense/3.0/category/policy-tree/{employee_id}/"""
        await self._ensure_fresh()
        resp = await self._http.get(
            f"/expense/3.0/category/policy-tree/{self.employee_id}/", cookies=self._cookies()
        )
        resp.raise_for_status()
        return resp.json()

    async def get_form_config(
        self, *, policy_id: int, receipt_date: date
    ) -> dict[str, Any]:
        """GET /expense/3.0/user/{uid}/policy/{pid}/expense-form-config/?receipt_date=YYYY-MM-DD

        Returns the per-policy form schema (mandatory + custom fields).
        """
        await self._ensure_fresh()
        resp = await self._http.get(
            f"/expense/3.0/user/{self.employee_id}/policy/{policy_id}/expense-form-config/",
            params={"receipt_date": receipt_date.isoformat()},
            cookies=self._cookies(),
        )
        resp.raise_for_status()
        return resp.json()

    async def schema(self, policy_id: int, receipt_date: date) -> FormSchema:
        return await get_schema(
            client=self,
            tenant_id=self.tenant_id,
            policy_id=policy_id,
            receipt_date=receipt_date,
        )

    # --- file upload ---

    async def upload_document(
        self,
        *,
        file_bytes: bytes | None = None,
        file_path: Path | None = None,
        name: str,
        media_type: str = "application/pdf",
    ) -> dict[str, Any]:
        """POST /expense/1.0/document/ (multipart). Returns {id, file_path, ...}.

        Pass either file_bytes (preferred — matches our chat/email pipelines)
        or file_path. `name` controls the filename OmniHR records.
        """
        await self._ensure_fresh()
        if file_bytes is None and file_path is None:
            raise ValueError("Provide file_bytes or file_path")
        if file_bytes is None:
            file_bytes = file_path.read_bytes()
        files = {"file": (name, file_bytes, media_type)}
        data = {"name": name, "owner": str(self.employee_id)}
        resp = await self._http.post(
            "/expense/1.0/document/",
            cookies=self._cookies(),
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return resp.json()

    # --- submissions ---

    async def list_submissions(
        self, *, status_filters: str = ACTIVE_STATUS_FILTERS, page: int = 1, page_size: int = 30
    ) -> dict[str, Any]:
        """GET .../submissions/?status_filters=..."""
        await self._ensure_fresh()
        resp = await self._http.get(
            "/expense/2.0/expense-metadata/metadata/submissions/",
            params={"page": page, "page_size": page_size, "status_filters": status_filters},
            cookies=self._cookies(),
        )
        resp.raise_for_status()
        return resp.json()

    async def create_draft(
        self,
        *,
        policy_id: int,
        schema: FormSchema,
        values: dict[str, Any],
        receipts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """POST /expense/2.0/expense-metadata-v2/draft/

        `values` is keyed by form_data_type for standard fields and by custom
        field LABEL for CUSTOM fields. Resolution to field_id + option_id
        happens here against the schema.

        `receipts` is a list of {id, file_path} as returned from upload_document.
        """
        await self._ensure_fresh()
        body = self._build_payload(policy_id, schema, values, receipts)
        resp = await self._http.post(
            "/expense/2.0/expense-metadata-v2/draft/",
            cookies=self._cookies(),
            json=body,
        )
        if resp.status_code == 400:
            data = resp.json()
            err = data.get("error_code", "")
            if err.startswith("ERROR_EXPENSE_METADATA_CUSTOM_FIELD"):
                # Schema drift OR genuinely missing field. Bubble up; caller
                # decides whether to invalidate + retry (drift) or surface to
                # user (true validation error).
                raise SchemaDriftError(err, field_errors=data.get("fields", []))
            raise ValidationError(err or resp.text, field_errors=data.get("fields", []))
        resp.raise_for_status()
        return resp.json()

    async def quick_action(
        self, submission_id: int, action: int
    ) -> dict[str, Any]:
        """POST /expense/2.0/expense-metadata/{id}/quick-actions/

        action=1 → delete (proven). 2/3 → likely submit/approve (probe needed).
        """
        await self._ensure_fresh()
        resp = await self._http.post(
            f"/expense/2.0/expense-metadata/{submission_id}/quick-actions/",
            cookies=self._cookies(),
            json={"action": action, "employee_id": self.employee_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_submission(self, submission_id: int) -> None:
        await self.quick_action(submission_id, QUICK_ACTION_DELETE)

    async def submit_draft(self, submission_id: int) -> dict[str, Any]:
        """Submit an existing draft. Action code currently TENTATIVE."""
        return await self.quick_action(submission_id, QUICK_ACTION_SUBMIT)

    # --- payload builder ---

    def _build_payload(
        self,
        policy_id: int,
        schema: FormSchema,
        values: dict[str, Any],
        receipts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Translate human-friendly values dict into OmniHR's fields[] payload.

        values keys can be:
          - form_data_type (for standard fields):  "AMOUNT", "MERCHANT", etc.
          - custom field label (case-insensitive): "Business Trip Destination"

        Values for AMOUNT/CUSTOM-amount: {"amount": "27.80", "amount_currency": "SGD"}
        Values for DATE: ISO date string "YYYY-MM-DD"
        Values for SINGLE_SELECT: option label ("Flight Ticket") or option id (2079)
        Values for SHORT_TEXT: plain string
        """
        fields_out: list[dict[str, Any]] = []
        seen_field_ids: set[int] = set()

        # Standard fields by form_data_type
        for fdt in ("AMOUNT", "MERCHANT", "RECEIPT_DATE", "DESCRIPTION", "RECEIPTS"):
            f = schema.field_by_fdt(fdt)
            if not f:
                continue
            if fdt == "RECEIPTS":
                fields_out.append({"field_id": f.field_id, "value": receipts})
                seen_field_ids.add(f.field_id)
                continue
            if fdt in values:
                fields_out.append({"field_id": f.field_id, "value": values[fdt]})
                seen_field_ids.add(f.field_id)
            elif f.is_mandatory:
                raise ValidationError(f"Missing mandatory standard field {fdt}")

        # Custom fields by label
        custom_lookup = {f.label.lower(): f for f in schema.custom_fields()}
        for key, raw in values.items():
            if key in ("AMOUNT", "MERCHANT", "RECEIPT_DATE", "DESCRIPTION", "RECEIPTS"):
                continue
            f = custom_lookup.get(key.lower())
            if not f:
                # unknown key — skip silently (caller might pass extras)
                continue
            value = self._coerce_value(f, raw)
            fields_out.append({"field_id": f.field_id, "value": value})
            seen_field_ids.add(f.field_id)

        # Mandatory custom fields not provided?
        missing = [
            f
            for f in schema.custom_fields()
            if f.is_mandatory and f.field_id not in seen_field_ids
        ]
        if missing:
            raise ValidationError(
                f"Missing mandatory custom field(s): {[m.label for m in missing]}"
            )

        return {
            "policy_id": policy_id,
            "employee_id": self.employee_id,
            "fields": fields_out,
        }

    @staticmethod
    def _coerce_value(field: FormField, raw: Any) -> Any:
        """For SINGLE_SELECT: resolve label → id. Other types: pass through."""
        if field.field_type == "SINGLE_SELECT":
            if isinstance(raw, int):
                return raw
            for opt in field.options:
                if opt.label.lower() == str(raw).lower():
                    return opt.id
            raise ValidationError(
                f"Unknown option '{raw}' for {field.label}. "
                f"Allowed: {[o.label for o in field.options]}"
            )
        return raw


# Convenience helper for the common create-with-retry pattern
async def create_draft_with_retry(
    client: OmniHRClient,
    *,
    policy_id: int,
    receipt_date: date,
    values: dict[str, Any],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """create_draft, but on SchemaDriftError invalidate + refetch + retry once."""
    schema = await client.schema(policy_id, receipt_date)
    try:
        return await client.create_draft(
            policy_id=policy_id, schema=schema, values=values, receipts=receipts
        )
    except SchemaDriftError as e:
        log.warning("schema drift on policy %s; refetching", policy_id, extra={"err": str(e)})
        await invalidate_schema(tenant_id=client.tenant_id, policy_id=policy_id)
        schema = await client.schema(policy_id, receipt_date)
        return await client.create_draft(
            policy_id=policy_id, schema=schema, values=values, receipts=receipts
        )
