"""
TenantConfigLoader — loads per-tenant config overrides from Postgres
`tenant_configs` table and merges them into ctx.config at request time.

Overrides are cached in-process for `cache_ttl` seconds to avoid a
Postgres round-trip on every request.
"""
import copy
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class TenantConfigLoader:
    """Load per-tenant config overrides from Postgres `tenant_configs` table.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.  When ``None`` the loader is a no-op.
    cache_ttl:
        Seconds to cache each tenant's overrides in memory (default 60 s).
    """

    def __init__(self, db_pool=None, cache_ttl: int = 60) -> None:
        self._db_pool = db_pool
        self._cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}  # tenant_id → (overrides, expires_at)

    async def load(self, ctx) -> None:
        """Merge per-tenant config overrides into ctx.config.

        If the database is unavailable or the tenant has no row in
        ``tenant_configs`` ctx.config is left unchanged.
        """
        if not self._db_pool:
            return

        tenancy_cfg = ctx.config.get("tenancy", {})
        if not tenancy_cfg.get("per_tenant_config_enabled", True):
            return

        tenant_id = getattr(ctx, "tenant_id", "default")
        overrides = await self._get_overrides(tenant_id)
        if not overrides:
            return

        def _dm(base, ov):
            for k, v in ov.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    _dm(base[k], v)
                else:
                    base[k] = v

        merged = copy.deepcopy(ctx.config)
        _dm(merged, overrides)
        ctx.config = merged
        logger.debug(
            "TenantConfigLoader: applied %d override key(s) for tenant %s",
            len(overrides),
            tenant_id,
        )

    async def _get_overrides(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached and now < cached[1]:
            return cached[0]

        try:
            async with self._db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT config_overrides FROM tenant_configs WHERE tenant_id = $1",
                    tenant_id,
                )
            overrides: Optional[Dict[str, Any]] = None
            if row and row["config_overrides"]:
                raw = row["config_overrides"]
                overrides = json.loads(raw) if isinstance(raw, str) else dict(raw)
            self._cache[tenant_id] = (overrides, now + self._cache_ttl)
            return overrides
        except Exception as exc:
            logger.warning(
                "TenantConfigLoader: failed to load overrides for tenant %s: %s",
                tenant_id,
                exc,
            )
            return None
