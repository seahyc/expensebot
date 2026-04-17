"""Shared styled HTML pages — dark theme matching the auth flow."""

from datetime import date

# Populated by server.run() after tg_app.initialize() resolves the bot username
# via getMe. Pages fall back to a generic label if the bot isn't up yet.
BOT_USERNAME: str | None = None


def _bot_link_html() -> tuple[str, str]:
    """Return (handle_text, deep_link_url) for the configured Telegram bot."""
    if BOT_USERNAME:
        return f"@{BOT_USERNAME}", f"https://t.me/{BOT_USERNAME}"
    # fallback while tg_app is still initializing or in a Lark-only deploy
    return "the ExpenseBot Telegram bot", "https://t.me/"


def styled_page(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>ExpenseBot — {title}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#1a1a2e;
       color:#eee;min-height:100vh;display:flex;align-items:center;justify-content:center;
       padding:20px}}
  .card{{background:#16213e;border-radius:16px;padding:28px;max-width:480px;width:100%}}
  h1{{font-size:22px;text-align:center;margin-bottom:4px}}
  .sub{{font-size:13px;color:#888;text-align:center;margin-bottom:20px}}
  h2{{font-size:16px;color:#ccc;margin:20px 0 8px}}
  p{{font-size:14px;color:#aaa;margin:10px 0;line-height:1.6}}
  ol,ul{{padding-left:20px;margin:10px 0}}
  li{{font-size:14px;color:#bbb;margin:6px 0;line-height:1.5}}
  a{{color:#8b6cff;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  a.btn{{display:block;background:#6633ee;color:#fff;padding:14px;border-radius:8px;
        text-decoration:none;font-size:16px;font-weight:600;margin:16px 0;text-align:center}}
  a.btn:hover{{background:#7744ff;text-decoration:none}}
  a.btn2{{background:#2d4a7a}}
  a.btn2:hover{{background:#3d5a9a}}
  code{{background:#0f3460;padding:2px 6px;border-radius:4px;font-size:13px;color:#ddd}}
  .step{{display:flex;align-items:flex-start;gap:12px;margin:12px 0}}
  .num{{background:#6633ee;color:#fff;width:28px;height:28px;border-radius:50%;
       display:flex;align-items:center;justify-content:center;font-size:14px;
       font-weight:700;flex-shrink:0}}
  .step-text{{font-size:14px;color:#bbb;line-height:1.5}}
  .divider{{border-top:1px solid #2a2a4e;margin:20px 0}}
  .footer{{text-align:center;margin-top:20px;font-size:12px;color:#555}}
  .footer a{{color:#666}}
</style></head><body>
<div class="card">
  {body_html}
  <div class="footer">
    <a href="/">ExpenseBot</a> · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> ·
    <a href="https://github.com/seahyc/expensebot">GitHub</a>
  </div>
</div>
</body></html>"""


def extension_page() -> str:
    handle, link = _bot_link_html()
    return styled_page("Chrome Extension", f"""
  <h1>💰 ExpenseBot</h1>
  <div class="sub">Chrome Extension</div>

  <p>This extension connects your OmniHR login to
  <a href="{link}" target="_blank"><strong>{handle}</strong></a> on Telegram.
  Install once, pair with one tap.</p>

  <a class="btn" href="/extension/download">⬇ Download Extension</a>

  <h2>Install in 4 steps</h2>

  <div class="step">
    <div class="num">1</div>
    <div class="step-text">
      <strong>Unzip the file you just downloaded.</strong><br>
      <span style="font-size:13px;color:#999">Double-click
      <code>ExpenseBot-Chrome-Extension.zip</code> in your Downloads folder.
      You'll end up with a folder called
      <code>ExpenseBot-Chrome-Extension</code> — note where it is.</span>
    </div>
  </div>

  <div class="step">
    <div class="num">2</div>
    <div class="step-text">
      <strong>Open Chrome's extensions page.</strong><br>
      <span style="font-size:13px;color:#999">Paste <code>chrome://extensions</code>
      into Chrome's address bar, hit Enter. Flip the <strong>Developer mode</strong>
      switch in the top-right to ON.</span>
    </div>
  </div>

  <div class="step">
    <div class="num">3</div>
    <div class="step-text">
      <strong>Click <em>Load unpacked</em> (top-left of the page).</strong><br>
      <span style="font-size:13px;color:#999">A file picker opens. Find the
      <code>ExpenseBot-Chrome-Extension</code> folder from step 1, click it
      <strong>once</strong> so it's highlighted, then click
      <strong>Select</strong> (or Open).
      <br><br>
      ⚠️ <strong>Pick the folder itself, not something inside it.</strong>
      Don't go into <code>icons</code> or <code>src</code> — just click the
      outer <code>ExpenseBot-Chrome-Extension</code> folder and select it.</span>
    </div>
  </div>

  <div class="step">
    <div class="num">4</div>
    <div class="step-text">
      <strong>Pin the 💰 icon to your Chrome toolbar.</strong><br>
      <span style="font-size:13px;color:#999">Click the puzzle-piece icon near
      your Chrome toolbar, find <em>ExpenseBot</em>, click the pin next to it.
      The 💰 icon should appear in your toolbar.</span>
    </div>
  </div>

  <div class="divider"></div>

  <h2>Then pair your account</h2>
  <p style="color:#ccc">Back on <a href="{link}" target="_blank"><strong>{handle}</strong></a>
  in Telegram:</p>
  <ol style="padding-left:22px;color:#bbb">
    <li style="margin:8px 0">Make sure you're signed in to
      <a href="https://app.omnihr.co" target="_blank" style="color:#8b6cff">omnihr.co</a>
      in Chrome. (If that link shows your dashboard, you're good. If it shows
      a login page, sign in first — Google SSO is fine.)</li>
    <li style="margin:8px 0">Send <code>/pair</code> to the bot. You'll get a 6-digit code.</li>
    <li style="margin:8px 0">On your omnihr.co tab, click the 💰 icon in the
      toolbar, paste the 6 digits, tap <strong>Pair</strong>.</li>
  </ol>

  <div class="divider"></div>

  <h2>Trouble?</h2>
  <p style="font-size:13px;color:#999"><strong>Chrome says "Manifest file is missing or unreadable":</strong>
  you selected the wrong folder. Back up one level and pick
  <code>ExpenseBot-Chrome-Extension</code> directly — not
  <code>icons</code> or <code>src</code>.</p>

  <p style="font-size:13px;color:#999"><strong>Can't find the 💰 icon:</strong>
  click the puzzle-piece icon (top-right of Chrome, left of your profile picture),
  find <em>ExpenseBot</em>, and click the pin. If <em>ExpenseBot</em> isn't listed,
  install didn't succeed — go back to <code>chrome://extensions</code> and confirm
  it's there and enabled.</p>

  <div class="divider"></div>

  <h2>What it does</h2>
  <p>Reads your OmniHR session cookies after you sign in normally (Google SSO).
  Sends them encrypted to ExpenseBot so it can file claims on your behalf.
  No passwords stored.</p>
""")


def terms_page() -> str:
    return styled_page("Terms", f"""
  <h1>💰 ExpenseBot</h1>
  <div class="sub">Terms of Service · {date.today().isoformat()}</div>

  <p><strong>What this is.</strong> ExpenseBot is an open-source tool
  (<a href="https://github.com/seahyc/expensebot">github.com/seahyc/expensebot</a>)
  that files expense claims into OmniHR on your behalf, via a Telegram or Lark bot
  and a Chrome extension. Provided as-is with no warranty.</p>

  <h2>Your account with your employer</h2>
  <p>You're responsible for anything the bot files using your OmniHR session.
  If your company's policy prohibits third-party automation, don't use this.
  The bot acts with your credentials.</p>

  <h2>Fair use</h2>
  <p>200 receipts/month on the Managed tier. Higher volume → self-host
  (it's a <code>git clone</code> + <code>docker compose up</code>).</p>

  <h2>No guarantees</h2>
  <p>If the bot misclassifies, files wrong amounts, or misses a claim, it's
  your job to review and correct. Always check the OmniHR dashboard.</p>

  <h2>We can stop serving you</h2>
  <p>if you abuse the bot (spam, attempt to bypass tenant isolation, etc.).
  You can stop using it any time by just walking away — ping the maintainer
  via GitHub if you want your data purged.</p>
""")


def privacy_page() -> str:
    return styled_page("Privacy", f"""
  <h1>💰 ExpenseBot</h1>
  <div class="sub">Privacy Policy · {date.today().isoformat()}</div>

  <h2>What we collect</h2>
  <ul>
    <li>Your Telegram/Lark user ID (to identify you across sessions)</li>
    <li>Your Anthropic API key, if you set one (encrypted at rest)</li>
    <li>Your OmniHR access + refresh JWTs (encrypted at rest)</li>
    <li>Parsed receipt metadata (merchant, date, amount, currency, policy) —
    kept to enable duplicate detection and status tracking</li>
    <li>Receipt files <strong>temporarily</strong> (deleted within 24h after upload to OmniHR)</li>
    <li>Your corrections (to improve classification)</li>
  </ul>

  <h2>What we don't do</h2>
  <ul>
    <li>Sell or share your data with anyone.</li>
    <li>Use your receipts to train any model.</li>
    <li>Log amounts, merchants, emails, or credentials in plaintext. All logs
    go through a redactor.</li>
  </ul>

  <h2>Where data lives</h2>
  <ul>
    <li>Postgres/SQLite on a single VM you can inspect (oracle.seahyingcong.com).</li>
    <li>No third-party analytics, no trackers.</li>
  </ul>

  <h2>Third parties that see your data</h2>
  <ul>
    <li><strong>OmniHR</strong> — obviously (it's your HR system)</li>
    <li><strong>Anthropic</strong> — parses your receipts (they don't train on
    API traffic per their policy). On Managed tier, via the maintainer's
    Anthropic account; on BYOK tier, via your own.</li>
    <li><strong>Telegram / Lark</strong> — the channel carrier</li>
    <li><strong>Chrome Web Store</strong> — if you install the extension</li>
  </ul>

  <h2>Your controls</h2>
  <ul>
    <li><code>/memories</code> — read everything the bot has remembered about
    your preferences; edit or remove entries by talking to it.</li>
    <li>Want your data deleted or exported? Open a GitHub issue (link below)
    and I'll run it by hand — low enough volume that automating it isn't
    worth the footgun risk.</li>
  </ul>

  <h2>Contact</h2>
  <p>Open a GitHub issue:
  <a href="https://github.com/seahyc/expensebot/issues">github.com/seahyc/expensebot/issues</a></p>
""")


def landing_page() -> str:
    handle, link = _bot_link_html()
    return styled_page("Home", f"""
  <h1>💰 ExpenseBot</h1>
  <div class="sub">File OmniHR expense claims from Telegram</div>

  <p>Send a receipt photo or PDF → bot parses it with AI → files as a draft on OmniHR.
  Track status, submit for approval, answer questions about your expenses — all from your phone.</p>

  <a class="btn" href="{link}" target="_blank">💬 Chat with {handle} on Telegram →</a>

  <h2>Setup (one-time, ~2 min)</h2>

  <div class="step">
    <div class="num">1</div>
    <div class="step-text">Open <a href="{link}" target="_blank"><strong>{handle}</strong></a>
    on Telegram and send <strong>/login</strong> — connect your Claude subscription
    (or paste an API key). This powers the AI that reads your receipts. Uses your
    existing Claude Pro/Max plan.</div>
  </div>
  <div class="step">
    <div class="num">2</div>
    <div class="step-text"><strong><a href="/extension">Install the Chrome extension</a></strong> —
    download, unzip, load in Chrome (Developer mode → Load unpacked).</div>
  </div>
  <div class="step">
    <div class="num">3</div>
    <div class="step-text">Back in <a href="{link}" target="_blank"><strong>{handle}</strong></a>,
    send <strong>/pair</strong> — sign into omnihr.co in Chrome, click the extension icon,
    paste the pairing code. This links your OmniHR account.</div>
  </div>

  <div class="divider"></div>

  <h2>Using the bot</h2>
  <ul>
    <li><strong>File a claim</strong> — send a receipt photo or PDF (with optional caption like "lunch with client")</li>
    <li><strong>Ask questions</strong> — "how much did I spend in April?" · "what's still pending?"</li>
    <li><strong>Take action</strong> — "submit claim 126758" · "delete the grab one"</li>
    <li><strong>Quick list</strong> — /list · /list approved · /list apr</li>
  </ul>

  <div class="divider"></div>

  <h2>How it works</h2>
  <p>The bot reads your receipt using Claude AI, matches it to your company's expense policies,
  and files it as a draft on OmniHR. You review on the dashboard, then submit for approval
  from Telegram or the web.</p>
  <p>Your data: receipts are deleted from our server within 24h (they're on OmniHR).
  API keys and tokens are encrypted at rest. <a href="/privacy">Full privacy policy</a>.</p>

  <div class="divider"></div>

  <p style="text-align:center">
    <a href="https://github.com/seahyc/expensebot" style="color:#666">Open source · MIT licensed</a>
  </p>
""")
