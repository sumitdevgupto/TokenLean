"""Real Postgres: an admin key's answer as another tenant is stored naming the impersonator,
and not billable; the tenant's own answer is billable as before.

The table is built the way a deploy builds it: the in-VPC migration (infra/migrations/
billing.sql), then the app's self-heal DDL twice (it must be idempotent). TEST_PG_DSN must
point at a THROWAWAY server: the test drops and recreates usage_events.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

MIGRATION = Path(__file__).resolve().parents[2] / "infra" / "migrations" / "billing.sql"


def _ctx(request_id, impersonator):
    from middleware import RequestContext
    from savings.models import SavingsRecord
    savings = SavingsRecord(request_id=request_id, user_id="u1", timestamp=datetime.now(timezone.utc),
                            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=100,
                            final_tokens_sent=80)
    ctx = RequestContext(request_id=request_id, user_id="u1", original_messages=[], messages=[],
                         model="gpt-4o", routed_model="gpt-4o", params={}, config={"groups": {}},
                         savings=savings, tenant_id="ACME-PRD-01", pricing_tier="free")
    ctx.impersonator_tenant_id = impersonator
    return ctx


async def _write_and_read():
    import asyncpg
    from billing.metering import UsageMeter
    from billing.models import USAGE_EVENTS_DDL
    pool = await asyncpg.create_pool(os.environ["TEST_PG_DSN"], min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS usage_events")
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
            await conn.execute(USAGE_EVENTS_DDL)
            await conn.execute(USAGE_EVENTS_DDL)
        meter = UsageMeter(db_pool=pool)
        await meter.record(_ctx("req-by-ops", "ops"), {}, billable=False)
        await meter.record(_ctx("req-by-acme", None), {}, billable=True)
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT request_id, tenant_id, impersonated_by, billable "
                                    "FROM usage_events ORDER BY request_id")
            await conn.execute("DROP TABLE usage_events")
        return [tuple(r) for r in rows]
    finally:
        await pool.close()


def test_the_impersonator_is_stored_and_the_row_is_not_billable():
    assert asyncio.run(_write_and_read()) == [
        ("req-by-acme", "ACME-PRD-01", "", True),
        ("req-by-ops", "ACME-PRD-01", "ops", False),
    ]
