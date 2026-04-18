"""Policy list with 24h in-memory cache.

Fetches the OmniHR policy tree and flattens it into a list of PolicyEntry
objects. Used to give the parser an enum of valid policy IDs so it can't
return null when a receipt clearly belongs to one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_CACHE_TTL = timedelta(hours=24)
_cache: dict[str, tuple[list["PolicyEntry"], datetime]] = {}


@dataclass
class PolicyEntry:
    id: int
    label: str
    category: str


async def get_policies(client: Any, tenant_id: str) -> list[PolicyEntry]:
    now = datetime.now(timezone.utc)
    hit = _cache.get(tenant_id)
    if hit:
        entries, fetched_at = hit
        if now - fetched_at < _CACHE_TTL:
            return entries
    tree = await client.policy_tree()
    entries = _flatten(tree)
    _cache[tenant_id] = (entries, now)
    return entries


def invalidate(tenant_id: str) -> None:
    _cache.pop(tenant_id, None)


def _flatten(tree: list[dict[str, Any]]) -> list[PolicyEntry]:
    """Flatten a nested policy tree into a flat list.

    OmniHR returns categories (top-level) each containing policies.
    Handles both shapes we've seen:
      { id, name, expense_policies: [{id, name}] }
      { id, name, policies: [{id, name}] }
    """
    out: list[PolicyEntry] = []
    for node in tree or []:
        cat = node.get("name") or node.get("label") or node.get("category_name") or ""
        # The node itself might be a leaf policy
        nid = node.get("id") or node.get("policy_id")
        nlabel = node.get("name") or node.get("label") or node.get("policy_name") or ""
        # Only treat as a policy if it has no children arrays (i.e., it IS a leaf)
        child_keys = ("expense_policies", "policies", "sub_categories", "children")
        has_children = any(node.get(k) for k in child_keys)
        if nid and nlabel and not has_children:
            out.append(PolicyEntry(id=int(nid), label=str(nlabel), category=""))

        for key in child_keys:
            for child in (node.get(key) or []):
                cid = child.get("id") or child.get("policy_id")
                clabel = (
                    child.get("name")
                    or child.get("label")
                    or child.get("policy_name")
                    or ""
                )
                if cid and clabel:
                    out.append(PolicyEntry(id=int(cid), label=str(clabel), category=cat))
    return out
