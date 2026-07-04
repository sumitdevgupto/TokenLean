"""Billing domain models and Postgres DDL."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional


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
    pricing_tier    TEXT        NOT NULL DEFAULT 'basic',
    model           TEXT        NOT NULL DEFAULT '',
    routed_model    TEXT        NOT NULL DEFAULT '',
    otel_trace_id   TEXT        NOT NULL DEFAULT '',
    proxy_optimised_tokens INTEGER NOT NULL DEFAULT 0,
    provider_prompt_tokens INTEGER,
    response_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS usage_events_tenant_ts_idx
    ON usage_events (tenant_id, timestamp DESC);

-- C2: idempotent migration for already-existing tables (non-destructive)
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS proxy_optimised_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider_prompt_tokens INTEGER;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS response_tokens INTEGER NOT NULL DEFAULT 0;
"""
