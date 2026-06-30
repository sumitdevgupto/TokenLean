"""Multi-tenancy isolation tests — gate for the Critical + High rows of
docs/business-viability-plan.md.

These tests prove that the cross-tenant leak fixes actually isolate
tenant-alpha from tenant-beta:
  1. G05 L2 pgvector — WHERE tenant_id filter + tenant-scoped query_hash
  2. G05 L3 GPTCache — tenant-scoped query string
  3. G04 Bypass rules — tenant-scoped DB query + per-tenant rule cache
  4. G10 Mem0 — tenant-scoped user_id
  5. G10 Zep — tenant-scoped session_id
  6. G05 Step Cache — tenant-scoped key prefix (High row #7)
  7. G00 Rate Limit — tenant-scoped bucket keys (High row #8)
  8. G04 Bypass stats key — tenant-scoped Redis key (Medium row #10)

Each test simulates two tenants reusing the same raw id (query text, user_id,
session_id) and asserts the underlying call/SQL is tenant-scoped so a
cross-tenant hit/leak cannot occur.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG05L2TenantIsolation:
    def _make_mock_pool(self, fetchrow_result=None):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=fetchrow_result)
        mock_conn.execute = AsyncMock()
        mock_acquire_cm = AsyncMock()
        mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
        return mock_pool, mock_conn

    async def test_l2_lookup_filters_by_tenant_id(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        ctx.tenant_id = "tenant-alpha"
        mock_pool, mock_conn = self._make_mock_pool(fetchrow_result=None)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                    from middleware.g05_cache import _l2_lookup
                    await _l2_lookup(ctx, 0.85)

        sql, embedding_str, threshold, tenant_param, model_scope = mock_conn.fetchrow.await_args.args
        assert "tenant_id" in sql and "model_scope" in sql
        assert tenant_param == "tenant-alpha"
        assert model_scope == ""  # default "tenant" scope filters on empty model_scope

    async def test_l2_store_writes_tenant_id_and_scoped_hash(self, make_ctx):
        ctx_alpha = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        ctx_alpha.tenant_id = "tenant-alpha"
        ctx_beta = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        ctx_beta.tenant_id = "tenant-beta"

        mock_pool, mock_conn = self._make_mock_pool()

        # execute() now also runs the app.tenant_id GUC set/reset (I2 tenant_conn),
        # so pick the INSERT call specifically rather than the most recent execute.
        def _last_insert(conn):
            inserts = [c for c in conn.execute.await_args_list
                       if "INSERT INTO cache_l2" in str(c.args[0])]
            return inserts[-1].args

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                    from middleware.g05_cache import _l2_store
                    await _l2_store(ctx_alpha, {"choices": []}, 3600)
                    args_alpha = _last_insert(mock_conn)
                    hash_alpha = args_alpha[1]
                    # INSERT params end with (..., tenant_id, model_scope)
                    tenant_alpha = args_alpha[-2]
                    model_scope_alpha = args_alpha[-1]

                    await _l2_store(ctx_beta, {"choices": []}, 3600)
                    args_beta = _last_insert(mock_conn)
                    hash_beta = args_beta[1]
                    tenant_beta = args_beta[-2]

        # Same question text, different tenants → different query_hash so the
        # INSERT ... ON CONFLICT(query_hash) can never overwrite/leak across
        # tenants even though the table has no composite unique key.
        assert hash_alpha != hash_beta
        assert tenant_alpha == "tenant-alpha"
        assert tenant_beta == "tenant-beta"
        assert model_scope_alpha == ""  # default "tenant" scope stores empty model_scope


@pytest.mark.asyncio
class TestG05L3GPTCacheTenantIsolation:
    async def test_lookup_and_store_scope_query_by_tenant(self):
        from middleware.g05_cache import _l3_scope_query

        scoped_alpha = _l3_scope_query("capital of France", "tenant-alpha")
        scoped_beta = _l3_scope_query("capital of France", "tenant-beta")
        assert scoped_alpha != scoped_beta
        assert "tenant-alpha" in scoped_alpha
        assert "tenant-beta" in scoped_beta

    async def test_gptcache_lookup_passes_scoped_query_to_cache_search(self):
        mock_cache = MagicMock()
        mock_cache.search = MagicMock(return_value=[])

        with patch("middleware.g05_cache._semantic_cache", mock_cache):
            from middleware.g05_cache import _l3_lookup
            await _l3_lookup("capital of France", 0.85, tenant_id="tenant-alpha")

        called_query = mock_cache.search.call_args.args[0]
        assert "tenant-alpha" in called_query

    async def test_gptcache_store_passes_scoped_query_to_cache_put(self):
        mock_cache = MagicMock()
        mock_cache.put = MagicMock()

        with patch("middleware.g05_cache._semantic_cache", mock_cache):
            from middleware.g05_cache import _l3_store
            await _l3_store("capital of France", {"choices": []}, 3600, tenant_id="tenant-beta")

        called_query = mock_cache.put.call_args.args[0]
        assert "tenant-beta" in called_query


@pytest.mark.asyncio
class TestG04BypassRuleTenantIsolation:
    async def test_load_rules_from_db_filters_by_tenant_id(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.close = AsyncMock()

        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=mock_conn):
            from middleware.g04_bypass import _load_rules_from_db
            await _load_rules_from_db(tenant_id="tenant-alpha")

        sql, tenant_param = mock_conn.fetch.await_args.args
        assert "tenant_id" in sql
        assert tenant_param == "tenant-alpha"

    async def test_tenant_beta_does_not_see_tenant_alpha_rule(self, make_ctx):
        """A rule loaded for tenant-alpha must not bypass tenant-beta's
        identical request — proves per-tenant rule caching, not just the
        SQL filter."""
        ctx_alpha = make_ctx([{"role": "user", "content": "order status please"}])
        ctx_alpha.tenant_id = "tenant-alpha"
        ctx_alpha.config["groups"]["G4_bypass"]["database_first"] = True

        ctx_beta = make_ctx([{"role": "user", "content": "order status please"}])
        ctx_beta.tenant_id = "tenant-beta"
        ctx_beta.config["groups"]["G4_bypass"]["database_first"] = True

        alpha_rule = [{
            "rule_id": "alpha-rule", "name": "alpha-rule", "category": "general",
            "keywords": [], "patterns": [r"order\s+status"],
            "backend_url": None, "static_response": "alpha-only-response",
            "confidence_threshold": 0.5,
        }]

        async def _fake_load_rules(tenant_id=None):
            return alpha_rule if tenant_id == "tenant-alpha" else []

        from middleware.g04_bypass import G04Bypass
        bypass = G04Bypass()

        with patch("middleware.g04_bypass._load_rules_from_db", side_effect=_fake_load_rules):
            ctx_alpha = await bypass.process_request(ctx_alpha)
            ctx_beta = await bypass.process_request(ctx_beta)

        assert ctx_alpha.bypassed is True
        assert ctx_beta.bypassed is False

    async def test_db_cache_ttl_is_independent_per_tenant(self, make_ctx):
        ctx_alpha = make_ctx([{"role": "user", "content": "order #1 please"}])
        ctx_alpha.tenant_id = "tenant-alpha"
        ctx_alpha.config["groups"]["G4_bypass"]["database_first"] = True

        ctx_beta = make_ctx([{"role": "user", "content": "order #2 please"}])
        ctx_beta.tenant_id = "tenant-beta"
        ctx_beta.config["groups"]["G4_bypass"]["database_first"] = True

        db_rules = [{
            "rule_id": "shared-rule", "name": "shared-rule", "category": "general",
            "keywords": [], "patterns": [r"order\s+#\d+"],
            "backend_url": None, "static_response": "from-database",
            "confidence_threshold": 0.5,
        }]

        from middleware.g04_bypass import G04Bypass
        bypass = G04Bypass()

        with patch("middleware.g04_bypass._load_rules_from_db", new_callable=AsyncMock, return_value=db_rules) as mock_load:
            await bypass.process_request(ctx_alpha)
            await bypass.process_request(ctx_beta)

        # Each tenant must trigger its own DB load — a single shared TTL
        # would let tenant-beta freeload off tenant-alpha's cache window.
        assert mock_load.await_count == 2


@pytest.mark.asyncio
class TestG10Mem0TenantIsolation:
    async def test_mem0_retrieve_called_with_tenant_scoped_user_id(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What did I tell you about my order?"}],
            params={"x_user_id": "shared-user-id"},
        )
        ctx.tenant_id = "tenant-alpha"
        ctx.config["groups"]["G10_memory"]["mem0_enabled"] = True

        mock_mem0 = AsyncMock()
        mock_mem0.retrieve_memories = AsyncMock(return_value=[])
        mock_mem0.store_memory = AsyncMock(return_value=True)

        from middleware.g10_memory import G10Memory
        memory = G10Memory()
        memory._mem0 = mock_mem0

        with patch("middleware.g10_memory._get_redis", side_effect=Exception("no redis needed")):
            await memory.process_request(ctx)

        called_user_id = mock_mem0.retrieve_memories.await_args.args[0]
        assert called_user_id == "tenant-alpha::shared-user-id"
        assert called_user_id != "shared-user-id"

    async def test_two_tenants_same_user_id_get_different_scoped_ids(self, make_ctx):
        mock_mem0 = AsyncMock()
        mock_mem0.retrieve_memories = AsyncMock(return_value=[])
        mock_mem0.store_memory = AsyncMock(return_value=True)

        from middleware.g10_memory import G10Memory
        memory = G10Memory()
        memory._mem0 = mock_mem0

        for tenant in ("tenant-alpha", "tenant-beta"):
            ctx = make_ctx(
                [{"role": "user", "content": "Remember my preference"}],
                params={"x_user_id": "shared-user-id"},
            )
            ctx.tenant_id = tenant
            ctx.config["groups"]["G10_memory"]["mem0_enabled"] = True
            with patch("middleware.g10_memory._get_redis", side_effect=Exception("no redis needed")):
                await memory.process_request(ctx)

        seen_user_ids = {c.args[0] for c in mock_mem0.retrieve_memories.await_args_list}
        assert seen_user_ids == {"tenant-alpha::shared-user-id", "tenant-beta::shared-user-id"}


@pytest.mark.asyncio
class TestG10ZepTenantIsolation:
    async def test_zep_get_memory_called_with_tenant_scoped_session_id(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Continue our conversation"}],
            params={"x_session_id": "shared-session-id"},
        )
        ctx.tenant_id = "tenant-alpha"
        ctx.config["groups"]["G10_memory"]["zep_enabled"] = True

        mock_zep = AsyncMock()
        mock_zep.get_memory = AsyncMock(return_value=[])

        from middleware.g10_memory import G10Memory
        memory = G10Memory()
        memory._zep = mock_zep

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()
        mock_redis.expire = AsyncMock()

        with patch("middleware.g10_memory._get_redis", return_value=mock_redis):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock, return_value="summary"):
                await memory.process_request(ctx)

        called_session_id = mock_zep.get_memory.await_args.args[0]
        assert called_session_id == "tenant-alpha::shared-session-id"
        assert called_session_id != "shared-session-id"

    async def test_two_tenants_same_session_id_never_collide(self, make_ctx):
        mock_zep = AsyncMock()
        mock_zep.get_memory = AsyncMock(return_value=[])

        from middleware.g10_memory import G10Memory
        memory = G10Memory()
        memory._zep = mock_zep

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()
        mock_redis.expire = AsyncMock()

        for tenant in ("tenant-alpha", "tenant-beta"):
            ctx = make_ctx(
                [{"role": "user", "content": "Continue our conversation"}],
                params={"x_session_id": "shared-session-id"},
            )
            ctx.tenant_id = tenant
            ctx.config["groups"]["G10_memory"]["zep_enabled"] = True
            with patch("middleware.g10_memory._get_redis", return_value=mock_redis):
                with patch("middleware.g10_memory._summarise", new_callable=AsyncMock, return_value="summary"):
                    await memory.process_request(ctx)

        seen_session_ids = {c.args[0] for c in mock_zep.get_memory.await_args_list}
        assert seen_session_ids == {"tenant-alpha::shared-session-id", "tenant-beta::shared-session-id"}


@pytest.mark.asyncio
class TestG05StepCacheTenantIsolation:
    """High row #7 — _step_cache_key() must be called with prefix=redis_prefix
    at all 3 call sites: _check_step_cache, _store_step_cache, temporal_activity_replay."""

    async def test_check_step_cache_uses_tenant_prefix(self, make_ctx):
        ctx = make_ctx(params={"x_step_name": "step-1", "x_step_inputs_hash": "h1", "x_template_version": "v1"})
        ctx.redis_prefix = "t:tenant-alpha:"

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        from middleware.g05_cache import G05Cache, _step_cache_key
        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            await G05Cache()._check_step_cache(ctx, ctx.config["groups"]["G5_cache"])

        called_key = mock_redis.get.await_args.args[0]
        expected_key = _step_cache_key("step-1", "h1", "v1", prefix="t:tenant-alpha:")
        assert called_key == expected_key
        assert called_key != _step_cache_key("step-1", "h1", "v1", prefix="")

    async def test_store_step_cache_uses_tenant_prefix(self, make_ctx):
        ctx = make_ctx(params={"x_step_name": "step-1", "x_step_inputs_hash": "h1", "x_template_version": "v1"})
        ctx.redis_prefix = "t:tenant-beta:"

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()

        from middleware.g05_cache import G05Cache, _step_cache_key
        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            await G05Cache()._store_step_cache(ctx, {"choices": []}, ctx.config["groups"]["G5_cache"])

        called_key = mock_redis.set.await_args.args[0]
        assert called_key == _step_cache_key("step-1", "h1", "v1", prefix="t:tenant-beta:")

    async def test_temporal_activity_replay_uses_tenant_prefix(self, make_ctx):
        ctx = make_ctx(params={"x_step_name": "step-1", "x_template_version": "v1"})
        ctx.redis_prefix = "t:tenant-gamma:"

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()

        activity = AsyncMock(return_value={"result": "ok"})

        from middleware.g05_cache import G05Cache, _step_cache_key, _hash_args
        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            await G05Cache().temporal_activity_replay(ctx, activity, "arg1")

        called_get_key = mock_redis.get.await_args.args[0]
        inputs_hash = _hash_args(("arg1",), {})
        assert called_get_key == _step_cache_key("step-1", inputs_hash, "v1", prefix="t:tenant-gamma:")

    async def test_two_tenants_same_step_inputs_do_not_collide(self, make_ctx):
        """Two tenants running the identical step (same name/inputs/version)
        must produce different cache keys."""
        from middleware.g05_cache import _step_cache_key

        key_alpha = _step_cache_key("step-1", "h1", "v1", prefix="t:tenant-alpha:")
        key_beta = _step_cache_key("step-1", "h1", "v1", prefix="t:tenant-beta:")
        assert key_alpha != key_beta


@pytest.mark.asyncio
class TestG00RateLimitTenantIsolation:
    """High row #8 — rate limit Redis keys must be scoped by tenant_id so
    tenant-alpha's burst traffic can never throttle tenant-beta."""

    async def test_minute_and_hour_keys_include_tenant_id(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.tenant_id = "tenant-alpha"
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {"requests_per_minute": 60, "requests_per_hour": 1000}

        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={})
        mock_redis.expire = AsyncMock(return_value=True)
        mock_redis.hset = AsyncMock(return_value=True)

        from middleware.g00_rate_limit import G00RateLimit
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            await G00RateLimit().process_request(ctx)

        called_keys = [c.args[0] for c in mock_redis.hgetall.await_args_list]
        assert any("tenant-alpha" in k for k in called_keys)

    async def test_two_tenants_same_user_team_get_independent_buckets(self, make_ctx):
        """Tenant-alpha saturating its bucket must not affect tenant-beta's
        bucket, even with identical user_id/team — proven by distinct keys."""
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={})
        mock_redis.expire = AsyncMock(return_value=True)
        mock_redis.hset = AsyncMock(return_value=True)

        from middleware.g00_rate_limit import G00RateLimit

        seen_keys = []
        for tenant in ("tenant-alpha", "tenant-beta"):
            ctx = make_ctx([{"role": "user", "content": "hello"}])
            ctx.tenant_id = tenant
            ctx.config["rate_limit"]["enabled"] = True
            ctx.config["rate_limit"]["default"] = {"requests_per_minute": 60, "requests_per_hour": 1000}
            with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
                await G00RateLimit().process_request(ctx)
            seen_keys.extend(c.args[0] for c in mock_redis.hgetall.await_args_list)
            mock_redis.hgetall.reset_mock()

        minute_keys = {k for k in seen_keys if ":minute:" in k}
        assert len(minute_keys) == 2  # one per tenant, never shared


@pytest.mark.asyncio
class TestG04BypassStatsKeyTenantIsolation:
    """Medium row #10 — bypass rule effectiveness stats must not blend
    tenant-alpha's hit-rate into tenant-beta's (and vice versa)."""

    async def test_record_bypass_stat_key_includes_tenant_id(self):
        from middleware.g04_bypass import _record_bypass_stat, _BYPASS_STATS_PREFIX

        mock_redis = AsyncMock()
        with patch("middleware.g04_bypass._get_redis", return_value=mock_redis):
            await _record_bypass_stat("rule-1", True, 0.9, tenant_id="tenant-alpha")

        called_key = mock_redis.hincrby.await_args_list[0].args[0]
        assert called_key == f"{_BYPASS_STATS_PREFIX}tenant-alpha:rule-1"

    async def test_two_tenants_same_rule_get_independent_stats_keys(self):
        from middleware.g04_bypass import _record_bypass_stat

        mock_redis = AsyncMock()
        seen_keys = []
        with patch("middleware.g04_bypass._get_redis", return_value=mock_redis):
            for tenant in ("tenant-alpha", "tenant-beta"):
                await _record_bypass_stat("shared-rule", True, 0.9, tenant_id=tenant)
                seen_keys.append(mock_redis.hincrby.await_args_list[-1].args[0])

        assert seen_keys[0] != seen_keys[1]

    async def test_process_request_passes_tenant_id_to_stat_recording(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello there"}])
        ctx.tenant_id = "tenant-gamma"

        from middleware.g04_bypass import G04Bypass
        with patch("middleware.g04_bypass._record_bypass_stat", new_callable=AsyncMock) as mock_stat:
            await G04Bypass().process_request(ctx)

        assert mock_stat.await_args.kwargs.get("tenant_id") == "tenant-gamma"


class TestTenantIsolationCallSiteLint:
    """CI lint: assert that soft-isolation helpers have exactly one call site each.

    A second call site that bypasses the scoped-id helpers (_gptcache_lookup,
    _gptcache_store, Mem0MemoryClient.retrieve_memories / store_memory,
    ZepMemoryClient.add_message / get_memory) would silently cross tenant
    boundaries.  These tests catch that before it ships.

    The search is done with a simple string scan of the source tree so there
    is no import-time dependency on the middleware itself.
    """

    _SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy", "middleware"))

    def _grep(self, pattern: str) -> list:
        """Return (filepath, lineno, line) tuples for every match in middleware/."""
        import re
        hits = []
        for fname in os.listdir(self._SRC):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(self._SRC, fname)
            with open(fpath, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if re.search(pattern, line):
                        hits.append((fname, lineno, line.rstrip()))
        return hits

    def test_gptcache_lookup_single_call_site(self):
        # Only count actual await-call lines for the L3 lookup helper
        hits = self._grep(r"await _l3_lookup\(")
        assert len(hits) == 1, (
            f"_l3_lookup has {len(hits)} await call sites (expected 1). "
            f"New call sites bypass tenant isolation scoping: {hits}"
        )

    def test_gptcache_store_single_call_site(self):
        hits = self._grep(r"await _l3_store\(")
        assert len(hits) == 1, (
            f"_l3_store has {len(hits)} await call sites (expected 1). "
            f"New call sites bypass tenant isolation scoping: {hits}"
        )

    def test_mem0_retrieve_memories_single_call_site(self):
        # The only external call site: `await mem0.retrieve_memories(...)`
        hits = self._grep(r"await mem0\.retrieve_memories\(")
        assert len(hits) == 1, (
            f"retrieve_memories has {len(hits)} external call sites (expected 1). "
            f"New call sites may use an unscoped user_id: {hits}"
        )

    def test_mem0_store_memory_single_call_site(self):
        # g10_memory.py has exactly 2 external store_memory() awaits
        hits = self._grep(r"await mem0\.store_memory\(")
        assert len(hits) == 2, (
            f"store_memory has {len(hits)} external call sites (expected 2 — user+assistant). "
            f"New call sites may bypass scoped_user_id: {hits}"
        )

    def test_zep_add_message_no_unintended_call_sites(self):
        # add_message is defined in ZepMemoryClient but currently not called from
        # process_request (Zep session state is read, not written, in the proxy path).
        # If a caller adds it, it MUST use scoped_session_id — this test asserts 0
        # external await calls so any future addition is intentionally reviewed.
        hits = self._grep(r"await zep\.add_message\(")
        assert len(hits) == 0, (
            f"zep.add_message has {len(hits)} external call site(s) (expected 0 — "
            f"any new call site MUST use scoped_session_id not bare session_id): {hits}"
        )

    def test_zep_get_memory_single_call_site(self):
        # Only the process_request call `await zep.get_memory(scoped_session_id, ...)` counts.
        # The internal `self._client.memory.get_memory(session_id)` in ZepMemoryClient is
        # already scoped (the outer wrapper enforces the scope).
        hits = self._grep(r"await zep\.get_memory\(")
        assert len(hits) == 1, (
            f"zep.get_memory has {len(hits)} external call sites (expected 1). "
            f"New call sites must use scoped_session_id: {hits}"
        )
