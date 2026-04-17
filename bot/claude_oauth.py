"""Claude OAuth PKCE flow — seamless subscription auth.

Instead of proxying `claude auth login` (fragile subprocess + paste flow),
we do the OAuth PKCE exchange ourselves:

  1. Generate code_verifier + code_challenge
  2. Build authorize URL with OUR redirect_uri
  3. User taps → authorizes on claude.com
  4. Claude redirects to https://expensebot.seahyingcong.com/auth/callback?code=XXX
  5. Our server exchanges code for tokens
  6. Tokens stored → user logged in
  7. Redirect user back to Telegram

Uses Claude Code's public client_id (PKCE = no client_secret needed).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# Claude Code's public OAuth client — PKCE, no client_secret
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
# Token endpoint — derived from authorize URL pattern
TOKEN_URL = "https://claude.com/cai/oauth/token"

SCOPES = "user:profile user:inference"

# In-memory pending sessions: {state: PendingAuth}
_pending: dict[str, "PendingAuth"] = {}


@dataclass
class PendingAuth:
    state: str
    code_verifier: str
    redirect_uri: str
    telegram_user_id: int
    user_db_id: int
    created_at: datetime


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def start_login(
    *,
    telegram_user_id: int,
    user_db_id: int,
    public_base_url: str,
) -> tuple[str, str]:
    """Start OAuth flow. Returns (authorize_url, state).

    The authorize_url is what we send to the user in Telegram.
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)
    redirect_uri = f"{public_base_url}/auth/callback"

    _pending[state] = PendingAuth(
        state=state,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        telegram_user_id=telegram_user_id,
        user_db_id=user_db_id,
        created_at=datetime.now(timezone.utc),
    )

    # Clean up expired sessions (> 10 min)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    for k in list(_pending):
        if _pending[k].created_at < cutoff:
            del _pending[k]

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = AUTHORIZE_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return url, state


async def complete_login(state: str, code: str) -> tuple[bool, str, dict | None]:
    """Exchange auth code for tokens. Returns (success, message, token_data)."""
    pending = _pending.pop(state, None)
    if not pending:
        return False, "Session expired or invalid. Run /login again.", None

    # Exchange code for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.code_verifier,
            },
        )

    if resp.status_code != 200:
        log.warning("Token exchange failed: %s %s", resp.status_code, resp.text[:200])
        return False, f"Token exchange failed ({resp.status_code}). Try /login again.", None

    data = resp.json()
    # Expected: {access_token, refresh_token, token_type, expires_in, ...}
    access = data.get("access_token")
    if not access:
        return False, f"No access_token in response. Try /login again.", None

    return True, "OK", {
        "access_token": access,
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "telegram_user_id": pending.telegram_user_id,
        "user_db_id": pending.user_db_id,
    }
