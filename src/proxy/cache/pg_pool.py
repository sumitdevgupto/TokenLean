"""Shared asyncpg connection pool to eliminate per-request Postgres connection churn.

Usage:
    from cache.pg_pool import get_pg_pool
    pool = await get_pg_pool(db_url)
    async with pool.acquire() as conn:
        await conn.fetch(...)

Shutdown:
    from cache.pg_pool import close_pool
    await close_pool()
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()
_pool_dsn: Optional[str] = None


@asynccontextmanager
async def tenant_conn(pool, tenant_id: str):
    """Acquire a pooled connection with the Postgres GUC ``app.tenant_id`` set.

    I2 (defense-in-depth): RLS policies scope every query on the tenant tables to
    ``current_setting('app.tenant_id')``, so a forgotten ``WHERE tenant_id`` cannot
    leak. The GUC is reset on release so the next borrower of this pooled
    connection is not scoped to a stale tenant. ``tenant_id`` empty/None leaves the
    GUC unset for cross-tenant admin/GDPR queries (the policies are permissive when
    it is unset). Backward-compatible: ``set_config`` is a no-op read elsewhere when
    RLS has not been applied, so call sites can adopt this before the migration runs.
    """
    async with pool.acquire() as conn:
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant_id or "")
            yield conn
        finally:
            try:
                await conn.execute("SELECT set_config('app.tenant_id', '', false)")
            except Exception as exc:
                logger.debug("app.tenant_id not reset on release: %r", exc)


async def may_run_ddl(pool, table: str) -> bool:
    """Whether this connection's role should run the startup schema steps for *table*.

    True when the role owns the table (or is a member of its owner), or the table does not
    exist yet, so this role would create it: a single-role deployment, as today. False for
    a restricted runtime role (audit/enforcement.py) reading tables that a migration role
    created: that role cannot alter them, and the schema job, run as their owner, keeps
    them current. A probe that fails (a test double, a database error) answers True,
    today's behaviour, and the schema step reports its own error."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pg_has_role(current_user, relowner, 'USAGE') AS mine "
                "FROM pg_class WHERE oid = to_regclass($1)", table)
        return row is None or bool(row["mine"])
    except Exception:
        return True


async def get_pg_pool(dsn: str) -> asyncpg.Pool:
    """Return the shared asyncpg pool, creating it on first use (idempotent)."""
    global _pool, _pool_dsn
    if _pool is not None and _pool_dsn == dsn:
        return _pool
    async with _pool_lock:
        if _pool is None or _pool_dsn != dsn:
            if _pool is not None:
                await _pool.close()
            _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
            _pool_dsn = dsn
    return _pool


async def close_pool() -> None:
    """Close the shared pool — call once at application shutdown."""
    global _pool, _pool_dsn
    if _pool is not None:
        await _pool.close()
        _pool = None
        _pool_dsn = None
