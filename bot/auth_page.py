"""HTML page served at /auth/start?session=XYZ that walks the user through OAuth.

Flow:
  1. Bot sends user: https://expensebot.seahyingcong.com/auth/start?session=abc
  2. Page shows "Authorize" button → opens Claude OAuth in same tab
  3. User authorizes → redirected to platform.claude.com/oauth/code/callback?code=XXX
  4. That page shows the code. User copies the FULL URL from address bar.
  5. User taps "Back" → returns to our page (still open if they used back button)
     OR the page instructions say "paste the URL below"
  6. Our page POSTs the code to /auth/complete → backend feeds to claude auth login
  7. Page shows "✅ Done! Go back to Telegram."
"""

from .voice import default_voice

_BRAND_NAME = default_voice().text("brand_name")

AUTH_START_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>""" + _BRAND_NAME + """ — Sign in with Claude</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         background: #1a1a2e; color: #eee; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; }
  .card { background: #16213e; border-radius: 16px; padding: 32px;
          max-width: 420px; width: 90%; text-align: center; }
  h1 { font-size: 20px; margin-bottom: 8px; }
  p { font-size: 14px; color: #aaa; margin-bottom: 20px; line-height: 1.5; }
  .step { text-align: left; margin: 16px 0; }
  .step li { margin: 8px 0; font-size: 14px; }
  a.btn { display: block; background: #6633ee; color: white; padding: 14px;
          border-radius: 8px; text-decoration: none; font-size: 16px;
          font-weight: 600; margin: 16px 0; }
  a.btn:hover { background: #7744ff; }
  input { width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #333;
          background: #0f3460; color: #eee; font-size: 14px; margin: 8px 0; }
  button { width: 100%; padding: 12px; border-radius: 8px; border: 0;
           background: #6633ee; color: white; font-size: 16px; font-weight: 600;
           cursor: pointer; margin: 8px 0; }
  button:disabled { background: #444; cursor: default; }
  .success { background: #1a4d2e; padding: 20px; border-radius: 12px;
             margin-top: 16px; }
  .error { background: #4d1a1a; padding: 12px; border-radius: 8px;
           margin-top: 12px; font-size: 13px; }
  .small { font-size: 12px; color: #666; margin-top: 12px; }
</style>
</head>
<body>
<div class="card" id="main">
  <h1>💰 """ + _BRAND_NAME + """</h1>
  <p>Sign in with your Claude subscription to parse receipts.</p>

  <div id="step1">
    <a class="btn" href="OAUTH_URL_PLACEHOLDER" id="authLink">
      Authorize with Claude →
    </a>
    <p>After authorizing, you'll see a page with a URL containing <code>?code=...</code></p>
  </div>

  <div id="step2" style="display:none">
    <p style="color:#eee">Paste the callback URL here:</p>
    <input id="codeInput" placeholder="https://platform.claude.com/oauth/code/callback?code=..." autocomplete="off">
    <button id="submitBtn" onclick="submitCode()">Complete Login</button>
    <div id="status"></div>
  </div>

  <div id="step3" style="display:none">
    <div class="success">
      <h2>✅ Logged in!</h2>
      <p style="color:#aaa; margin-top:8px">Go back to Telegram — your bot is ready.</p>
    </div>
  </div>

  <p class="small">Session: SESSION_PLACEHOLDER</p>
</div>

<script>
const session = "SESSION_PLACEHOLDER";
const baseUrl = window.location.origin;

// Show step 2 after user clicks authorize (they'll come back via back button)
document.getElementById('authLink').addEventListener('click', function() {
  // Show step 2 after a short delay (user navigates away then comes back)
  setTimeout(() => {
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
  }, 1000);
});

// Also show step 2 on page focus (user returns from OAuth page)
window.addEventListener('focus', function() {
  if (document.getElementById('step1').style.display !== 'none') {
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
  }
});

async function submitCode() {
  const input = document.getElementById('codeInput').value.trim();
  const status = document.getElementById('status');
  const btn = document.getElementById('submitBtn');

  // Extract code from URL or raw text
  let code = null;
  const match = input.match(/[?&]code=([A-Za-z0-9_\\-]+)/);
  if (match) code = match[1];
  else if (input.length > 20 && /^[A-Za-z0-9_\\-]+$/.test(input)) code = input;

  if (!code) {
    status.innerHTML = '<div class="error">Could not find the code. Paste the full callback URL.</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Completing login…';

  try {
    const resp = await fetch(baseUrl + '/auth/complete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: session, code: code}),
    });
    const data = await resp.json();
    if (resp.ok && data.ok) {
      document.getElementById('step2').style.display = 'none';
      document.getElementById('step3').style.display = 'block';
    } else {
      status.innerHTML = '<div class="error">' + (data.detail || 'Login failed. Try /login again.') + '</div>';
      btn.disabled = false;
      btn.textContent = 'Complete Login';
    }
  } catch (e) {
    status.innerHTML = '<div class="error">Network error: ' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = 'Complete Login';
  }
}
</script>
</body>
</html>"""
