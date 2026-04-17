"""Shared styled HTML pages — dark theme matching the auth flow."""


def styled_page(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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


EXTENSION_PAGE = styled_page("Chrome Extension", """
  <h1>🧾 ExpenseBot</h1>
  <div class="sub">Chrome Extension</div>

  <p>This extension bridges your OmniHR login to the Telegram bot.
  Install once, pair with one tap.</p>

  <a class="btn" href="/extension/download">⬇ Download Extension (.zip)</a>

  <h2>Install in 4 steps</h2>

  <div class="step">
    <div class="num">1</div>
    <div class="step-text">Download and unzip the file above</div>
  </div>
  <div class="step">
    <div class="num">2</div>
    <div class="step-text">Open <code>chrome://extensions</code> → toggle <strong>Developer mode</strong> (top right)</div>
  </div>
  <div class="step">
    <div class="num">3</div>
    <div class="step-text"><strong>Load unpacked</strong> → select the unzipped folder</div>
  </div>
  <div class="step">
    <div class="num">4</div>
    <div class="step-text">Pin the ExpenseBot icon in your toolbar (puzzle icon → pin)</div>
  </div>

  <div class="divider"></div>

  <h2>Then pair</h2>
  <p>In Telegram: <code>/pair</code> → open any omnihr.co tab → click the extension icon → paste the code → done.</p>

  <div class="divider"></div>

  <h2>What it does</h2>
  <p>Reads your OmniHR session cookies after you sign in normally (Google SSO).
  Sends them encrypted to ExpenseBot so it can file claims on your behalf.
  No passwords stored.</p>
""")


LANDING_PAGE = styled_page("Home", """
  <h1>🧾 ExpenseBot</h1>
  <div class="sub">File OmniHR expense claims from Telegram</div>

  <p>Send a receipt photo or PDF → bot parses it with AI → files as a draft on OmniHR.
  Track status, submit for approval, all from your phone.</p>

  <a class="btn" href="https://t.me/yc_sop_wedding_bot" target="_blank">Open in Telegram →</a>

  <h2>Get started</h2>
  <div class="step">
    <div class="num">1</div>
    <div class="step-text"><strong>/login</strong> — connect your Claude subscription (or paste an API key)</div>
  </div>
  <div class="step">
    <div class="num">2</div>
    <div class="step-text"><a href="/extension">Install the Chrome extension</a> → <strong>/pair</strong> with OmniHR</div>
  </div>
  <div class="step">
    <div class="num">3</div>
    <div class="step-text">Send a receipt — bot does the rest</div>
  </div>

  <div class="divider"></div>

  <p style="text-align:center">
    <a href="https://github.com/seahyc/expensebot" style="color:#666">Open source · MIT licensed</a>
  </p>
""")
