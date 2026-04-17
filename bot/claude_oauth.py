"""Pure OAuth PKCE flow for Claude subscription auth.

No subprocess, no `claude` CLI, no shared files. Scales to unlimited users.

Flow:
  1. Server generates PKCE code_verifier + code_challenge (in-memory)
  2. Builds authorize URL with Claude Code's public client_id +
     the registered redirect_uri (platform.claude.com — can't change)
  3. User authorizes → lands on callback page showing ?code=XXX
  4. User pastes URL into our web page → POST to /auth/complete
  5. Server exchanges code for tokens (one HTTP POST to token endpoint)
  6. Tokens stored per-user, encrypted in DB
  7. Auto-refresh when access_token expires
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Claude Code's public OAuth client (PKCE = no client_secret)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://claude.com/cai/oauth/token"
# Registered redirect — we MUST use this exact value
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

# In-memory pending sessions: {state: PendingAuth}
_pending: dict[str, "PendingAuth"] = {}


@dataclass
class PendingAuth:
    state: str
    code_verifier: str
    telegram_user_id: int
    user_db_id: int
    created_at: datetime


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier (128 chars) and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(96)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_login(*, telegram_user_id: int, user_db_id: int) -> str:
    """Start OAuth flow. Returns the full authorize URL to send to the user.

    Thread-safe — each call creates independent state. Multiple users can
    /login concurrently with zero conflicts.
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    _pending[state] = PendingAuth(
        state=state,
        code_verifier=verifier,
        telegram_user_id=telegram_user_id,
        user_db_id=user_db_id,
        created_at=datetime.now(timezone.utc),
    )

    # Purge expired sessions (> 10 min)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    for k in list(_pending):
        if _pending[k].created_at < cutoff:
            del _pending[k]

    from urllib.parse import urlencode
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "code": "true",  # tells the callback page to display the code
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", state


async def exchange_code(state: str, code: str) -> tuple[bool, str, dict[str, Any] | None]:
    """Exchange authorization code for tokens. Pure HTTP, no subprocess.

    Returns (success, message, token_data).
    token_data keys: access_token, refresh_token, expires_in, user_db_id, telegram_user_id
    """
    pending = _pending.pop(state, None)
    if not pending:
        return False, "Session expired or invalid. Run /login again.", None

    # Token exchange — one HTTP POST
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": pending.code_verifier,
                },
            )
        except httpx.HTTPError as e:
            log.warning("Token exchange HTTP error: %s", e)
            return False, f"Network error during token exchange. Try /login again.", None

    if resp.status_code != 200:
        log.warning("Token exchange failed: %s %s", resp.status_code, resp.text[:300])
        return False, f"Token exchange failed ({resp.status_code}). Try /login again.", None

    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        log.warning("No access_token in response: %s", list(data.keys()))
        return False, "No access_token returned. Try /login again.", None

    return True, "OK", {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "token_type": data.get("token_type"),
        "user_db_id": pending.user_db_id,
        "telegram_user_id": pending.telegram_user_id,
    }


async def refresh_token(stored_refresh_token: str) -> tuple[bool, dict[str, Any] | None]:
    """Refresh an expired access_token. Returns (success, new_token_data)."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": stored_refresh_token,
                },
            )
        except httpx.HTTPError as e:
            log.warning("Token refresh error: %s", e)
            return False, None

    if resp.status_code != 200:
        log.warning("Token refresh failed: %s", resp.status_code)
        return False, None

    data = resp.json()
    return True, {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
    }
