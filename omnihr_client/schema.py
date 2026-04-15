"""Form schema discovery + cache + invalidation.

Schema is per (tenant, policy_id, year-month). Cached 24h. Re-fetched on:
 - cache miss
 - active invalidation (API returned ERROR_EXPENSE_METADATA_CUSTOM_FIELD_*)
 - nightly background refresh (ops/schema_refresher.py)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

# Standard form_data_type values shared across all OmniHR tenants
StandardFDT = Literal[
    "AMOUNT",          # claim amount (mandatory)
    "MERCHANT",        # vendor name
    "RECEIPT_DATE",    # date on receipt (mandatory)
    "DESCRIPTION",     # free text
    "RECEIPTS",        # file attachments (mandatory)
    "CUSTOM",          # tenant-defined custom field
]


@dataclass
class FieldOption:
    id: int
    label: str
    ordering: int


@dataclass
class FormField:
    field_id: int
    label: str
    field_type: str  # "AMOUNT" | "DATE" | "SHORT_TEXT" | "ATTACHMENT" | "SINGLE_SELECT"
    form_data_type: str  # see StandardFDT
    is_mandatory: bool
    ordering: int
    options: list[FieldOption] = field(default_factory=list)


@dataclass
class FormSchema:
    """Parsed /expense-form-config/ response for one (tenant, policy, date)."""

    tenant_id: str
    policy_id: int
    receipt_date_bucket: str   # YYYY-MM, since OmniHR allows time-bounded policies
    fields: list[FormField]
    fetched_at: datetime

    def field_by_fdt(self, fdt: StandardFDT) -> FormField | None:
        """Find a standard field by form_data_type. Returns first match."""
        for f in self.fields:
            if f.form_data_type == fdt:
                return f
        return None

    def custom_fields(self) -> list[FormField]:
        return [f for f in self.fields if f.form_data_type == "CUSTOM"]

    def is_stale(self, max_age_hours: int = 24) -> bool:
        return datetime.now(timezone.utc) - self.fetched_at > timedelta(hours=max_age_hours)

    @classmethod
    def from_api(
        cls,
        *,
        tenant_id: str,
        policy_id: int,
        receipt_date: date,
        api_response: dict[str, Any],
    ) -> "FormSchema":
        bucket = receipt_date.strftime("%Y-%m")
        fields = []
        for f in api_response["form"]["fields"]:
            opts = []
            for o in f.get("options") or []:
                opts.append(FieldOption(id=o["id"], label=o["label"], ordering=o["ordering"]))
            fields.append(
                FormField(
                    field_id=f["field_id"],
                    label=f["label"],
                    field_type=f["field_type"],
                    form_data_type=f["form_data_type"],
                    is_mandatory=bool(f["is_mandatory"]),
                    ordering=f["ordering"],
                    options=opts,
                )
            )
        return cls(
            tenant_id=tenant_id,
            policy_id=policy_id,
            receipt_date_bucket=bucket,
            fields=fields,
            fetched_at=datetime.now(timezone.utc),
        )


# In-memory cache for now. Persistent layer (Postgres `schema_cache` table) is
# wired via the SchemaStore protocol below.
_memory_cache: dict[tuple[str, int, str], FormSchema] = {}


class SchemaStore:
    """Persistence interface — implement against Postgres in production."""

    async def get(self, tenant: str, policy_id: int, bucket: str) -> FormSchema | None:
        return _memory_cache.get((tenant, policy_id, bucket))

    async def put(self, schema: FormSchema) -> None:
        _memory_cache[(schema.tenant_id, schema.policy_id, schema.receipt_date_bucket)] = schema

    async def invalidate(self, tenant: str, policy_id: int) -> None:
        for key in list(_memory_cache.keys()):
            if key[0] == tenant and key[1] == policy_id:
                _memory_cache.pop(key, None)


_default_store = SchemaStore()


async def get_schema(
    *,
    client,  # OmniHRClient (forward ref to avoid circular import)
    tenant_id: str,
    policy_id: int,
    receipt_date: date,
    store: SchemaStore | None = None,
) -> FormSchema:
    store = store or _default_store
    bucket = receipt_date.strftime("%Y-%m")
    cached = await store.get(tenant_id, policy_id, bucket)
    if cached and not cached.is_stale():
        return cached
    resp = await client.get_form_config(policy_id=policy_id, receipt_date=receipt_date)
    schema = FormSchema.from_api(
        tenant_id=tenant_id, policy_id=policy_id, receipt_date=receipt_date, api_response=resp
    )
    await store.put(schema)
    return schema


async def invalidate_schema(
    *, tenant_id: str, policy_id: int, store: SchemaStore | None = None
) -> None:
    store = store or _default_store
    await store.invalidate(tenant_id, policy_id)
