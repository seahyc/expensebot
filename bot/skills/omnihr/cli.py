#!/usr/bin/env python3
"""Local CLI for OmniHR — for testing outside Telegram."""
import argparse, asyncio, os, sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from omnihr_client import OmniHRClient
from omnihr_client.auth import tokens_from_cookies


async def main():
    parser = argparse.ArgumentParser(description="OmniHR local CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_p = sub.add_parser("list", help="List claims")
    list_p.add_argument(
        "--status",
        default="draft",
        choices=["draft", "pending", "approved", "reimbursed", "all"],
    )

    submit_p = sub.add_parser("submit", help="Submit a draft claim")
    submit_p.add_argument("--id", type=int, required=True)

    delete_p = sub.add_parser("delete", help="Delete a claim")
    delete_p.add_argument("--id", type=int, required=True)

    args = parser.parse_args()

    # Get credentials
    access_jwt = os.getenv("OMNIHR_ACCESS_JWT")
    refresh_jwt = os.getenv("OMNIHR_REFRESH_JWT")
    employee_id = os.getenv("OMNIHR_EMPLOYEE_ID")
    tenant_id = os.getenv("OMNIHR_TENANT_ID", "glints")

    if not access_jwt:
        # Try reading from DB (works only if ENCRYPTION_KEY is set in env)
        try:
            import sqlite3

            db_path = Path(__file__).parent.parent.parent.parent / "expensebot.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT access_jwt, refresh_jwt, omnihr_employee_id, tenant_id "
                "FROM users WHERE access_jwt IS NOT NULL LIMIT 1"
            ).fetchone()
            if row:
                from bot.crypto import decrypt

                access_jwt = decrypt(row["access_jwt"])
                refresh_jwt = decrypt(row["refresh_jwt"])
                employee_id = employee_id or str(row["omnihr_employee_id"] or "")
                tenant_id = row["tenant_id"] or tenant_id
        except Exception as e:
            print(f"Could not read credentials from DB: {e}", file=sys.stderr)
            sys.exit(1)

    if not access_jwt:
        print("No credentials found. Set OMNIHR_ACCESS_JWT env var.", file=sys.stderr)
        sys.exit(1)
    if not refresh_jwt:
        print("No refresh token. Set OMNIHR_REFRESH_JWT env var.", file=sys.stderr)
        sys.exit(1)
    if not employee_id:
        print(
            "No employee_id. Set OMNIHR_EMPLOYEE_ID env var.", file=sys.stderr
        )
        sys.exit(1)

    tokens = tokens_from_cookies(access_jwt, refresh_jwt)

    async with OmniHRClient(
        tokens=tokens,
        employee_id=int(employee_id),
        tenant_id=tenant_id,
    ) as client:
        if args.cmd == "list":
            status_map = {
                "draft": "3",
                "pending": "4",
                "approved": "7",
                "reimbursed": "5",
                "all": "3,4,7,5,6",
            }
            data = await client.list_submissions(
                status_filters=status_map[args.status]
            )
            items = data.get("results", [])
            if not items:
                print("No claims found.")
            for item in items:
                status_label = item.get("status_label") or item.get("status", "?")
                merchant = item.get("merchant") or item.get("name") or "?"
                amount = item.get("claim_amount") or item.get("amount", "?")
                currency = item.get("currency", "")
                print(
                    f"#{item.get('id')} [{status_label}] {merchant} "
                    f"{amount} {currency}".rstrip()
                )
        elif args.cmd == "submit":
            result = await client.submit_draft(args.id)
            print(f"Submitted: {result}")
        elif args.cmd == "delete":
            await client.delete_submission(args.id)
            print(f"Deleted #{args.id}")


if __name__ == "__main__":
    asyncio.run(main())
