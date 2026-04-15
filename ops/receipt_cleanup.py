"""Background job: every hour, delete receipt files older than 24h from S3.

Receipts in OmniHR are the authoritative copy. We only need a brief local cache
for retries + dupe detection (which uses hashes, not files).
"""

import asyncio


async def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    asyncio.run(main())
