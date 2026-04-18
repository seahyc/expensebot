# Local end-to-end test

You'll run the bot locally, install the extension unpacked, pair via the bot.

## 0. Telegram bot token

Open https://t.me/BotFather → `/newbot` → name it (e.g. "Janai Dev") →
get the token.

## 1. Backend running locally

```bash
cd ~/Code/expensebot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env, set:
#   TELEGRAM_BOT_TOKEN=8000:abc...
#   PUBLIC_BASE_URL=http://localhost:8000
# (Anthropic key not needed in env — set per-user via /setkey)

python -m bot.server
```

Should print `Uvicorn running on http://0.0.0.0:8000` and start polling Telegram.

Health check: `curl http://localhost:8000/healthz` → `{"status":"ok"}`

## 2. Install the extension (unpacked)

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** (top-right).
3. **Load unpacked** → select `~/Code/expensebot/extension/`.
4. Pin the Janai icon to your toolbar (puzzle icon → pin).

## 3. Open omnihr.co and sign in

In any tab, go to https://glints.omnihr.co/ and sign in via Google as usual.

## 4. Pair

In Telegram, find your bot, send:

```
/start
/setkey sk-ant-…           # your Anthropic key
/pair
```

Bot replies with a 6-digit code (e.g. `384192`).

Click the Janai extension icon → popup shows "Signed in as Ying Cong (Glints)".
Paste the code → click **Pair**.

Bot DMs back: `✅ Paired as Ying Cong (Glints, employee #59430)`.

## 5. File a receipt

In Telegram, send the bot a receipt photo or PDF (with optional caption like
"gojek to airport").

Bot replies:
```
📄 Gojek SGD 25.50 on 2026-04-07
Suggested: Transportation (Airport) (policy 3712)
⏳ Filing draft…
✅ Drafted #126XXX
Gojek SGD 25.50 · 2026-04-07
Transportation (Airport)

/submit 126XXX  ·  /delete 126XXX  ·  /list
```

Verify on https://glints.omnihr.co/expenses/submission/.

## 6. List / delete / submit

```
/list                # last 10
/delete 126XXX       # removes the draft
/submit 126XXX       # uses tentative submit action code — verify on dashboard
```

## Troubleshooting

- **Extension popup says "Open omnihr.co and sign in first"** — you're not
  logged in to omnihr.co in this Chrome profile. Open omnihr.co tab, sign in,
  reopen popup.
- **Pair button does nothing** — backend isn't reachable. Check
  `curl http://localhost:8000/healthz`. Open extension service worker logs at
  chrome://extensions → Janai → "Service worker" → console.
- **Bot says "Not paired"** after pairing — JWTs landed in the DB but lookup
  failed. Check `sqlite3 expensebot.db "SELECT id, omnihr_full_name, omnihr_employee_id, length(access_jwt) FROM users;"`
- **OmniHR 401** — JWT expired. Tokens are valid ~15 min for access, ~30 days
  for refresh. The client auto-refreshes; if both expired, re-pair.
- **Schema drift error** — HR added a mandatory field. Open the modal once
  via web UI to see what's needed; update `tenants/glints.md` curated rules
  section to teach the parser.
- **`/submit` fails with 400** — quick-action submit code is currently a
  guess (`QUICK_ACTION_SUBMIT = 2` in `omnihr_client/client.py`). Submit one
  draft via web UI with network capture on, find the action code, update.

## Deploy to Oracle (next)

Once happy locally, see `DEPLOY.md` (TODO).

Plan: Docker on the Oracle VM, systemd service, ngrok or Cloudflare Tunnel for
the public URL the extension talks to. Or skip the extension entirely on the
deployed bot and have it act as a "headless" companion using cookies you push
manually for prototyping.
