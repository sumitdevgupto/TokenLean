"""E1-T: Tests for TenantConfigLoader (per-tenant config overrides from Postgres)."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock


class _AcquireCtx:
    def __init__(self, row):
        self._row = row
        self._conn = _MockConn(row)

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


class _MockConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, query, tenant_id):
        return self._row


class _MockPool:
    def __init__(self, row):
        self._row = row

    def acquire(self):
        return _AcquireCtx(self._row)


class _Ctx:
    def __init__(self, config=None, tenant_id="tenant-a"):
        self.tenant_id = tenant_id
        self.config = config or {}


class TestTenantConfigLoaderNoPool:
    def test_no_pool_leaves_config_unchanged(self):
        from tenancy.config import TenantConfigLoader
        loader = TenantConfigLoader(db_pool=None)
        ctx = _Ctx({"groups": {"G01": {"enabled": True}}})
        asyncio.run(loader.load(ctx))
        assert ctx.config["groups"]["G01"]["enabled"] is True

    def test_no_pool_is_noop(self):
        from tenancy.config import TenantConfigLoader
        loader = TenantConfigLoader(db_pool=None)
        ctx = _Ctx({"proxy": {"port": 4000}})
        original_config = dict(ctx.config)
        asyncio.run(loader.load(ctx))
        assert ctx.config == original_config


class TestTenantConfigLoaderOverrides:
    def _loader(self, row=None):
        from tenancy.config import TenantConfigLoader
        pool = _MockPool(row)
        return TenantConfigLoader(db_pool=pool, cache_ttl=60)

    def test_no_row_leaves_config_unchanged(self):
        loader = self._loader(row=None)
        ctx = _Ctx({"groups": {"G01": {"enabled": True}}})
        asyncio.run(loader.load(ctx))
        assert ctx.config["groups"]["G01"]["enabled"] is True

    def test_override_disables_group(self):
        row = {"config_overrides": json.dumps({"groups": {"G01": {"enabled": False}}})}
        loader = self._loader(row=row)
        ctx = _Ctx({"groups": {"G01": {"enabled": True}}})
        asyncio.run(loader.load(ctx))
        assert ctx.config["groups"]["G01"]["enabled"] is False

    def test_override_adds_top_level_key(self):
        row = {"config_overrides": json.dumps({"custom_key": "custom_value"})}
        loader = self._loader(row=row)
        ctx = _Ctx({})
        asyncio.run(loader.load(ctx))
        assert ctx.config["custom_key"] == "custom_value"

    def test_per_tenant_config_enabled_false_skips(self):
        row = {"config_overrides": json.dumps({"custom": "value"})}
        loader = self._loader(row=row)
        ctx = _Ctx({"tenancy": {"per_tenant_config_enabled": False}})
        asyncio.run(loader.load(ctx))
        assert "custom" not in ctx.config


class TestTenantConfigLoaderCaching:
    def test_cache_avoids_second_db_call(self):
        calls = []

        class _TrackConn(_MockConn):
            async def fetchrow(self, q, tid):
                calls.append(tid)
                return {"config_overrides": json.dumps({})}

        class _TrackAcquireCtx(_AcquireCtx):
            async def __aenter__(self):
                return _TrackConn(None)

        class _TrackPool:
            def acquire(self):
                return _TrackAcquireCtx(None)

        from tenancy.config import TenantConfigLoader
        loader = TenantConfigLoader(db_pool=_TrackPool(), cache_ttl=60)
        ctx = _Ctx({})
        asyncio.run(loader.load(ctx))
        asyncio.run(loader.load(ctx))
        # Second call should use cache
        assert len(calls) == 1

    def test_expired_cache_re_queries(self):
        calls = []

        class _TrackConn(_MockConn):
            async def fetchrow(self, q, tid):
                calls.append(tid)
                return {"config_overrides": json.dumps({})}

        class _TrackAcquireCtx(_AcquireCtx):
            async def __aenter__(self):
                return _TrackConn(None)

        class _TrackPool:
            def acquire(self):
                return _TrackAcquireCtx(None)

        from tenancy.config import TenantConfigLoader
        loader = TenantConfigLoader(db_pool=_TrackPool(), cache_ttl=0)
        ctx = _Ctx({})
        asyncio.run(loader.load(ctx))
        asyncio.run(loader.load(ctx))
        assert len(calls) == 2
