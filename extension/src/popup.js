// Popup logic: detect logged-in omnihr.co session, accept pairing code, push to backend.

const BACKEND = "https://expensebot.example";  // configurable via storage in v2

const statusEl = document.getElementById("status");
const codeEl = document.getElementById("code");
const pairBtn = document.getElementById("pair");

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

init();
