"""Background job: every 6h, refresh JWTs for users whose access tokens are
within an hour of expiry. Avoids users hitting expiry mid-action.

Stub. v1 implementation:
  - Iterate users where tokens.access_expires_at < now + 1h
  - For each: omnihr_client.auth.refresh_access_token
  - On AuthError: mark user as 'needs_repair', DM them
"""

import asyncio


async def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    asyncio.run(main())
