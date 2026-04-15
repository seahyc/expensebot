"""JWT lifecycle for OmniHR.

OmniHR sets HttpOnly cookies for access_token + refresh_token on api.omnihr.co.
We never see the cookies in browser JS — extension reads them via chrome.cookies
and pushes to backend. Backend uses them as plain cookies in httpx.

Access token TTL: ~15 minutes
Refresh token TTL: ~30 days
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .exceptions import AuthError

log = logging.getLogger(__name__)

# JWT payload: {"token_type":"access","exp":<unix>,"jti":"...","user_id":<int>}
# We don't decode/verify locally — just track refresh schedule.


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime  # parsed from JWT exp
    refresh_expires_at: datetime

    @property
    def access_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.access_expires_at - timedelta(seconds=30)

    @property
    def refresh_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.refresh_expires_at


def parse_jwt_exp(token: str) -> datetime:
    """Decode the exp claim without verifying signature (we trust the source)."""
    import base64
    import json

    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)


def tokens_from_cookies(access: str, refresh: str) -> Tokens:
    return Tokens(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=parse_jwt_exp(access),
        refresh_expires_at=parse_jwt_exp(refresh),
    )


async def refresh_access_token(client: httpx.AsyncClient, refresh_token: str) -> Tokens:
    """Use refresh cookie to get a new access token. SimpleJWT-style endpoint.

    OmniHR's /auth/token/refresh/ reads the refresh cookie and Sets new access
    + refresh cookies in the response. We extract them from Set-Cookie.
    """
    cookies = {"refresh_token": refresh_token}
    resp = await client.post(
        "/auth/token/refresh/",
        cookies=cookies,
        headers={"Content-Type": "application/json"},
        json={},
    )
    if resp.status_code == 401:
        raise AuthError("Refresh token rejected — user must re-pair")
    resp.raise_for_status()

    new_access = resp.cookies.get("access_token")
    new_refresh = resp.cookies.get("refresh_token") or refresh_token
    if not new_access:
        raise AuthError("Refresh succeeded but no access_token cookie returned")
    return tokens_from_cookies(new_access, new_refresh)


async def login_password(
    client: httpx.AsyncClient, username: str, password: str
) -> Tokens:
    """Password login — only works for tenants without SSO-only enforcement."""
    resp = await client.post(
        "/auth/token/",
        json={"username": username, "password": password},
    )
    if resp.status_code == 401:
        raise AuthError("Invalid credentials")
    resp.raise_for_status()
    access = resp.cookies.get("access_token")
    refresh = resp.cookies.get("refresh_token")
    if not access or not refresh:
        raise AuthError("Login succeeded but cookies missing")
    return tokens_from_cookies(access, refresh)


async def login_google(
    client: httpx.AsyncClient, credential: str, client_id: str
) -> Tokens:
    """Google SSO login — credential is the Google-issued ID token, client_id
    is OmniHR's Google clientId. ONLY usable when the credential was issued
    for OmniHR's clientId from a whitelisted origin. Practically unusable
    from a third-party domain — kept here for completeness and for use by the
    extension in cases where OmniHR hosts an OAuth helper."""
    resp = await client.post(
        "/auth/token/google/",
        json={"credential": credential, "clientId": client_id},
    )
    if resp.status_code == 401:
        raise AuthError("Google credential rejected")
    resp.raise_for_status()
    access = resp.cookies.get("access_token")
    refresh = resp.cookies.get("refresh_token")
    return tokens_from_cookies(access, refresh)


async def logout(client: httpx.AsyncClient, refresh_token: str) -> None:
    cookies = {"refresh_token": refresh_token}
    await client.post("/auth/logout/", cookies=cookies)
