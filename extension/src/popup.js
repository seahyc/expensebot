// Popup logic: detect logged-in omnihr.co session, accept pairing code, push to backend.

const DEFAULT_BACKEND = "https://expensebot.seahyingcong.com";
const BACKEND = await chrome.storage.local.get("backend").then(r => r.backend || DEFAULT_BACKEND);

// --- Element refs ---
const statusEl = document.getElementById("status");
const codeEl = document.getElementById("code");
const pairBtn = document.getElementById("pair");

const googleStatusEl = document.getElementById("google-status");
const googleCodeEl = document.getElementById("google-code");
const googleConnectBtn = document.getElementById("google-connect");

const tgStatusEl = document.getElementById("tg-status");
const tgStep1El = document.getElementById("tg-step1");
const tgStep2El = document.getElementById("tg-step2");
const tgCodeEl = document.getElementById("tg-code");
const tgPhoneEl = document.getElementById("tg-phone");
const tgInitBtn = document.getElementById("tg-init");
const tgOtpEl = document.getElementById("tg-otp");
const tgVerifyBtn = document.getElementById("tg-verify");

const waStatusEl = document.getElementById("wa-status");
const waStep1El = document.getElementById("wa-step1");
const waQrContainer = document.getElementById("wa-qr-container");
const waQrImg = document.getElementById("wa-qr");
const waQrLoading = document.getElementById("wa-qr-loading");
const waCodeEl = document.getElementById("wa-code");
const waInitBtn = document.getElementById("wa-init");

// --- Status refresh from backend ---
async function refreshStatus() {
  const { ext_session } = await chrome.storage.local.get("ext_session");
  if (!ext_session) return;
  try {
    const r = await fetch(`${BACKEND}/extension/status?token=${ext_session}`);
    if (!r.ok) return;
    applyStatus(await r.json());
  } catch (_) {}
}

function applyStatus(s) {
  if (s.paired) {
    statusEl.className = "status ok";
    statusEl.textContent = `✅ Paired as ${s.name || "?"}`;
    codeEl.style.display = "none";
    pairBtn.style.display = "none";
  }

  if (s.google) {
    googleStatusEl.className = "status ok";
    googleStatusEl.textContent = `✅ Connected${s.google_email ? " as " + s.google_email : ""}`;
    googleCodeEl.style.display = "none";
    googleConnectBtn.style.display = "none";
  }

  if (s.telegram) {
    tgStatusEl.className = "status ok";
    tgStatusEl.textContent = `✅ Connected${s.telegram_phone ? " (" + s.telegram_phone + ")" : ""}`;
    tgStep1El.style.display = "none";
    tgStep2El.style.display = "none";
  }

  if (s.whatsapp) {
    waStatusEl.className = "status ok";
    waStatusEl.textContent = `✅ Connected${s.whatsapp_phone ? " (" + s.whatsapp_phone + ")" : ""}`;
    waStep1El.style.display = "none";
    waQrContainer.style.display = "none";
  }
}

// --- OmniHR init ---
async function getOmniHRCookies() {
  const access = await chrome.cookies.get({ url: "https://api.omnihr.co/", name: "access_token" });
  const refresh = await chrome.cookies.get({ url: "https://api.omnihr.co/", name: "refresh_token" });
  return { access: access?.value, refresh: refresh?.value };
}

async function init() {
  await refreshStatus();  // show persisted status immediately

  const { access, refresh } = await getOmniHRCookies();
  if (!access || !refresh) {
    if (statusEl.className !== "status ok") {
      statusEl.className = "status warn";
      statusEl.textContent = "Open omnihr.co and sign in first.";
    }
    return;
  }
  const r = await fetch("https://api.omnihr.co/api/v1/auth/details/", { credentials: "include" });
  const me = r.ok ? await r.json() : null;
  if (!me) {
    if (statusEl.className !== "status ok") {
      statusEl.className = "status warn";
      statusEl.textContent = "Session not active — try signing in to omnihr.co again.";
    }
    return;
  }
  if (statusEl.className !== "status ok") {
    statusEl.className = "status ok";
    statusEl.textContent = `Signed in as ${me.full_name} (${me.org?.name ?? "?"}).`;
  }
  pairBtn.disabled = false;
  pairBtn.dataset.employeeId = me.id;
  pairBtn.dataset.org = JSON.stringify(me.org ?? {});
}

// Fetch Google client_id
let googleClientId = null;
try {
  const cfg = await fetch(`${BACKEND}/config/google`).then(r => r.json());
  googleClientId = cfg.client_id || null;
} catch (_) {}
if (!googleClientId) {
  googleConnectBtn.disabled = true;
}

// --- Pair with OmniHR ---
pairBtn.addEventListener("click", async () => {
  const code = codeEl.value.trim();
  if (!/^\d{6}$/.test(code)) {
    statusEl.className = "status err"; statusEl.textContent = "Code must be 6 digits."; return;
  }
  const { access, refresh } = await getOmniHRCookies();
  pairBtn.disabled = true;
  statusEl.className = "status warn"; statusEl.textContent = "Pairing…";
  try {
    const r = await fetch(`${BACKEND}/extension/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pairing_code: code, access_token: access, refresh_token: refresh,
        employee_id: Number(pairBtn.dataset.employeeId),
        org: JSON.parse(pairBtn.dataset.org || "{}"),
      }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    if (data.ext_session) await chrome.storage.local.set({ ext_session: data.ext_session });
    statusEl.className = "status ok"; statusEl.textContent = "✅ Paired. Check your bot.";
    await refreshStatus();
  } catch (e) {
    statusEl.className = "status err"; statusEl.textContent = `Pair failed: ${e.message}`;
    pairBtn.disabled = false;
  }
});

// --- Google connect ---
googleConnectBtn.addEventListener("click", async () => {
  const code = googleCodeEl.value.trim();
  if (!/^\d{6}$/.test(code)) {
    googleStatusEl.className = "status err"; googleStatusEl.textContent = "Code must be 6 digits."; return;
  }
  googleConnectBtn.disabled = true;
  googleStatusEl.className = "status warn"; googleStatusEl.textContent = "Opening Google sign-in…";

  const redirectUri = `https://${chrome.runtime.id}.chromiumapp.org/`;
  const scopes = ["https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly", "email", "profile"].join(" ");
  const authUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  authUrl.searchParams.set("client_id", googleClientId);
  authUrl.searchParams.set("redirect_uri", redirectUri);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("scope", scopes);
  authUrl.searchParams.set("access_type", "offline");
  authUrl.searchParams.set("prompt", "consent");

  try {
    const responseUrl = await chrome.identity.launchWebAuthFlow({ url: authUrl.toString(), interactive: true });
    const authCode = new URL(responseUrl).searchParams.get("code");
    if (!authCode) throw new Error("No auth code");
    googleStatusEl.textContent = "Connecting…";
    const r = await fetch(`${BACKEND}/extension/google-auth`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairing_code: code, auth_code: authCode, redirect_uri: redirectUri }),
    });
    if (!r.ok) throw new Error(await r.text());
    googleCodeEl.value = "";
    await refreshStatus();
  } catch (e) {
    googleStatusEl.className = "status err"; googleStatusEl.textContent = `Failed: ${e.message}`;
    googleConnectBtn.disabled = false;
  }
});

// --- Telegram connect ---
tgInitBtn.addEventListener("click", async () => {
  const code = tgCodeEl.value.trim();
  const phone = tgPhoneEl.value.trim();
  if (!/^\d{6}$/.test(code)) { tgStatusEl.className = "status err"; tgStatusEl.textContent = "Code must be 6 digits."; return; }
  if (!phone) { tgStatusEl.className = "status err"; tgStatusEl.textContent = "Enter your phone number."; return; }
  tgInitBtn.disabled = true;
  tgStatusEl.className = "status warn"; tgStatusEl.textContent = "Sending code to Telegram…";
  try {
    const r = await fetch(`${BACKEND}/extension/telegram-init`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairing_code: code, phone }),
    });
    if (!r.ok) throw new Error(await r.text());
    tgStatusEl.textContent = "Code sent — check Telegram for the OTP.";
    tgStep1El.style.display = "none";
    tgStep2El.style.display = "block";
  } catch (e) {
    tgStatusEl.className = "status err"; tgStatusEl.textContent = `Failed: ${e.message}`;
    tgInitBtn.disabled = false;
  }
});

tgVerifyBtn.addEventListener("click", async () => {
  const code = tgCodeEl.value.trim();
  const otp = tgOtpEl.value.trim();
  tgVerifyBtn.disabled = true;
  tgStatusEl.className = "status warn"; tgStatusEl.textContent = "Verifying…";
  try {
    const r = await fetch(`${BACKEND}/extension/telegram-verify`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairing_code: code, code: otp }),
    });
    if (!r.ok) throw new Error(await r.text());
    await refreshStatus();
  } catch (e) {
    tgStatusEl.className = "status err"; tgStatusEl.textContent = `Failed: ${e.message}`;
    tgVerifyBtn.disabled = false;
  }
});

// --- WhatsApp connect ---
let waPolling = null;

waInitBtn.addEventListener("click", async () => {
  const code = waCodeEl.value.trim();
  if (!/^\d{6}$/.test(code)) { waStatusEl.className = "status err"; waStatusEl.textContent = "Code must be 6 digits."; return; }
  waInitBtn.disabled = true;
  waStatusEl.className = "status warn"; waStatusEl.textContent = "Connecting to WhatsApp…";
  try {
    const r = await fetch(`${BACKEND}/extension/whatsapp-init`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairing_code: code }),
    });
    if (!r.ok) throw new Error(await r.text());
    waStep1El.style.display = "none";
    waQrContainer.style.display = "block";
    waStatusEl.textContent = "Scan the QR code below with WhatsApp.";
    startWaQrPoll(code);
  } catch (e) {
    waStatusEl.className = "status err"; waStatusEl.textContent = `Failed: ${e.message}`;
    waInitBtn.disabled = false;
  }
});

function startWaQrPoll(pairingCode) {
  if (waPolling) clearInterval(waPolling);
  waPolling = setInterval(async () => {
    try {
      const r = await fetch(`${BACKEND}/extension/whatsapp-qr?pairing_code=${pairingCode}`);
      if (!r.ok) return;
      const data = await r.json();
      if (data.connected) {
        clearInterval(waPolling); waPolling = null;
        await refreshStatus(); return;
      }
      if (data.qr) {
        waQrImg.src = data.qr;
        waQrImg.style.display = "block";
        waQrLoading.style.display = "none";
      }
    } catch (_) {}
  }, 3000);
}

init();
