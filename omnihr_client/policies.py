"""Policy catalog — flat list of (id, label, category) for a tenant.

Fetched from `/expense/3.0/category/policy-tree/{employee_id}/` via
OmniHRClient.policy_tree() and cached in-memory per (tenant, employee)
for 24h. The parser uses this to constrain `suggested_policy_id` to a
known-valid enum, so Haiku can't hedge its classification with null.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class PolicyEntry:
    id: int
    label: str            # policy name, e.g. "Client Meetings and Entertainment"
    category: str         # parent category name


# Key = tenant_id (we assume every employee in the same tenant sees the
# same policy tree; true for OmniHR SSO-based tenants).
_cache: dict[str, tuple[list[PolicyEntry], datetime]] = {}
_TTL = timedelta(hours=24)


def _flatten(tree: list[dict[str, Any]]) -> list[PolicyEntry]:
    """Turn the nested policy-tree response into a flat list."""
    out: list[PolicyEntry] = []
    for cat in tree or []:
        cat_name = (cat.get("category") or {}).get("name") or cat.get("name") or "?"
        for pol in cat.get("policies") or []:
            pid = pol.get("id")
            if pid is None:
                continue
            out.append(
                PolicyEntry(
                    id=int(pid),
                    label=pol.get("name") or f"Policy {pid}",
                    category=cat_name,
                )
            )
    return out


async def get_policies(client, tenant_id: str) -> list[PolicyEntry]:
    """Return the cached flat policy list for this tenant, fetching on miss."""
    now = datetime.now(timezone.utc)
    hit = _cache.get(tenant_id)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    tree = await client.policy_tree()
    flat = _flatten(tree)
    _cache[tenant_id] = (flat, now)
    return flat


def invalidate(tenant_id: str | None = None) -> None:
    """Drop the cache for a tenant (or all tenants if None). Called by the
    nightly refresher once implemented."""
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)
