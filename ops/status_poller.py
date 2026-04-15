"""Background job: every 15 min per active user, diff OmniHR submissions vs
last seen, DM the user when status changes (approved / rejected / paid).

Stub. v1 implementation should:
  - Iterate users with valid refresh tokens (not expired)
  - Fetch /submissions/?status_filters=3,1,2,5 — recent N
  - Compare each submission's status to db.submissions.last_seen_status
  - On change: write status_event row, DM user via channel adapter
  - Update last_seen_status
"""

import asyncio


async def poll_user(user) -> None:
    raise NotImplementedError


async def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    asyncio.run(main())
