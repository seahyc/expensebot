"""Per-user sliding-window rate limiter. In-memory; fine for one-host deploy.

Limits cover the expensive / abusable operations:
  - receipt parse (Claude call)
  - create_draft (writes to OmniHR)
  - omnihr list  (less costly but chatty-spam-abuse target)
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class Rule:
    max_calls: int
    window_seconds: int


DEFAULT_RULES: dict[str, Rule] = {
    "parse": Rule(max_calls=30, window_seconds=3600),        # 30 receipts/hour/user
    "create_draft": Rule(max_calls=30, window_seconds=3600),
    "list": Rule(max_calls=60, window_seconds=60),
    "pair": Rule(max_calls=5, window_seconds=600),           # 5 pair attempts / 10min
    "setkey": Rule(max_calls=10, window_seconds=600),
    # IP-scoped buckets for unauthenticated HTTP endpoints. Keep these tight —
    # the pairing code is only 6 digits (1M space) so brute-forcing must be
    # made infeasible within the 5-min TTL.
    "ip_pair": Rule(max_calls=10, window_seconds=600),       # 10 POSTs / 10min / IP
}


_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def check(
    user_id: int | str, kind: str, *, rules: dict[str, Rule] | None = None
) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). retry_after=0 when allowed.

    `user_id` is an opaque bucket key — use the DB user id for per-user buckets
    and the client IP for anonymous HTTP endpoints.

    Call on every attempt. Records the timestamp only when allowed (so retries
    after rate-limit don't worsen the backoff)."""
    rules = rules or DEFAULT_RULES
    rule = rules.get(kind)
    if not rule:
        return True, 0
    now = time.time()
    key = (str(user_id), kind)
    q = _buckets[key]
    cutoff = now - rule.window_seconds
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= rule.max_calls:
        retry = int(q[0] + rule.window_seconds - now) + 1
        return False, max(retry, 1)
    q.append(now)
    return True, 0


def reset(user_id: int | str, kind: str | None = None) -> None:
    """For tests / admin ops."""
    sid = str(user_id)
    if kind is None:
        for k in list(_buckets.keys()):
            if k[0] == sid:
                _buckets.pop(k, None)
    else:
        _buckets.pop((sid, kind), None)
