CREATE TABLE IF NOT EXISTS usage_events (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      TEXT        NOT NULL,
  request_id     TEXT        NOT NULL UNIQUE,
  timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  baseline_tokens   INT     NOT NULL DEFAULT 0,
  optimised_tokens  INT     NOT NULL DEFAULT 0,
  tokens_saved      INT     NOT NULL DEFAULT 0,
  cost_saved_usd    NUMERIC(12,8) NOT NULL DEFAULT 0,
  groups_applied    TEXT[]  NOT NULL DEFAULT '{}',
  pricing_tier      TEXT    NOT NULL DEFAULT 'free'
);
CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_id
  ON usage_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp
  ON usage_events (timestamp DESC);
-- Requests Explorer filter columns (the app's startup DDL also self-heals these
-- via ALTER ... IF NOT EXISTS; kept here so a fresh GCP provision matches).
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_level TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS complexity_tier TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS bypassed BOOLEAN NOT NULL DEFAULT false;
-- #4 multi-protocol ingress: which client protocol served this request (never billed).
-- Default must equal protocols.base.DEFAULT_PROTOCOL_NAME (the app-side source of truth).
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'openai';
