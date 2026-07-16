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
-- Model routing + trace id. These live in the app's CREATE TABLE (billing/models.py) but
-- ONLY get created there on a fresh table — since this migration creates the table first,
-- CREATE TABLE IF NOT EXISTS is a no-op for the app and these columns are never added,
-- so metering's INSERT fails with 'column "model" does not exist'. Add them here explicitly.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS routed_model TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS otel_trace_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS proxy_optimised_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider_prompt_tokens INTEGER;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS response_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_level TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS complexity_tier TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS bypassed BOOLEAN NOT NULL DEFAULT false;
-- #4 multi-protocol ingress: which client protocol served this request (never billed).
-- Default must equal protocols.base.DEFAULT_PROTOCOL_NAME (the app-side source of truth).
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'openai';
-- Token & cost transparency + reliability/latency observability (never billed). Mirrors the
-- app self-heal in billing/models.py; kept here so the in-VPC migration produces the FULL
-- schema the metering INSERT (billing/metering.py) writes — otherwise metering 500s on the
-- first missing column and no usage row is recorded.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_actual_usd NUMERIC(12,6) NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_baseline_usd NUMERIC(12,6) NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS group_savings JSONB NOT NULL DEFAULT '{}';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS status_code SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS billable BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS total_duration_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS llm_duration_ms INTEGER NOT NULL DEFAULT 0;
