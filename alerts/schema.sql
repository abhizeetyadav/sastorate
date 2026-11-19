-- Bhaau price alerts (Cloudflare D1 / SQLite)
-- Apply with:  npx wrangler d1 execute bhaau-alerts --remote --file=alerts/schema.sql

-- Latest lowest in-stock price per phone variant, pushed by the scraper after every run.
CREATE TABLE IF NOT EXISTS variants (
  key        TEXT PRIMARY KEY,          -- "iphone-17-pro|256"
  label      TEXT NOT NULL,             -- "iPhone 17 Pro 256 GB"
  slug       TEXT NOT NULL,             -- "iphone-17-pro" (page address)
  storage_gb INTEGER,
  price      INTEGER,                   -- lowest in-stock price (NPR); NULL when nobody has stock
  store      TEXT,
  url        TEXT,
  updated_at TEXT NOT NULL
);

-- One row per (email, variant). Created as 'pending' until the email link is clicked.
CREATE TABLE IF NOT EXISTS alerts (
  id            INTEGER PRIMARY KEY,
  email         TEXT NOT NULL,
  variant       TEXT NOT NULL,
  target        INTEGER,                -- optional: only alert at or below this price
  baseline      INTEGER,                -- price we last told them about (or price when they signed up)
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending | active
  token         TEXT NOT NULL UNIQUE,   -- secret for confirm / unsubscribe links
  ip_hash       TEXT,
  created_at    TEXT NOT NULL,
  confirmed_at  TEXT,
  last_sent_at  TEXT,
  confirm_sent_at TEXT,
  UNIQUE (email, variant)
);
CREATE INDEX IF NOT EXISTS ix_alerts_variant ON alerts (variant, status);
CREATE INDEX IF NOT EXISTS ix_alerts_ip ON alerts (ip_hash, created_at);
