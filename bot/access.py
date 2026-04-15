"""Access control.

Three env vars control who can use the bot:

  PRIVATE_MODE=true       — only ADMIN_TELEGRAM_USER_IDS may interact at all.
                            Useful during testing / invite-only launch.
  ADMIN_TELEGRAM_USER_IDS — comma-separated Telegram user ids. Admins bypass
                            all restrictions.
  ALLOWED_EMAIL_DOMAINS   — comma-separated. After /pair, user's OmniHR email
                            must end in one of these or the session is
                            immediately unpaired. Empty = allow any.

  BANNED_TELEGRAM_USER_IDS — comma-separated hard-blocklist.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


def _split(env: str) -> set[str]:
    return {x.strip() for x in os.environ.get(env, "").split(",") if x.strip()}


@dataclass(frozen=True)
class Policy:
    private_mode: bool
    admin_ids: set[str]
    allowed_email_domains: set[str]
    banned_ids: set[str]


def load() -> Policy:
    return Policy(
        private_mode=os.environ.get("PRIVATE_MODE", "").lower() in ("1", "true", "yes"),
        admin_ids=_split("ADMIN_TELEGRAM_USER_IDS"),
        allowed_email_domains={d.lower() for d in _split("ALLOWED_EMAIL_DOMAINS")},
        banned_ids=_split("BANNED_TELEGRAM_USER_IDS"),
    )


def is_admin(telegram_user_id: str | int, p: Policy | None = None) -> bool:
    p = p or load()
    return str(telegram_user_id) in p.admin_ids


def is_allowed(telegram_user_id: str | int, p: Policy | None = None) -> tuple[bool, str]:
    """First gate — can this user interact at all?
    Returns (allowed, reason-if-denied)."""
    p = p or load()
    tid = str(telegram_user_id)
    if tid in p.banned_ids:
        return False, "You have been banned from this bot."
    if p.private_mode and tid not in p.admin_ids:
        return False, (
            "This bot is in private mode. Self-host from "
            "github.com/seahyc/expensebot — it's ~10 min to set up your own."
        )
    return True, ""


def email_allowed(email: str | None, p: Policy | None = None) -> tuple[bool, str]:
    """Second gate — after pairing we know the OmniHR email. Enforce domain allowlist."""
    p = p or load()
    if not p.allowed_email_domains:
        return True, ""
    if not email:
        return False, "Couldn't read your OmniHR email — pair aborted."
    domain = email.lower().rsplit("@", 1)[-1]
    if domain in p.allowed_email_domains:
        return True, ""
    return False, (
        f"Your OmniHR email ({email}) isn't in the allowed domain list "
        f"({', '.join(sorted(p.allowed_email_domains))}). "
        "Ask the admin to add your domain, or self-host."
    )
