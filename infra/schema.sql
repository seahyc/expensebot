-- expensebot Postgres schema. v1.

CREATE TABLE tenants (
  id              TEXT PRIMARY KEY,           -- e.g. "glints" (subdomain)
  org_id          INT,                         -- omnihr org id
  domain          TEXT NOT NULL,               -- glints.omnihr.co
  payroll_currency TEXT,
  shepherd_user_id INT,                        -- references users.id
  tenant_md       TEXT,                        -- the curated config doc
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id              SERIAL PRIMARY KEY,
  channel         TEXT NOT NULL,               -- "telegram" | "lark"
  channel_user_id TEXT NOT NULL,               -- platform's user id
  tenant_id       TEXT REFERENCES tenants(id),
  omnihr_employee_id INT,
  omnihr_full_name TEXT,
  omnihr_email    TEXT,
  -- encrypted secrets (libsodium secretbox); decrypt only at use time
  anth_key_enc    BYTEA,                       -- BYOK: user's anthropic key
  refresh_jwt_enc BYTEA,                       -- omnihr refresh token
  access_jwt_enc  BYTEA,                       -- omnihr access token
  access_expires_at  TIMESTAMPTZ,
  refresh_expires_at TIMESTAMPTZ,
  tier            TEXT NOT NULL DEFAULT 'byok',-- byok | managed
  user_md         TEXT,                        -- per-user rules + glossary
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel, channel_user_id)
);

CREATE INDEX users_tenant_idx ON users(tenant_id);

CREATE TABLE schema_cache (
  tenant_id       TEXT NOT NULL,
  policy_id       INT NOT NULL,
  date_bucket     TEXT NOT NULL,               -- YYYY-MM
  schema_json     JSONB NOT NULL,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, policy_id, date_bucket)
);

CREATE TABLE trips (
  id              SERIAL PRIMARY KEY,
  user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,               -- "Jakarta investor trip"
  destination     TEXT NOT NULL,
  start_date      DATE NOT NULL,
  end_date        DATE NOT NULL,
  active          BOOL NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX trips_user_active_idx ON trips(user_id) WHERE active;

CREATE TABLE receipts (
  id              SERIAL PRIMARY KEY,
  user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trip_id         INT REFERENCES trips(id),
  file_sha256     TEXT NOT NULL,
  s3_key          TEXT,                         -- nullable after cleanup
  parsed_json     JSONB NOT NULL,               -- ParsedReceipt
  parsed_merchant TEXT,
  parsed_date     DATE,
  parsed_amount   NUMERIC(14,2),
  parsed_currency TEXT,
  omnihr_doc_id        INT,
  omnihr_submission_id INT,
  status          INT,                          -- omnihr submission status
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX receipts_user_sha_idx ON receipts(user_id, file_sha256);
CREATE INDEX receipts_user_dupe_idx ON receipts(user_id, parsed_merchant, parsed_date, parsed_amount);
CREATE INDEX receipts_user_submission_idx ON receipts(user_id, omnihr_submission_id);

CREATE TABLE status_events (
  id              SERIAL PRIMARY KEY,
  receipt_id      INT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  from_status     INT,
  to_status       INT NOT NULL,
  actor           TEXT,                         -- approver name if available
  comment         TEXT,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE corrections (
  id              SERIAL PRIMARY KEY,
  user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  receipt_id      INT REFERENCES receipts(id),
  field           TEXT NOT NULL,
  suggested       TEXT,
  user_value      TEXT,
  context_json    JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX corrections_user_field_idx ON corrections(user_id, field);

CREATE TABLE pairing_codes (
  code            TEXT PRIMARY KEY,             -- 6-digit
  user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE email_inboxes (
  alias           TEXT PRIMARY KEY,             -- e.g. "yc-k7m4"
  user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  allowed_senders TEXT[],
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
