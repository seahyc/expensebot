"""Background job: nightly per tenant, re-fetch schema for all policies used
in the last 30 days. Diff against cached. On detected drift (esp. new
mandatory fields), DM the tenant shepherd to update tenants/<org>.md.

This is the "agentic" tenant config maintenance — bot proactively learns
when HR changes the form.
"""

import asyncio


async def refresh_tenant(tenant_id: str) -> None:
    raise NotImplementedError


async def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    asyncio.run(main())
