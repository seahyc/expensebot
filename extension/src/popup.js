// Popup logic: detect logged-in omnihr.co session, accept pairing code, push to backend.

// For local dev, override via:
//   chrome.storage.local.set({backend: "http://localhost:8000"})
const DEFAULT_BACKEND = "https://expensebot.seahyingcong.com";
const BACKEND = await chrome.storage.local.get("backend").then(r => r.backend || DEFAULT_BACKEND);

const statusEl = document.getElementById("status");
const codeEl = document.getElementById("code");
const pairBtn = document.getElementById("pair");

const googleStatusEl = document.getElementById("google-status");
const googleCodeEl = document.getElementById("google-code");
const googleConnectBtn = document.getElementById("google-connect");

// Fetch Google client_id from backend so it doesn't need to be hardcoded here.
let googleClientId = null;
try {
  const cfg = await fetch(`${BACKEND}/config/google`).then(r => r.json());
  googleClientId = cfg.client_id || null;
} catch (_) {}

if (!googleClientId) {
  googleStatusEl.className = "status warn";
  googleStatusEl.textContent = "Google integration not configured.";
  googleConnectBtn.disabled = true;
}

async function getOmniHRCookies() {
  const access = await chrome.cookies.get({ url: "https://api.omnihr.co/", name: "access_token" });
  const refresh = await chrome.cookies.get({ url: "https://api.omnihr.co/", name: "refresh_token" });
  return { access: access?.value, refresh: refresh?.value };
}

async function fetchAuthDetails(accessToken) {
  // Cookie is HttpOnly so we can't set it from JS. But the cookies are auto-sent
  // for *.omnihr.co requests. Use credentials:include.
  const r = await fetch("https://api.omnihr.co/api/v1/auth/details/", {
    credentials: "include",
  });
  if (!r.ok) return null;
  return r.json();
}

async function init() {
  const { access, refresh } = await getOmniHRCookies();
  if (!access || !refresh) {
    statusEl.className = "status warn";
    statusEl.textContent = "Open omnihr.co and sign in first.";
    return;
  }
  const me = await fetchAuthDetails(access);
  if (!me) {
    statusEl.className = "status warn";
    statusEl.textContent = "Session not active — try signing in to omnihr.co again.";
    return;
  }
  statusEl.className = "status ok";
  statusEl.textContent = `Signed in as ${me.full_name} (${me.org?.name ?? "?"}).`;
  pairBtn.disabled = false;
  pairBtn.dataset.employeeId = me.id;
  pairBtn.dataset.org = JSON.stringify(me.org ?? {});
}

pairBtn.addEventListener("click", async () => {
  const code = codeEl.value.trim();
  if (!/^\d{6}$/.test(code)) {
    statusEl.className = "status err";
    statusEl.textContent = "Code must be 6 digits.";
    return;
  }
  const { access, refresh } = await getOmniHRCookies();
  pairBtn.disabled = true;
  statusEl.className = "status warn";
  statusEl.textContent = "Pairing…";
  try {
    const r = await fetch(`${BACKEND}/extension/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pairing_code: code,
        access_token: access,
        refresh_token: refresh,
        employee_id: Number(pairBtn.dataset.employeeId),
        org: JSON.parse(pairBtn.dataset.org || "{}"),
      }),
    });
    if (!r.ok) throw new Error(await r.text());
    statusEl.className = "status ok";
    statusEl.textContent = "✅ Paired. Check your bot.";
  } catch (e) {
    statusEl.className = "status err";
    statusEl.textContent = `Pair failed: ${e.message}`;
    pairBtn.disabled = false;
  }
});

googleConnectBtn.addEventListener("click", async () => {
  const code = googleCodeEl.value.trim();
  if (!/^\d{6}$/.test(code)) {
    googleStatusEl.className = "status err";
    googleStatusEl.textContent = "Code must be 6 digits.";
    return;
  }
  if (!googleClientId) {
    googleStatusEl.className = "status err";
    googleStatusEl.textContent = "Google not configured — contact your admin.";
    return;
  }

  googleConnectBtn.disabled = true;
  googleStatusEl.className = "status warn";
  googleStatusEl.textContent = "Opening Google sign-in…";

  const redirectUri = `https://${chrome.runtime.id}.chromiumapp.org/`;
  const scopes = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "email",
    "profile",
  ].join(" ");

  const authUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  authUrl.searchParams.set("client_id", googleClientId);
  authUrl.searchParams.set("redirect_uri", redirectUri);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("scope", scopes);
  authUrl.searchParams.set("access_type", "offline");
  authUrl.searchParams.set("prompt", "consent");

  try {
    const responseUrl = await chrome.identity.launchWebAuthFlow({
      url: authUrl.toString(),
      interactive: true,
    });
    const params = new URL(responseUrl).searchParams;
    const authCode = params.get("code");
    if (!authCode) throw new Error("No auth code in response");

    googleStatusEl.textContent = "Connecting…";
    const r = await fetch(`${BACKEND}/extension/google-auth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pairing_code: code,
        auth_code: authCode,
        redirect_uri: redirectUri,
      }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    googleStatusEl.className = "status ok";
    googleStatusEl.textContent = `✅ Connected${data.email ? " as " + data.email : ""}.`;
    googleCodeEl.value = "";
  } catch (e) {
    googleStatusEl.className = "status err";
    googleStatusEl.textContent = `Failed: ${e.message}`;
    googleConnectBtn.disabled = false;
  }
});

init();
