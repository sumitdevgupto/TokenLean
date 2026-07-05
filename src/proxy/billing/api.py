"""Billing FastAPI router — per-tenant usage and savings reports."""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def create_billing_router(db_pool: Optional[Any] = None):
    """Return a FastAPI APIRouter with billing endpoints.

    ``db_pool`` is an asyncpg pool; when None the endpoints return 503.
    """
    try:
        from fastapi import APIRouter, HTTPException
    except ImportError:
        return None

    router = APIRouter(prefix="/api/v1/tenants", tags=["billing"])

    # Item 55: scope every read with tenant_conn so the RLS GUC app.tenant_id is set
    # (defense-in-depth atop the WHERE tenant_id filter). Lazy import keeps this core
    # module light in the OSS build, where the router ships unwired.
    from cache.pg_pool import tenant_conn

    async def _query(db, tenant_id: str, sql: str, *args):
        if db is None:
            raise HTTPException(status_code=503, detail="Database unavailable")
        async with tenant_conn(db, tenant_id) as conn:
            return await conn.fetch(sql, *args)

    @router.get("/{tenant_id}/usage")
    async def get_usage(tenant_id: str, limit: int = 100):
        rows = await _query(
            db_pool, tenant_id,
            "SELECT * FROM usage_events WHERE tenant_id=$1 ORDER BY timestamp DESC LIMIT $2",
            tenant_id, limit,
        )
        if not rows:
            raise HTTPException(status_code=404, detail=f"No usage data for tenant '{tenant_id}'")
        return [dict(r) for r in rows]

    @router.get("/{tenant_id}/usage/monthly")
    async def get_usage_monthly(tenant_id: str):
        rows = await _query(
            db_pool, tenant_id,
            """
            SELECT
                date_trunc('month', timestamp) AS month,
                SUM(tokens_saved)    AS total_tokens_saved,
                SUM(cost_saved_usd)  AS total_cost_saved_usd,
                COUNT(*)             AS request_count
            FROM usage_events
            WHERE tenant_id = $1
            GROUP BY 1
            ORDER BY 1 DESC
            """,
            tenant_id,
        )
        return [dict(r) for r in rows]

    @router.get("/{tenant_id}/savings-report")
    async def get_savings_report(tenant_id: str):
        rows = await _query(
            db_pool, tenant_id,
            """
            SELECT
                UNNEST(groups_applied) AS group_id,
                SUM(tokens_saved)      AS tokens_saved,
                SUM(cost_saved_usd)    AS cost_saved_usd,
                COUNT(*)               AS requests
            FROM usage_events
            WHERE tenant_id = $1
            GROUP BY 1
            ORDER BY 2 DESC
            """,
            tenant_id,
        )
        return [dict(r) for r in rows]

    return router
