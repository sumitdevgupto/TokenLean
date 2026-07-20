"""Billing domain models and Postgres DDL."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
    # C1 — per-G-group realised token savings {group: tokens_saved}, non-zero steps only.
    # Lets the indexed usage_events table answer "savings by optimisation" without parsing
    # the unindexed Langfuse traces blob. Compact (~2–8 keys). Observability; never billed.
    group_savings: Dict[str, int] = field(default_factory=dict)
    # C2 — reliability/latency observability (never billed). status_code is the served HTTP
    # status; billable marks whether this row counts as a billable unit (2xx only) so
    # error/latency rows can exist without inflating the request-count invoice. Latencies
    # in ms: total = end-to-end, llm = provider-call only (proxy overhead = total - llm).
    status_code: int = 0
    billable: bool = True
    total_duration_ms: int = 0
    llm_duration_ms: int = 0
    # F2/F3 — the registered downstream agent this request was dispatched to by intent
    # orchestration (empty = normal LLM path). Observability only, never billed; lets the
    # portal's routing-decision view answer "which agent handled request X" joined to cost.
    agent_id: str = ""
    # Free-trial: True when this served 2xx was made while the tenant's trial was active.
    # Persisted for usage visibility but EXCLUDED from invoices (a trial-only period bills
    # $0). Set at write time from ctx.config so it is robust to a later trial-state edit.
    trial: bool = False

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
    protocol        TEXT        NOT NULL DEFAULT '__PROTO_DEFAULT__',
    group_savings   JSONB       NOT NULL DEFAULT '{}',
    status_code     SMALLINT    NOT NULL DEFAULT 0,
    billable        BOOLEAN     NOT NULL DEFAULT true,
    total_duration_ms INTEGER   NOT NULL DEFAULT 0,
    llm_duration_ms INTEGER     NOT NULL DEFAULT 0,
    agent_id        TEXT        NOT NULL DEFAULT '',
    trial           BOOLEAN     NOT NULL DEFAULT false
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
-- model/routed_model/otel_trace_id are in the CREATE TABLE above, but that is a no-op when
-- the table already exists from an earlier (minimal) migration — so self-heal them here too,
-- else metering's INSERT fails with 'column "model" does not exist'.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS routed_model TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS otel_trace_id TEXT NOT NULL DEFAULT '';
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
-- C1: per-G-group realised token savings (observability; never billed)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS group_savings JSONB NOT NULL DEFAULT '{}';
-- C2: reliability/latency observability + billable flag (error/latency rows exist without
-- inflating the request-count invoice; invoice/quota queries filter WHERE billable)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS status_code SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS billable BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS total_duration_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS llm_duration_ms INTEGER NOT NULL DEFAULT 0;
-- F2/F3: downstream agent this request was dispatched to (observability; never billed)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS agent_id TEXT NOT NULL DEFAULT '';
-- Free trial: flags a served 2xx made during an active trial; EXCLUDED from invoices
-- (invoice/usage-agg SQL filters `AND NOT COALESCE(trial, false)`) but kept for visibility.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS trial BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS usage_events_tenant_user_idx ON usage_events (tenant_id, user_id);
-- C2: keep the error-rate / latency-percentile queries index-only over the hot window.
CREATE INDEX IF NOT EXISTS usage_events_tenant_ts_status_idx
    ON usage_events (tenant_id, timestamp DESC) INCLUDE (status_code, total_duration_ms, billable);
""".replace("__PROTO_DEFAULT__", DEFAULT_PROTOCOL_NAME)
