// Background service worker: keeps tokens fresh, alerts on expiry.
//
// v2 features (TODO):
//   - Listen for omnihr.co cookie changes; auto-resync to backend on refresh.
//   - Periodic alarm (every 6h) to push current cookies if access token rotated.

chrome.runtime.onInstalled.addListener(() => {
  console.log("[expensebot] extension installed");
});

chrome.cookies.onChanged.addListener(async ({ cookie, removed, cause }) => {
  if (!cookie.domain.endsWith("omnihr.co")) return;
  if (cookie.name !== "access_token" && cookie.name !== "refresh_token") return;
  // TODO v2: throttle and POST to backend.extension/refresh
  console.log("[expensebot] omnihr cookie change", cookie.name, removed ? "removed" : "set", cause);
});
