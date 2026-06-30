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
