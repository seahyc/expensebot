"""Headless OmniHR re-auth via the camofox-browser REST API.

OmniHR caps the refresh token at ~3 days with no rotation (see auth.py), so the
server-side httpx refresh eventually 401s and the only way to mint a brand-new
token is a fresh Google SSO login. This module does that login *headlessly* by
driving a persistent Camoufox profile — seeded once with an interactive Google
login — through OmniHR's "Sign in with Google" flow.

Validated 2026-06-13 on the Oracle VM:
  - The profile's Google session lasts ~13 months, so the SSO redirect
    auto-completes with no password.
  - First grant from an UNTRUSTED profile triggers a Google 2FA step-up
    ("Verify it's you"); after the human clears it once (phone tap / TOTP),
    the profile is device-trusted and subsequent grants complete SILENTLY.

So the calling contract is:
  - try `refresh_or_reauth`; normally it just refreshes over HTTP.
  - at the 3-day cap it falls back here and re-auths silently.
  - if Google challenges (untrusted / risk), it raises `NeedsHumanReauth` so the
    caller can route a one-tap re-auth to the human via the miniapp, then retry.

This talks to camofox-browser (https://github.com/jo-inc/camofox-browser) over
its REST API. On the VM it runs as a memory-capped systemd unit on :9377 with a
persistent profile keyed by `user_id`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

import httpx

from .auth import Tokens, refresh_access_token, tokens_from_cookies
from .exceptions import AuthError, OmniHRError

log = logging.getLogger(__name__)


class NeedsHumanReauth(OmniHRError):
    """Google stepped up to a 2FA challenge during the SSO grant.

    The headless worker can't pass it; route a one-tap re-auth to the human
    (miniapp: "Tap Yes on your phone" / authenticator code), then retry.
    `verify_url` is the live Google challenge URL for that hand-off.
    """

    def __init__(self, message: str, verify_url: str | None = None):
        super().__init__(message)
        self.verify_url = verify_url


class ReauthFailed(OmniHRError):
    """SSO re-auth didn't complete and wasn't a recognizable 2FA challenge
    (popup never appeared, tokens never landed, etc.). Retryable with backoff."""


class CamofoxBrowser:
    """Thin client for the bits of the camofox-browser REST API we need.

    All tab ops are scoped to a persistent `user_id` so cookies + the seeded
    Google session survive browser/daemon restarts (camofox persistence plugin).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9377",
        *,
        user_id: str = "omnihr",
        session_key: str = "reauth",
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.session_key = session_key
        self.timeout = timeout

    def _req(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise ReauthFailed(f"camofox {method} {path} -> {e.code}: {e.read()[:200]!r}")
        except urllib.error.URLError as e:
            raise ReauthFailed(f"camofox unreachable at {self.base_url}: {e}")
        return json.loads(raw) if raw else {}

    def health(self) -> dict:
        return self._req("/health")

    def open_tab(self, url: str) -> str:
        r = self._req("/tabs", "POST", {"userId": self.user_id, "sessionKey": self.session_key, "url": url})
        return r["tabId"]

    def navigate(self, tab: str, url: str) -> None:
        self._req(f"/tabs/{tab}/navigate", "POST", {"userId": self.user_id, "url": url})

    def snapshot(self, tab: str) -> dict:
        return self._req(f"/tabs/{tab}/snapshot?userId={self.user_id}")

    def click(self, tab: str, ref: str) -> None:
        self._req(f"/tabs/{tab}/click", "POST", {"userId": self.user_id, "ref": ref})

    def tabs(self) -> list[dict]:
        r = self._req(f"/tabs?userId={self.user_id}")
        return [t for t in r.get("tabs", []) if isinstance(t, dict)]

    def storage_state(self) -> dict:
        return self._req(f"/sessions/{self.user_id}/storage_state")

    def expire_omnihr_cookies(self) -> None:
        """Force the stored access+refresh cookies to expire — used only by tests
        to simulate the 3-day cap without waiting 3 days."""
        past = [
            {"name": n, "value": "", "domain": "api.omnihr.co", "path": "/", "expires": 100000,
             "httpOnly": True, "secure": True}
            for n in ("access_token", "refresh_token", "is_user_logged_in")
        ]
        self._req(f"/sessions/{self.user_id}/cookies", "POST", {"cookies": past})


def _tab_id(t: dict) -> str | None:
    return t.get("tabId") or t.get("id")


def _find_ref(snapshot: dict, *substrings: str) -> str | None:
    """Return the element ref (e.g. 'e3') of the first snapshot line containing
    any of `substrings` (case-insensitive). camofox snapshot lines look like:
        - button "Sign in as Ying Cong..." [e2]
    """
    for line in snapshot.get("snapshot", "").splitlines():
        low = line.lower()
        if "[e" in line and any(s.lower() in low for s in substrings):
            return "e" + line.split("[e")[1].split("]")[0]
    return None


def _omnihr_tokens_from_state(state: dict) -> tuple[str | None, str | None]:
    access = refresh = None
    for c in state.get("cookies", []):
        if c.get("domain") == "api.omnihr.co":
            if c.get("name") == "access_token":
                access = c.get("value") or None
            elif c.get("name") == "refresh_token":
                refresh = c.get("value") or None
    return access, refresh


def harvest_tokens(browser: CamofoxBrowser) -> Tokens:
    """Read the OmniHR access+refresh cookies out of the browser profile and
    build a Tokens (expiries parsed from the JWTs)."""
    access, refresh = _omnihr_tokens_from_state(browser.storage_state())
    if not access or not refresh:
        raise ReauthFailed("OmniHR tokens not present in profile after re-auth")
    return tokens_from_cookies(access, refresh)


def silent_reauth(
    browser: CamofoxBrowser,
    *,
    tenant: str = "glints",
    account_email: str | None = None,
    poll_timeout: float = 30.0,
) -> Tokens:
    """Drive the persistent Camoufox profile through OmniHR's Google SSO and
    return fresh Tokens.

    Raises NeedsHumanReauth if Google shows a 2FA challenge, ReauthFailed if the
    flow doesn't complete for any other reason.
    """
    base = f"https://{tenant}.omnihr.co"
    tab = browser.open_tab(f"{base}/dashboard/")
    time.sleep(4)

    # Already logged in? (token still within the 3-day window)
    access, refresh = _omnihr_tokens_from_state(browser.storage_state())
    if access and refresh and browser.snapshot(tab).get("url", "").rstrip("/").endswith("/dashboard"):
        log.info("omnihr browser session already live; harvesting")
        return tokens_from_cookies(access, refresh)

    snap = browser.snapshot(tab)
    # Dismiss the "Your session has expired" modal if present.
    ok = _find_ref(snap, '"Ok"')
    if ok:
        browser.click(tab, ok)
        time.sleep(2)
        snap = browser.snapshot(tab)

    gbtn = _find_ref(snap, "Sign in as", "Sign in with Google")
    if not gbtn:
        raise ReauthFailed(f"no Google sign-in control on {snap.get('url')}")

    before = {_tab_id(t) for t in browser.tabs()}
    browser.click(tab, gbtn)

    # The GIS button opens the account chooser in a new tab.
    popup = _wait_for_popup(browser, exclude=before, timeout=poll_timeout)
    if popup is None:
        # Some flows complete in-place (no popup). Give it a moment, then check.
        toks = _wait_for_tokens(browser, timeout=poll_timeout)
        if toks:
            return toks
        raise ReauthFailed("SSO produced no popup and no tokens")

    psnap = browser.snapshot(popup)
    acc = _find_ref(psnap, account_email) if account_email else None
    acc = acc or _find_ref(psnap, "Seah", "@glints.com", "@")  # first listed account
    if acc:
        browser.click(popup, acc)

    # Detect a 2FA step-up before waiting on tokens.
    challenge_url = _wait_for_challenge_or_close(browser, popup, timeout=12.0)
    if challenge_url:
        raise NeedsHumanReauth(
            "Google 2FA step-up during OmniHR SSO; needs one human verification",
            verify_url=challenge_url,
        )

    toks = _wait_for_tokens(browser, timeout=poll_timeout)
    if toks:
        log.info("silent omnihr re-auth succeeded (refresh exp %s)", toks.refresh_expires_at)
        return toks
    raise ReauthFailed("SSO completed without minting OmniHR tokens")


def _wait_for_popup(browser: CamofoxBrowser, *, exclude: set, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for t in browser.tabs():
            tid = _tab_id(t)
            if tid not in exclude and "google" in (t.get("url") or ""):
                return tid
        time.sleep(1.5)
    return None


def _wait_for_challenge_or_close(browser: CamofoxBrowser, popup: str, *, timeout: float) -> str | None:
    """After selecting the account, watch the popup: if it lands on a Google
    /challenge page, return that URL (2FA step-up); if it transforms/closes
    cleanly, return None (silent success path)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = browser.snapshot(popup).get("url", "")
        except ReauthFailed:
            return None  # popup closed = grant completed
        if "/challenge" in url:
            return url
        if "/gsi/transform" in url or not url:
            return None
        time.sleep(1.5)
    return None


def _wait_for_tokens(browser: CamofoxBrowser, *, timeout: float) -> Tokens | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        access, refresh = _omnihr_tokens_from_state(browser.storage_state())
        if access and refresh:
            return tokens_from_cookies(access, refresh)
        time.sleep(2)
    return None


async def refresh_or_reauth(
    http: httpx.AsyncClient,
    tokens: Tokens,
    *,
    browser: CamofoxBrowser | None = None,
    tenant: str = "glints",
    account_email: str | None = None,
) -> Tokens:
    """Primary entrypoint for the sweeper.

    1. Try the cheap server-side httpx refresh (no browser).
    2. If that 401s (3-day cap hit) and a browser is configured, fall back to a
       silent headless SSO re-auth to mint a brand-new 3-day token.

    NeedsHumanReauth propagates so the caller can ping the user via the miniapp.
    """
    try:
        return await refresh_access_token(http, tokens.refresh_token)
    except AuthError:
        if browser is None:
            raise
        log.info("httpx refresh rejected (3-day cap); attempting silent browser re-auth")
        # silent_reauth is blocking I/O against the local camofox daemon; run it
        # off the event loop so we don't stall other connectors.
        import asyncio

        return await asyncio.to_thread(
            silent_reauth, browser, tenant=tenant, account_email=account_email
        )


def _cli() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Headless OmniHR re-auth via camofox-browser")
    p.add_argument("--base-url", default="http://localhost:9377")
    p.add_argument("--user", default="omnihr", help="camofox persistent profile id")
    p.add_argument("--tenant", default="glints")
    p.add_argument("--account-email", default=None)
    p.add_argument("--simulate-expiry", action="store_true",
                   help="expire stored omnihr cookies first (test the 3-day-cap path)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    b = CamofoxBrowser(a.base_url, user_id=a.user)
    h = b.health()
    log.info("camofox health: connected=%s", h.get("browserConnected"))
    if a.simulate_expiry:
        b.expire_omnihr_cookies()
        log.info("expired stored omnihr cookies (simulating 3-day cap)")
    try:
        t = silent_reauth(b, tenant=a.tenant, account_email=a.account_email)
        log.info("OK — minted tokens: access exp %s, refresh exp %s",
                 t.access_expires_at, t.refresh_expires_at)
    except NeedsHumanReauth as e:
        log.warning("NEEDS HUMAN: %s | verify_url=%s", e, e.verify_url)
        raise SystemExit(2)
    except OmniHRError as e:
        log.error("re-auth failed: %s", e)
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
