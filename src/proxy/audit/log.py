"""
AuditLogger — appends immutable audit events to the Postgres `audit_events`
table.  The Postgres role used at runtime has INSERT-only privilege on this
table; UPDATE and DELETE are forbidden at the database level.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Mirrors the Terraform heredoc (infra/main.tf audit_events_schema_migration) so local /
# self-host deployments get the table without Terraform; the ALTER back-fills `details`
# on GCP databases created at schema_version=1. All statements are idempotent.
AUDIT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
  id             BIGSERIAL    PRIMARY KEY,
  tenant_id      TEXT         NOT NULL,
  request_id     TEXT         NOT NULL,
  timestamp      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  action         TEXT         NOT NULL DEFAULT 'proxy_request',
  user_id        TEXT,
  groups_applied TEXT[]       NOT NULL DEFAULT '{}',
  tokens_saved   INT          NOT NULL DEFAULT 0,
  otel_trace_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_id
  ON audit_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp
  ON audit_events (timestamp DESC);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS details JSONB;
"""


async def ensure_audit_schema(pg_pool) -> None:
    """Create ``audit_events`` (+ the ``details`` column) if absent. Idempotent.

    Failures are logged, never raised — audit must not block startup.
    """
    if pg_pool is None:
        return
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute(AUDIT_EVENTS_DDL)
    except Exception as exc:
        logger.warning("ensure_audit_schema failed: %s", exc)


class AuditLogger:
    """Write one audit row per proxy request to `audit_events`.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.  When ``None`` the logger is a no-op (useful
        in tests and environments without a database).
    """

    def __init__(self, db_pool=None) -> None:
        self._db_pool = db_pool

    async def log(self, ctx, response: Dict[str, Any]) -> None:
        """Insert one audit row for the completed request.

        Called from G18 after metrics are recorded.  Non-blocking: any
        exception is caught and logged as a warning rather than surfaced to
        the caller.
        """
        if not self._db_pool:
            return

        try:
            await self._insert(ctx, response)
        except Exception as exc:
            logger.warning(
                "[%s] AuditLogger: failed to write audit event: %s",
                getattr(ctx, "request_id", "?"),
                exc,
            )

    async def log_config_change(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        details: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> bool:
        """Append one config-change audit event (portal/admin writes). Never raises.

        ``actor`` goes into ``user_id`` (portal email, ``admin-key:<tenant>`` or
        ``key:<tenant>``); ``details`` is a JSON diff/summary — callers must never put
        secrets in it (provider keys are recorded as provider + last4 only). During the
        rollout window where the ``details`` column doesn't exist yet (Terraform race),
        the insert retries without it so the event is not lost.
        """
        if not self._db_pool:
            return False
        base_sql = (
            "INSERT INTO audit_events (tenant_id, request_id, timestamp, action, user_id"
        )
        args = [
            (tenant_id or "default"),
            request_id or f"cfg-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            datetime.now(timezone.utc),
            (action or "config.updated")[:200],
            (actor or "")[:320] or None,
        ]
        try:
            async with self._db_pool.acquire() as conn:
                try:
                    await conn.execute(
                        base_sql + ", details) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                        *args, json.dumps(details or {}),
                    )
                except Exception as exc:
                    if type(exc).__name__ != "UndefinedColumnError":
                        raise
                    await conn.execute(
                        base_sql + ") VALUES ($1,$2,$3,$4,$5)", *args
                    )
            logger.info(
                "audit: %s tenant=%s actor=%s", action, tenant_id, actor
            )
            return True
        except Exception as exc:
            logger.warning(
                "AuditLogger.log_config_change failed (tenant=%s action=%s): %s",
                tenant_id, action, exc,
            )
            return False

    async def _insert(self, ctx, response: Dict[str, Any]) -> None:
        tenant_id = getattr(ctx, "tenant_id", "default")
        request_id = getattr(ctx, "request_id", "unknown")
        user_id = getattr(ctx, "user_id", None)
        # I6: when an admin key impersonated this tenant, record the actor in the
        # action so the audit trail shows who acted on whose behalf (no schema
        # change — folded into the action string).
        impersonator = getattr(ctx, "impersonator_tenant_id", None)
        action = f"proxy_request;impersonator={impersonator}" if impersonator else "proxy_request"
        groups_applied = [
            s.group for s in ctx.savings.step_savings if s.absolute_saving > 0
        ] if hasattr(ctx, "savings") and hasattr(ctx.savings, "step_savings") else []

        otel_trace_id: Optional[str] = None
        span = getattr(ctx, "otel_span", None)
        if span and hasattr(span, "get_span_context"):
            sc = span.get_span_context()
            if sc and sc.trace_id:
                otel_trace_id = format(sc.trace_id, "032x")

        tokens_saved = 0
        if hasattr(ctx, "savings"):
            baseline = getattr(ctx.savings, "baseline_tokens", 0)
            final = getattr(ctx.savings, "final_tokens_sent", baseline)
            tokens_saved = max(0, baseline - final)

        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_events
                    (tenant_id, request_id, timestamp, action, user_id,
                     groups_applied, tokens_saved, otel_trace_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                tenant_id,
                request_id,
                datetime.now(timezone.utc),
                action,
                user_id,
                groups_applied,
                tokens_saved,
                otel_trace_id,
            )
        logger.debug(
            "[%s] AuditLogger: wrote audit event for tenant %s",
            request_id,
            tenant_id,
        )
