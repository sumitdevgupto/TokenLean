"""Billing domain models and Postgres DDL."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional

from protocols.base import DEFAULT_PROTOCOL_NAME


@dataclass
class UsageEvent:
    tenant_id: str
    request_id: str
    timestamp: datetime
    baseline_tokens: int
    optimised_tokens: int
    tokens_saved: int
    cost_saved_usd: float
    groups_applied: List[str]
    pricing_tier: str
    model: str = ""
    routed_model: str = ""
    otel_trace_id: str = ""
    # C2 — lean confidence columns (Track 2 — estimates, never billed): y and z.
    # x = baseline_tokens (above); optimised_tokens ≈ z post-G18 (kept for back-compat).
    proxy_optimised_tokens: int = 0
    provider_prompt_tokens: Optional[int] = None
    # Real output tokens from the provider response (observability — never billed).
    # Pairs with provider_prompt_tokens (z, real input) so the metering engine holds the
    # full real input+output picture, not just input. 0 on defer / no-usage paths.
    response_tokens: int = 0
    # Filter/observability metadata for the Requests Explorer dashboard (never billed).
    # Mirror the request-context flags so the fast, indexed usage_events table carries
    # every filterable field — avoids querying the unindexed Langfuse traces JSONB blob.
    user_id: str = ""
    cache_hit: bool = False
    cache_level: str = ""
    complexity_tier: str = ""
    bypassed: bool = False
    # Token & cost transparency (observability — NEVER billed; billing = request count).
    # cost_actual_usd = what this call cost at config prices; cost_baseline_usd = the
    # unoptimised would-have-cost; provider = resolved provider name (openai/anthropic/…).
    cost_actual_usd: float = 0.0
    cost_baseline_usd: float = 0.0
    provider: str = ""
    # Ingress protocol the client used (#4): openai | anthropic | gemini. Observability
    # only — billing is one row per served request regardless of protocol.
    protocol: str = DEFAULT_PROTOCOL_NAME

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


# Postgres DDL — applied via migration script or Terraform
USAGE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    request_id      TEXT        NOT NULL UNIQUE,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline_tokens INTEGER     NOT NULL DEFAULT 0,
    optimised_tokens INTEGER    NOT NULL DEFAULT 0,
    tokens_saved    INTEGER     NOT NULL DEFAULT 0,
    cost_saved_usd  NUMERIC(12,6) NOT NULL DEFAULT 0,
    groups_applied  TEXT[]      NOT NULL DEFAULT '{}',
    pricing_tier    TEXT        NOT NULL DEFAULT 'free',
    model           TEXT        NOT NULL DEFAULT '',
    routed_model    TEXT        NOT NULL DEFAULT '',
    otel_trace_id   TEXT        NOT NULL DEFAULT '',
    proxy_optimised_tokens INTEGER NOT NULL DEFAULT 0,
    provider_prompt_tokens INTEGER,
    response_tokens INTEGER NOT NULL DEFAULT 0,
    user_id         TEXT        NOT NULL DEFAULT '',
    cache_hit       BOOLEAN     NOT NULL DEFAULT false,
    cache_level     TEXT        NOT NULL DEFAULT '',
    complexity_tier TEXT        NOT NULL DEFAULT '',
    bypassed        BOOLEAN     NOT NULL DEFAULT false,
    cost_actual_usd   NUMERIC(12,6) NOT NULL DEFAULT 0,
    cost_baseline_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
    provider        TEXT        NOT NULL DEFAULT '',
    protocol        TEXT        NOT NULL DEFAULT '__PROTO_DEFAULT__'
);

CREATE INDEX IF NOT EXISTS usage_events_tenant_ts_idx
    ON usage_events (tenant_id, timestamp DESC);
-- Per-user token/cost rollups (item 18 + token transparency) stay fast at scale.
CREATE INDEX IF NOT EXISTS usage_events_tenant_user_idx
    ON usage_events (tenant_id, user_id);

-- C2: idempotent migration for already-existing tables (non-destructive)
-- FREE/ENTERPRISE tier collapse: default new rows to the $0 self-host floor. Existing
-- legacy-tier rows (e.g. 'basic') are left as-is and invoice at $0 via the invoicing fallback.
ALTER TABLE usage_events ALTER COLUMN pricing_tier SET DEFAULT 'free';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS proxy_optimised_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider_prompt_tokens INTEGER;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS response_tokens INTEGER NOT NULL DEFAULT 0;
-- Requests Explorer filter/observability columns (non-destructive; never billed)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_level TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS complexity_tier TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS bypassed BOOLEAN NOT NULL DEFAULT false;
-- Token & cost transparency (observability; never billed)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_actual_usd NUMERIC(12,6) NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_baseline_usd NUMERIC(12,6) NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
-- #4 multi-protocol ingress: which client protocol served this request (never billed)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT '__PROTO_DEFAULT__';
CREATE INDEX IF NOT EXISTS usage_events_tenant_user_idx ON usage_events (tenant_id, user_id);
""".replace("__PROTO_DEFAULT__", DEFAULT_PROTOCOL_NAME)
