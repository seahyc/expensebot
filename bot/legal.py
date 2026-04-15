"""Terms + Privacy pages served at /terms and /privacy.

Deliberately short and plain. The gist: you store user tokens + API keys
encrypted, you don't sell data, you delete on request.
"""

from __future__ import annotations

from datetime import date

TERMS_MD = f"""# ExpenseBot — Terms of Service

_Last updated: {date.today().isoformat()}_

**What this is**: ExpenseBot is an open-source tool ([github.com/seahyc/expensebot](https://github.com/seahyc/expensebot))
that files expense claims into OmniHR on your behalf, via a Telegram or Lark bot
and a Chrome extension. Provided as-is with no warranty.

**Your account with your employer**: you're responsible for anything the bot
files using your OmniHR session. If your company's policy prohibits third-party
automation, don't use this. The bot acts with your credentials.

**Fair use**: 200 receipts/month on the Managed tier. Higher volume →
self-host (it's a `git clone` + `docker compose up`).

**No guarantees**: if the bot misclassifies, files wrong amounts, or misses a
claim, it's your job to review and correct. Always check the OmniHR dashboard.

**We can stop serving you** if you abuse the bot (spam, attempt to bypass tenant
isolation, etc.). You can stop using it at any time with `/delete-account`.
"""


PRIVACY_MD = f"""# ExpenseBot — Privacy Policy

_Last updated: {date.today().isoformat()}_

## What we collect
- Your Telegram/Lark user ID (to identify you across sessions)
- Your Anthropic API key, if you set one (encrypted at rest)
- Your OmniHR access + refresh JWTs (encrypted at rest)
- Parsed receipt metadata (merchant, date, amount, currency, policy) — kept to
  enable duplicate detection and status tracking
- Receipt files **temporarily** (deleted within 24h after upload to OmniHR)
- Your corrections (to improve classification)

## What we don't do
- Sell or share your data with anyone.
- Use your receipts to train any model.
- Log amounts, merchants, emails, or credentials in plaintext. All logs go
  through a redactor.

## Where data lives
- Postgres/SQLite on a single VM you can inspect (oracle.seahyingcong.com).
- No third-party analytics, no trackers.

## Third parties that see your data
- **OmniHR** — obviously (it's your HR system)
- **Anthropic** — parses your receipts (they don't train on API traffic per
  their policy). On Managed tier, via the maintainer's Anthropic account;
  on BYOK tier, via your own
- **Telegram / Lark** — the channel carrier
- **Chrome Web Store** — if you install the extension

## Your controls
- `/export-me` — JSON dump of everything we have on you
- `/delete-account` — purges your row, tokens, keys, and receipt records

## Contact
Open a GitHub issue: [github.com/seahyc/expensebot/issues](https://github.com/seahyc/expensebot/issues)
"""


def html_page(title: str, body_md: str) -> str:
    # tiny self-contained HTML, no JS
    import html
    import re
    body = html.escape(body_md)
    # minimal markdown: headings + paragraphs + **bold** + `code`
    body = re.sub(r"^# (.+)$", r"<h1>\1</h1>", body, flags=re.M)
    body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", body, flags=re.M)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"`([^`]+)`", r"<code>\1</code>", body)
    body = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', body)
    body = re.sub(r"\n\n+", "</p><p>", body)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 640px;
       margin: 2em auto; padding: 0 1em; line-height: 1.5; color: #222; }}
h1, h2 {{ color: #111; }}
code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }}
a {{ color: #1a73e8; }}
</style></head>
<body><p>{body}</p></body></html>"""
