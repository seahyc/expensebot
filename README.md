# expensebot

Telegram + Lark bot for filing OmniHR expense claims by sending receipts.
Multi-tenant, schema-driven, agentic. MIT licensed.

## Status

Early scaffold. Validated end-to-end on glints.omnihr.co (filed real claims via the API).
The architecture below is the design we're building toward, not what's shipped today.

## What it does

Send a receipt photo or PDF to the bot. It:

1. Parses the receipt (Claude Sonnet 4.5, structured output)
2. Classifies into the right OmniHR policy + sub-category for your tenant
3. Files as a draft (or submits, if you say so) via the OmniHR API
4. Tracks status, DMs you when approved/rejected/paid
5. Catches duplicates against both local cache and OmniHR
6. Learns your corrections — improves classification over time per user and per org

Also:
- Per-user email inbox: forward receipts as emails, they get filed
- Trip mode: tag a window of dates as a trip, all receipts auto-fill destination/dates
- Reconciliation: cross-check approved claims against your payslip
- Year-end export: CSV + PDF for tax season

## How auth works (the hard bit)

OmniHR uses cookie-based JWTs. For Google-SSO orgs (Glints, etc.) we can't do an OAuth
flow on a third-party domain because OmniHR's Google clientId is restricted to their
whitelisted origins. Solution: a companion **Chrome extension** that reads the user's
HttpOnly cookies after they log in to omnihr.co normally, and pushes the JWT to the bot
backend with a one-time pairing code.

User flow:
1. `/start` in bot → instructions
2. `/setkey sk-ant-…` (or `/upgrade` for managed tier) → Anthropic API key for parsing
3. `/pair` → bot returns 6-digit code
4. User installs extension, opens omnihr.co (signs in via Google SSO normally),
   clicks extension icon, pastes code → backend stores refresh JWT
5. Bot DMs "Paired as Ying Cong (Glints)"

Refresh tokens are server-side. When they expire, extension auto-resyncs from the
user's active omnihr.co session in background.

## Pricing tiers

| Tier | Cost | API key |
|---|---|---|
| **Free / BYOK** | $0/mo + Anthropic API costs (~$0.02/receipt) | User's Anthropic key |
| **Managed** | $5/mo (fair-use 200 receipts/mo, $0.10 overage) | Maintainer's Anthropic key |
| **Future: Claude OAuth** | TBD when Anthropic ships consumer OAuth | OAuth |

## Architecture

```
Telegram / Lark user
      ↓
Bot backend (FastAPI)
      ↓  ↓  ↓
   Postgres  Redis  S3 (24h receipt cache)
      ↓
Claude API (Sonnet 4.5)  +  OmniHR API
```

Extension lives in user's Chrome, talks to backend over HTTPS.

Background workers:
- **status_poller**: every 15 min per active user, diff OmniHR submissions, DM on changes
- **refresh_sweeper**: every 6h, refresh JWTs proactively
- **receipt_cleanup**: every hour, delete receipt files > 24h old
- **schema_refresher**: nightly, re-fetch tenant schemas, alert shepherds on drift

## Per-tenant configuration

Every OmniHR tenant has different policies, custom fields, sub-categories.
The bot maintains a `tenants/<org>.md` per tenant. Two sections:

1. **Auto-seeded** (DO NOT EDIT) — schema fetched from OmniHR API
2. **User-curated** — natural-language rules and glossary, edited via `/orgconfig`

Both injected into the Claude prompt at parse time. See `tenants/glints.md` for
a real example, `tenants/_template.md` for a fresh tenant.

## Per-user learning

Each user's corrections accumulate. After N occurrences of the same correction
pattern, bot proposes a rule for the user (or for the whole org if multiple users
hit the same correction).

Stored in `users.user_md` column, edited via `/myrules`.

## Token efficiency

Most operations don't touch Claude:

| Action | LLM call? |
|---|---|
| `/list`, `/status`, `/pair`, `/setkey` | No |
| Status poller DMs | No |
| Dupe check on file SHA | No |
| Edit field on existing draft | No |
| Receipt parse + classify | 1 Sonnet call (~$0.005 with prompt cache) |
| Routing ambiguous chat message | 1 Haiku call (~$0.0001) |

Prompt-caching: tenant.md + user.md + last 10 claims context cached for 5 min,
90% discount on repeated tokens.

Result: 100 receipts/mo ≈ $0.50 actual LLM cost.

## Repo structure

```
expensebot/
├── README.md                    you are here
├── omnihr_client/               schema-driven OmniHR API client
│   ├── client.py                draft, submit, list, refresh
│   ├── schema.py                discovery + cache + invalidation
│   └── auth.py                  JWT lifecycle
├── bot/
│   ├── common/                  shared handlers (parse, file, status)
│   ├── telegram/                Telegram-specific (webhooks, formatting)
│   └── lark/                    Lark-specific
├── extension/                   Chrome MV3 (cookie bridge)
├── tenants/
│   ├── _template.md             fresh tenant skeleton
│   └── glints.md                pre-seeded from real probing
├── infra/
│   ├── docker-compose.yml       postgres + redis local
│   └── fly.toml                 deploy
├── ops/
│   ├── status_poller.py
│   ├── refresh_sweeper.py
│   ├── receipt_cleanup.py
│   └── schema_refresher.py
└── .env.example
```

## Build phases

- **v1 (week 1-2)**: Telegram + extension + draft mode + dupe check + status poller. BYOK only. Glints-only.
- **v2 (week 3-4)**: Email inbox, trip mode, reconciliation, Stripe managed tier, multi-tenant `tenant.md` + `/orgconfig`.
- **v3 (week 5+)**: Lark adapter, recurring-expense detection, year-end export.

## Contributing

Open to PRs. Patterns to follow:

- New OmniHR endpoint? Add to `omnihr_client/`, never hardcode field IDs.
- New tenant? Pair with bot, then edit `tenants/<org>.md` with natural-language rules.
- New channel (Slack, WhatsApp, Discord)? Add `bot/<channel>/` mirroring `bot/telegram/` interface.

## Self-hosting

The hosted instance at `expensebot.seahyingcong.com` is convenient but you're
trusting someone else with your OmniHR tokens and Anthropic key (encrypted at
rest, but still — trust is trust). Self-hosting takes ~10 minutes:

```bash
# 1. On any VM with Docker (1 vCPU / 512MB RAM is plenty)
git clone https://github.com/seahyc/expensebot
cd expensebot
cp .env.example .env
# Set:
#   TELEGRAM_BOT_TOKEN (from @BotFather)
#   ENCRYPTION_KEY  (python -c "import secrets; print(secrets.token_urlsafe(32))")
#   PUBLIC_BASE_URL (e.g. https://expensebot.you.com)

# 2. Point a DNS A record at your VM IP, front with Caddy or any reverse proxy
docker compose up -d

# 3. In Chrome, load extension/ unpacked (or distribute via the Chrome Web Store
#    to your team). Open extension popup → DevTools → set backend URL:
#    chrome.storage.local.set({backend: "https://expensebot.you.com"})
```

Your users DM your bot; everything stays on your VM.

## License

MIT.
