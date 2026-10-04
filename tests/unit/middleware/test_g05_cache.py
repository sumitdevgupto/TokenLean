"""Unit tests for G05 — Response & Step Caching (L1 + L2)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


_CACHED_RESPONSE = json.dumps({
    "id": "cached-1",
    "choices": [{"message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 0, "completion_tokens": 5},
})


@pytest.mark.asyncio
class TestG05Cache:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G5_cache"]["enabled"] = False
        from middleware.g05_cache import G05Cache
        ctx = await G05Cache().process_request(ctx)
        assert ctx.cache_hit is False

    async def test_no_cache_skips_read(self, make_ctx):
        # G29 masked PII → lossy cache key → G05 must not read the shared cache,
        # even when a matching entry exists (would else serve another caller's answer).
        ctx = make_ctx()
        ctx.no_cache = True
        get_redis = MagicMock()
        with patch("middleware.g05_cache._get_redis", get_redis):
            from middleware.g05_cache import G05Cache
            ctx = await G05Cache().process_request(ctx)
        assert ctx.cache_hit is False
        get_redis.assert_not_called()  # short-circuits before touching Redis

    async def test_no_cache_skips_store(self, make_ctx):
        ctx = make_ctx()
        ctx.no_cache = True
        get_redis = MagicMock()
        with patch("middleware.g05_cache._get_redis", get_redis):
            from middleware.g05_cache import G05Cache
            await G05Cache().store_response(ctx, {"choices": [{"message": {"content": "x"}}]})
        get_redis.assert_not_called()  # a masked request's answer is never cached

    async def test_x_no_cache_param_skips_read_and_store(self, make_ctx):
        """An internal self-call (e.g. commercial docs-chat) that already applies its OWN
        caching layer opts OUT of G05 via the x_no_cache request param — G05's semantic L2
        match on the internal prompt text would otherwise serve one differently-grounded
        call's answer in place of another's. Regression for the docs-chat cache-pollution bug."""
        ctx = make_ctx()
        ctx.params["x_no_cache"] = "true"
        get_redis = MagicMock()
        with patch("middleware.g05_cache._get_redis", get_redis):
            from middleware.g05_cache import G05Cache
            g05 = G05Cache()
            ctx = await g05.process_request(ctx)
            assert ctx.no_cache is True  # param → ctx flag, so BOTH read and write guards trip
            await g05.store_response(ctx, {"choices": [{"message": {"content": "x"}}]})
        assert ctx.cache_hit is False
        get_redis.assert_not_called()  # short-circuits before touching Redis on read AND write

    async def test_l1_cache_hit(self, make_ctx):
        ctx = make_ctx()

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=_CACHED_RESPONSE)
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            from middleware.g05_cache import G05Cache
            ctx = await G05Cache().process_request(ctx)

        assert ctx.cache_hit is True
        assert ctx.cache_level == "L1"
        assert ctx.savings.cache_hit is True
        assert ctx.savings.cache_level == "L1"

    async def test_l1_hit_records_step_saving(self, make_ctx):
        ctx = make_ctx()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=_CACHED_RESPONSE)
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            from middleware.g05_cache import G05Cache
            ctx = await G05Cache().process_request(ctx)

        assert len(ctx.savings.step_savings) == 1
        step = ctx.savings.step_savings[0]
        assert step.group == "G05"
        assert step.tokens_after == 0

    async def test_l1_miss_l2_hit(self, make_ctx):
        ctx = make_ctx()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)  # L1 miss
        mock_redis.aclose = AsyncMock()

        cached_resp = {"choices": [{"message": {"content": "Paris"}}]}

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            with patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock,
                       return_value=(cached_resp, 0.95)):
                from middleware.g05_cache import G05Cache
                ctx = await G05Cache().process_request(ctx)

        assert ctx.cache_hit is True
        assert ctx.cache_level == "L2"

    async def test_l1_miss_l2_miss_no_cache_hit(self, make_ctx):
        ctx = make_ctx()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            with patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock,
                       return_value=(None, 0.0)):
                from middleware.g05_cache import G05Cache
                ctx = await G05Cache().process_request(ctx)

        assert ctx.cache_hit is False

    async def test_redis_error_graceful_fallback(self, make_ctx):
        ctx = make_ctx()
        with patch("middleware.g05_cache._get_redis", side_effect=Exception("Redis unavailable")):
            with patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock,
                       return_value=(None, 0.0)):
                from middleware.g05_cache import G05Cache
                ctx = await G05Cache().process_request(ctx)
        assert ctx.cache_hit is False

    async def test_store_response_skipped_on_cache_hit(self, make_ctx):
        ctx = make_ctx()
        ctx.cache_hit = True
        mock_redis = AsyncMock()

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            from middleware.g05_cache import G05Cache
            await G05Cache().store_response(ctx, {})

        mock_redis.set.assert_not_called()

    async def test_store_response_skipped_on_agent_dispatched(self, make_ctx):
        """Regression: an F2 agent-dispatched answer must never be cached — G05's lookup
        runs BEFORE F2 in the pipeline, so caching it would let a later matching prompt be
        served straight from cache, bypassing intent classification entirely and replaying
        a stale agent answer even after the agent is disabled/removed."""
        ctx = make_ctx()
        ctx.agent_dispatched = True
        mock_redis = AsyncMock()

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            from middleware.g05_cache import G05Cache
            await G05Cache().store_response(ctx, {"choices": [{"message": {"content": "x"}}]})

        mock_redis.set.assert_not_called()

    async def test_store_response_calls_redis(self, make_ctx):
        ctx = make_ctx()
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
            with patch("middleware.g05_cache._l2_store", new_callable=AsyncMock):
                from middleware.g05_cache import G05Cache
                await G05Cache().store_response(ctx, {"choices": [{"message": {"content": "Paris"}}]})

        mock_redis.set.assert_called_once()


@pytest.mark.asyncio
class TestG05L2VerbosityIsolation:
    """Regression coverage: L2's SELECT (lookup) must filter on the SAME combined
    model+verbosity scope value that INSERT (store) writes — otherwise a terse-mode
    answer can be semantically served to a verbose-configured request via L2, even
    though query_hash (INSERT-only, never read by SELECT) folds verbosity in."""

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

    def _ctx_with_verbosity(self, make_ctx, level="full"):
        ctx = make_ctx([{"role": "user", "content": "Explain the outage"}])
        ctx.config["groups"]["G11_output"] = {
            "enabled": True,
            "verbosity_steering": {"enabled": True, "level": level},
        }
        return ctx

    @staticmethod
    def _insert_call_args(mock_conn):
        """tenant_conn() also issues SET/RESET app.tenant_id via conn.execute (I2),
        so the INSERT is not necessarily the last execute() call — filter for it
        explicitly, matching the pattern in TestG05L2PgPool below."""
        insert_calls = [
            c for c in mock_conn.execute.await_args_list
            if "INSERT INTO cache_l2" in str(c.args[0])
        ]
        assert len(insert_calls) == 1
        return insert_calls[0].args

    async def test_lookup_and_store_use_identical_scope_value(self, make_ctx):
        ctx = self._ctx_with_verbosity(make_ctx, level="ultra")
        mock_pool, mock_conn = self._make_mock_pool(fetchrow_result=None)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock):
                    with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                        from middleware.g05_cache import _l2_lookup, _l2_store
                        await _l2_lookup(ctx, 0.85)
                        lookup_scope = mock_conn.fetchrow.await_args.args[4]  # WHERE model_scope = $4

                        await _l2_store(ctx, {"choices": []}, 3600)
                        store_scope = self._insert_call_args(mock_conn)[6]  # INSERT ... model_scope

        assert lookup_scope, "verbosity-steered lookup must produce a non-empty scope"
        assert lookup_scope == store_scope, (
            "L2 lookup and store scope values diverged — a terse/verbose cache "
            "collision is possible"
        )

    async def test_default_off_scope_is_empty_both_paths(self, make_ctx):
        # No verbosity steering configured → byte-identical to pre-feature ("").
        ctx = make_ctx([{"role": "user", "content": "Explain the outage"}])
        mock_pool, mock_conn = self._make_mock_pool(fetchrow_result=None)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock):
                    with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                        from middleware.g05_cache import _l2_lookup, _l2_store
                        await _l2_lookup(ctx, 0.85)
                        lookup_scope = mock_conn.fetchrow.await_args.args[4]
                        await _l2_store(ctx, {"choices": []}, 3600)
                        store_scope = self._insert_call_args(mock_conn)[6]

        assert lookup_scope == "" and store_scope == ""

    async def test_different_verbosity_levels_produce_different_scope(self, make_ctx):
        mock_pool, mock_conn = self._make_mock_pool(fetchrow_result=None)
        scopes = []
        for level in ("lite", "ultra"):
            ctx = self._ctx_with_verbosity(make_ctx, level=level)
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
                with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                    with patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock):
                        with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                            from middleware.g05_cache import _l2_lookup
                            await _l2_lookup(ctx, 0.85)
                            scopes.append(mock_conn.fetchrow.await_args.args[4])
        assert scopes[0] != scopes[1]

    async def test_verbosity_tag_computed_once_and_cached_on_ctx(self, make_ctx):
        """DRY fix: _verbosity_scope_tag must not recompute verbosity_cache_tag on
        every call within the same request — it should stash the result on ctx.params."""
        from middleware.g05_cache import _verbosity_scope_tag
        ctx = self._ctx_with_verbosity(make_ctx, level="full")
        with patch(
            "middleware.g11_output_format.verbosity_cache_tag", return_value="vbabc123"
        ) as mock_tag:
            first = _verbosity_scope_tag(ctx)
            second = _verbosity_scope_tag(ctx)
        assert first == second == "vbabc123"
        mock_tag.assert_called_once()


@pytest.mark.asyncio
class TestG05L2PgPool:
    """Pool-lifecycle tests for the G5 L2 pgvector cache (acquire/release under
    a shared asyncpg pool, instead of per-request connect/close)."""

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

    async def test_l2_lookup_uses_shared_pool_acquire(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        mock_pool, mock_conn = self._make_mock_pool(fetchrow_result=None)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock):
                    with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                        from middleware.g05_cache import _l2_lookup
                        result = await _l2_lookup(ctx, 0.85)

        assert result == (None, 0.0)
        mock_pool.acquire.assert_called_once()
        mock_conn.fetchrow.assert_awaited_once()

    async def test_l2_store_uses_shared_pool_acquire(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        mock_pool, mock_conn = self._make_mock_pool()

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock):
                    with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
                        from middleware.g05_cache import _l2_store
                        await _l2_store(ctx, {"choices": []}, 3600)

        mock_pool.acquire.assert_called_once()
        # execute is now called for the app.tenant_id GUC (set + reset, I2) plus
        # the INSERT — assert the INSERT itself happened exactly once.
        insert_calls = [
            c for c in mock_conn.execute.await_args_list
            if "INSERT INTO cache_l2" in str(c.args[0])
        ]
        assert len(insert_calls) == 1

    async def test_concurrent_l2_lookups_reuse_same_pool(self, make_ctx):
        """Concurrent L2 lookups must all resolve via get_pg_pool, which
        returns the same shared pool instance — not a fresh connection each
        time."""
        import asyncio

        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        mock_pool, mock_conn = self._make_mock_pool(fetchrow_result=None)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]):
                with patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock):
                    with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool) as mock_get_pool:
                        from middleware.g05_cache import _l2_lookup
                        results = await asyncio.gather(
                            _l2_lookup(ctx, 0.85),
                            _l2_lookup(ctx, 0.85),
                            _l2_lookup(ctx, 0.85),
                        )

        assert all(r == (None, 0.0) for r in results)
        assert mock_pool.acquire.call_count == 3
        for call in mock_get_pool.await_args_list:
            assert call.args[0] == "postgresql://test/db"


@pytest.mark.asyncio
class TestG05CacheL2Schema:
    """The cache_l2 table self-heal — must create the table on a fresh DB and
    add the tenant_id column on a persisted old-schema DB, exactly once per
    process (g05_cache._ensure_cache_l2_schema)."""

    def _make_mock_pool(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        mock_acquire_cm = AsyncMock()
        mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_cm.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
        return mock_pool, mock_conn

    async def test_ensure_schema_runs_create_alter_index_ddl(self, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_cache_l2_schema_ready", False)
        build = MagicMock()
        monkeypatch.setattr(g05, "_spawn_l2_index_build", build)
        mock_pool, mock_conn = self._make_mock_pool()

        await g05._ensure_cache_l2_schema(mock_pool)
        build.assert_called_once_with(mock_pool)      # the vector index, in the background

        # CREATE TABLE + ALTER tenant_id + INDEX tenant + ALTER model_scope + INDEX tenant_model = 5
        assert mock_conn.execute.await_count == 5
        sql = " ".join(str(c.args[0]) for c in mock_conn.execute.await_args_list)
        assert "CREATE TABLE IF NOT EXISTS cache_l2" in sql
        assert "ADD COLUMN IF NOT EXISTS" in sql and "tenant_id" in sql
        assert "model_scope" in sql
        assert "idx_cache_l2_tenant" in sql

    async def test_ensure_schema_runs_only_once_per_process(self, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_cache_l2_schema_ready", False)
        build = MagicMock()
        monkeypatch.setattr(g05, "_spawn_l2_index_build", build)
        mock_pool, mock_conn = self._make_mock_pool()

        await g05._ensure_cache_l2_schema(mock_pool)
        first = (mock_conn.execute.await_count, mock_pool.acquire.call_count)
        await g05._ensure_cache_l2_schema(mock_pool)  # guard flag → no-op

        assert build.call_count == 1
        assert mock_conn.execute.await_count == 5  # not 10
        # The second call touches nothing: no ownership probe, no DDL.
        assert (mock_conn.execute.await_count, mock_pool.acquire.call_count) == first


class TestSemanticQueryText:
    """L2/L3 must match on the user turns, not the whole message string — otherwise a
    system prompt longer than the embedding window dominates the vector and collapses
    distinct questions onto one another (returning a cached answer for a different Q)."""

    # A system prompt longer than the bge-small ~512-token embedding window.
    BIG_SYS = {"role": "system", "content": "You are a support assistant. " * 200}

    def _q(self, question):
        return [self.BIG_SYS, {"role": "user", "content": question}]

    def test_excludes_system_prompt(self):
        from middleware.g05_cache import _semantic_query_text
        text = _semantic_query_text(self._q("How do I reset my password?"))
        assert "support assistant" not in text          # system prompt is gone
        assert "reset my password" in text

    def test_distinct_questions_same_system_differ(self):
        from middleware.g05_cache import _semantic_query_text, _normalise
        a = self._q("How do I reset my password?")
        b = self._q("Which regions are GDPR compliant?")
        # The fix: semantic text differs for different questions ...
        assert _semantic_query_text(a) != _semantic_query_text(b)
        # ... even though both normalise to a string dominated by the shared system prompt.
        assert _normalise(a)[:512] == _normalise(b)[:512]

    def test_same_question_matches(self):
        from middleware.g05_cache import _semantic_query_text
        a = self._q("How do I reset my password?")
        b = [{"role": "system", "content": "totally different system prompt"},
             {"role": "user", "content": "How do I reset my password?"}]
        # Same question → same semantic key (system prompt is intentionally ignored).
        assert _semantic_query_text(a) == _semantic_query_text(b)

    def test_concatenates_multiple_user_turns(self):
        from middleware.g05_cache import _semantic_query_text
        msgs = [self.BIG_SYS,
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"}]
        text = _semantic_query_text(msgs)
        assert "first" in text and "second" in text and "ok" not in text

    def test_no_user_text_embeds_nothing(self):
        """No user text → "" (L2 skipped), never the whole transcript: the system prompt
        and the repr of the parts would embed near-identically across requests."""
        from middleware.g05_cache import _semantic_query_text
        msgs = [self.BIG_SYS, {"role": "assistant", "content": "hello"}]
        assert _semantic_query_text(msgs) == ""


class TestSemanticCacheDisabled:
    """Fuzzy L2/L3 semantic serving must be skipped for stateful multi-turn
    continuations — their answer depends on conversation state, so a near-match
    can return another turn's response (e.g. a stale tool plan). L1 exact-match
    is unaffected. Single-turn Q&A still uses the semantic cache."""

    SKIP_CFG = {"groups": {"G5_cache": {"semantic_skip_multiturn": True}}}
    OFF_CFG = {"groups": {"G5_cache": {"semantic_skip_multiturn": False}}}

    def test_single_turn_uses_semantic_cache(self, make_ctx):
        from middleware.g05_cache import _semantic_cache_disabled
        ctx = make_ctx(
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "capital of France?"}],
            config=self.SKIP_CFG,
        )
        assert _semantic_cache_disabled(ctx) is False

    def test_multiturn_with_assistant_turn_skips_semantic(self, make_ctx):
        from middleware.g05_cache import _semantic_cache_disabled
        ctx = make_ctx(
            messages=[
                {"role": "system", "content": "s"},
                {"role": "user", "content": "fetch the logs"},
                {"role": "assistant", "content": "here are the logs"},
                {"role": "user", "content": "now get the user profile"},
            ],
            config=self.SKIP_CFG,
        )
        assert _semantic_cache_disabled(ctx) is True

    def test_tool_turn_skips_semantic(self, make_ctx):
        from middleware.g05_cache import _semantic_cache_disabled
        ctx = make_ctx(
            messages=[
                {"role": "user", "content": "deploy it"},
                {"role": "tool", "content": "{\"status\": \"ok\"}"},
                {"role": "user", "content": "now roll back"},
            ],
            config=self.SKIP_CFG,
        )
        assert _semantic_cache_disabled(ctx) is True

    def test_explicit_opt_out_always_disables(self, make_ctx):
        from middleware.g05_cache import _semantic_cache_disabled
        ctx = make_ctx(
            messages=[{"role": "user", "content": "q"}],
            params={"x_cache_semantic": "false"},
            config=self.SKIP_CFG,
        )
        assert _semantic_cache_disabled(ctx) is True

    def test_config_can_disable_the_multiturn_guard(self, make_ctx):
        # When semantic_skip_multiturn is off, a multi-turn request is NOT skipped.
        from middleware.g05_cache import _semantic_cache_disabled
        ctx = make_ctx(
            messages=[
                {"role": "user", "content": "fetch the logs"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "get the profile"},
            ],
            config=self.OFF_CFG,
        )
        assert _semantic_cache_disabled(ctx) is False

    def test_multiturn_default_on_without_config(self, make_ctx):
        # Guard defaults ON when the config key is absent.
        from middleware.g05_cache import _semantic_cache_disabled
        ctx = make_ctx(
            messages=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
            ],
            config={"groups": {"G5_cache": {}}},
        )
        assert _semantic_cache_disabled(ctx) is True


class TestG05CacheScope:
    """Durable contract for cache_scope (tenant | tenant+model). Locks two guarantees
    against future changes: (1) default "tenant" keeps L1 keys byte-identical to
    content-only keying (no cache invalidation; answers reused across providers); and
    (2) "tenant+model" isolates the L1 key per *requested* model."""

    @staticmethod
    def _scope(ctx, scope):
        ctx.config["groups"]["G5_cache"]["cache_scope"] = scope

    def test_resolve_defaults_to_tenant(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        assert _resolve_cache_scope(make_ctx()) == "tenant"

    def test_resolve_global_override(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx(); self._scope(ctx, "tenant+model")
        assert _resolve_cache_scope(ctx) == "tenant+model"

    def test_resolve_unknown_value_is_tenant(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx(); self._scope(ctx, "banana")
        assert _resolve_cache_scope(ctx) == "tenant"

    def test_resolve_per_tenant_override_wins(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx()
        ctx.tenant_id = "acme"
        ctx.config["tenants"] = {"acme": {"groups": {"G5_cache": {"cache_scope": "tenant+model"}}}}
        assert _resolve_cache_scope(ctx) == "tenant+model"  # global stays default tenant

    @pytest.mark.parametrize("tenants", [
        None, "off", {"acme": None}, {"acme": "off"}, {"acme": {"groups": "x"}},
        {"acme": {"groups": {"G5_cache": "off"}}},
    ])
    def test_a_malformed_tenants_node_falls_back_to_the_global_scope(self, make_ctx, tenants):
        """Operator YAML: `tenants:` with no children, or a block that is not a mapping, used
        to raise AttributeError on every lookup and store for every tenant."""
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx(); self._scope(ctx, "tenant+model")
        ctx.tenant_id = "acme"
        ctx.config["tenants"] = tenants
        try:
            scope = _resolve_cache_scope(ctx)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"a malformed tenants node must not raise: {exc!r}")
        assert scope == "tenant+model"

    def test_model_tag_empty_in_tenant_scope(self, make_ctx):
        from middleware.g05_cache import _model_scope_tag
        assert _model_scope_tag(make_ctx(model="gpt-4o")) == ""

    def test_model_tag_is_requested_model_in_tenant_model_scope(self, make_ctx):
        from middleware.g05_cache import _model_scope_tag
        ctx = make_ctx(model="gpt-4o"); self._scope(ctx, "tenant+model")
        assert _model_scope_tag(ctx) == "gpt-4o"

    def test_tenant_scope_shares_key_across_models(self, make_ctx):
        from middleware.g05_cache import _normalise, _cache_key, _apply_model_scope
        msgs = [{"role": "user", "content": "same question"}]
        a = make_ctx(messages=msgs, model="gpt-4o")
        b = make_ctx(messages=msgs, model="claude-3.5-sonnet")
        norm = _normalise(msgs)
        assert _cache_key(_apply_model_scope(norm, a)) == _cache_key(_apply_model_scope(norm, b))

    def test_tenant_scope_key_unchanged_vs_content_only(self, make_ctx):
        """Backward-compat: default scope must not alter the key (no invalidation)."""
        from middleware.g05_cache import _normalise, _cache_key, _apply_model_scope
        msgs = [{"role": "user", "content": "hello"}]
        ctx = make_ctx(messages=msgs, model="gpt-4o")
        norm = _normalise(msgs)
        assert _cache_key(_apply_model_scope(norm, ctx)) == _cache_key(norm)

    def test_tenant_model_scope_isolates_key_across_models(self, make_ctx):
        from middleware.g05_cache import _normalise, _cache_key, _apply_model_scope
        msgs = [{"role": "user", "content": "same question"}]
        a = make_ctx(messages=msgs, model="gpt-4o"); self._scope(a, "tenant+model")
        b = make_ctx(messages=msgs, model="claude-3.5-sonnet"); self._scope(b, "tenant+model")
        norm = _normalise(msgs)
        assert _cache_key(_apply_model_scope(norm, a)) != _cache_key(_apply_model_scope(norm, b))


class TestG05SystemPromptScope:
    """Durable contract for the "+system" cache-scope component.

    Regression lock for the DS8 finding (pitch-test-plan, 2026-07-20): the L2
    semantic key embeds USER TURNS ONLY (a long system prompt would dominate and
    truncate the embedding window), so the key was blind to the system prompt. A
    request whose system prompt scoped the assistant to one domain was served a
    cached answer produced under a different prompt — the baseline correctly
    declined off-topic geography questions while the optimised arm returned cached
    "Rome." / "Cairo" at 0.95-0.96 similarity. Fingerprinting the system prompt into
    the KEY (not the embedding) fixes it without reintroducing truncation."""

    @staticmethod
    def _scope(ctx, scope):
        ctx.config["groups"]["G5_cache"]["cache_scope"] = scope

    SYS_STRICT = {"role": "system", "content": "Only answer Northwind Cloud Services questions."}
    SYS_LAX = {"role": "system", "content": "You are a helpful general assistant."}
    USER = {"role": "user", "content": "What is the capital of Egypt?"}

    # ── scope resolution ────────────────────────────────────────────────────
    def test_resolve_tenant_system(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx(); self._scope(ctx, "tenant+system")
        assert _resolve_cache_scope(ctx) == "tenant+system"

    def test_resolve_tenant_model_system_is_compositional(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx(); self._scope(ctx, "tenant+model+system")
        assert _resolve_cache_scope(ctx) == "tenant+model+system"

    def test_resolve_reversed_spelling_normalises(self, make_ctx):
        from middleware.g05_cache import _resolve_cache_scope
        ctx = make_ctx(); self._scope(ctx, "tenant+system+model")
        assert _resolve_cache_scope(ctx) == "tenant+model+system"

    def test_resolve_legacy_spellings_unchanged(self, make_ctx):
        """The original rollout accepted tenant_model / model — must keep resolving."""
        from middleware.g05_cache import _resolve_cache_scope
        for legacy in ("tenant_model", "model"):
            ctx = make_ctx(); self._scope(ctx, legacy)
            assert _resolve_cache_scope(ctx) == "tenant+model", legacy

    def test_typo_fails_closed_to_tenant_and_warns(self, make_ctx, caplog):
        """A misspelled scope must NOT silently activate or drop isolation — it
        fails closed to "tenant" and logs a warning naming the valid values, so
        an operator who thinks they closed the DS8 hole finds out they didn't."""
        import logging
        from middleware.g05_cache import _resolve_cache_scope, _warned_cache_scopes
        _warned_cache_scopes.discard("tenant+sytem")     # warn-once: reset for test
        ctx = make_ctx(); self._scope(ctx, "tenant+sytem")   # typo
        with caplog.at_level(logging.WARNING, logger="middleware.g05_cache"):
            assert _resolve_cache_scope(ctx) == "tenant"
        assert any("unrecognised cache_scope" in r.message for r in caplog.records)

    def test_substring_lookalikes_do_not_activate_scopes(self, make_ctx):
        """Values merely CONTAINING "model"/"system" (e.g. "ecosystem") must not
        switch scoping on — that would be a silent full cache invalidation."""
        from middleware.g05_cache import _resolve_cache_scope
        for garbage in ("ecosystem", "supermodel", "no-model", "system: disabled"):
            ctx = make_ctx(); self._scope(ctx, garbage)
            assert _resolve_cache_scope(ctx) == "tenant", garbage

    def test_model_tag_still_applies_alongside_system(self, make_ctx):
        """Adding "+system" must not disable the pre-existing "+model" component."""
        from middleware.g05_cache import _model_scope_tag
        ctx = make_ctx(model="gpt-4o"); self._scope(ctx, "tenant+model+system")
        assert _model_scope_tag(ctx) == "gpt-4o"

    # ── tag behaviour ───────────────────────────────────────────────────────
    def test_system_tag_empty_by_default(self, make_ctx):
        """Default scope → no tag → keys byte-identical to pre-feature."""
        from middleware.g05_cache import _system_scope_tag
        ctx = make_ctx(messages=[self.SYS_STRICT, self.USER])
        assert _system_scope_tag(ctx) == ""

    def test_system_tag_differs_for_different_system_prompts(self, make_ctx):
        from middleware.g05_cache import _system_scope_tag
        a = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(a, "tenant+system")
        b = make_ctx(messages=[self.SYS_LAX, self.USER]); self._scope(b, "tenant+system")
        assert _system_scope_tag(a) != _system_scope_tag(b)

    def test_system_tag_stable_for_identical_prompt(self, make_ctx):
        from middleware.g05_cache import _system_scope_tag
        a = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(a, "tenant+system")
        b = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(b, "tenant+system")
        assert _system_scope_tag(a) == _system_scope_tag(b)

    def test_system_tag_ignores_cosmetic_whitespace(self, make_ctx):
        """Reformatting a prompt must not needlessly split the cache."""
        from middleware.g05_cache import _system_scope_tag
        a = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(a, "tenant+system")
        spaced = {"role": "system", "content": "Only  answer\n\nNorthwind Cloud Services   questions."}
        b = make_ctx(messages=[spaced, self.USER]); self._scope(b, "tenant+system")
        assert _system_scope_tag(a) == _system_scope_tag(b)

    def test_absent_system_prompt_is_its_own_bucket(self, make_ctx):
        """No-system-prompt must not be served an answer cached under one."""
        from middleware.g05_cache import _system_scope_tag
        a = make_ctx(messages=[self.USER]); self._scope(a, "tenant+system")
        b = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(b, "tenant+system")
        assert _system_scope_tag(a) != _system_scope_tag(b)

    def test_system_tag_handles_multimodal_content(self, make_ctx):
        from middleware.g05_cache import _system_scope_tag
        multimodal = {"role": "system", "content": [{"type": "text", "text": "Only answer Northwind Cloud Services questions."}]}
        a = make_ctx(messages=[multimodal, self.USER]); self._scope(a, "tenant+system")
        b = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(b, "tenant+system")
        assert _system_scope_tag(a) == _system_scope_tag(b)  # same text, either shape

    # ── the actual DS8 regression, on both cache layers ─────────────────────
    def test_L1_key_isolates_across_system_prompts(self, make_ctx):
        """THE DS8 REGRESSION (L1): same user question, different system prompts →
        different keys, so the strict prompt can't be served the lax prompt's answer."""
        from middleware.g05_cache import _normalise, _cache_key, _apply_model_scope
        msgs_a = [self.SYS_STRICT, self.USER]
        msgs_b = [self.SYS_LAX, self.USER]
        a = make_ctx(messages=msgs_a); self._scope(a, "tenant+system")
        b = make_ctx(messages=msgs_b); self._scope(b, "tenant+system")
        ka = _cache_key(_apply_model_scope(_normalise(msgs_a), a))
        kb = _cache_key(_apply_model_scope(_normalise(msgs_b), b))
        assert ka != kb

    def test_L2_scope_value_isolates_across_system_prompts(self, make_ctx):
        """THE DS8 REGRESSION (L2): _scope_value feeds the L2 WHERE filter, which is
        the layer that actually served "Rome."/"Cairo". User turns embed identically,
        so the scope value is the ONLY thing keeping them apart."""
        from middleware.g05_cache import _scope_value
        a = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(a, "tenant+system")
        b = make_ctx(messages=[self.SYS_LAX, self.USER]); self._scope(b, "tenant+system")
        assert _scope_value(a) != _scope_value(b)

    def test_default_scope_still_collides_documenting_opt_in(self, make_ctx):
        """Documents that the fix is OPT-IN: under the default scope the keys still
        match (pre-feature behaviour preserved, no cache invalidation on upgrade).
        Flip cache_scope to tenant+system to get the isolation."""
        from middleware.g05_cache import _scope_value
        a = make_ctx(messages=[self.SYS_STRICT, self.USER])
        b = make_ctx(messages=[self.SYS_LAX, self.USER])
        assert _scope_value(a) == _scope_value(b) == ""

    def test_lookup_and_store_read_identical_tag(self, make_ctx):
        """L1 and L2 must never disagree within a request — the tag is memoised on
        ctx.params, so a second read returns the same value."""
        from middleware.g05_cache import _system_scope_tag
        ctx = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(ctx, "tenant+system")
        first = _system_scope_tag(ctx)
        ctx.messages = [self.SYS_LAX, self.USER]      # mutate after first read
        assert _system_scope_tag(ctx) == first        # memoised → store matches lookup

    # (The two `_l3_query` scope tests that sat here were removed 2026-09-07 with the L3
    #  tier. They were explicitly labelled future-proofing for "the moment the L3 rewrite
    #  lands" — that rewrite will not land: L3 never executed and its library is gone.
    #  The scope contract they were protecting is asserted above for L1 and L2, which are
    #  the tiers that actually run.)


# ── M2: L2 embedding-window truncation guard ─────────────────────────────────
class TestG05L2EmbedWindowGuard:
    def test_helper_flags_over_window(self):
        from middleware.g05_cache import _embed_input_truncates
        assert _embed_input_truncates("a" * 3000, {}) is True
        assert _embed_input_truncates("short query", {}) is False

    def test_helper_respects_config_and_disable(self):
        from middleware.g05_cache import _embed_input_truncates
        assert _embed_input_truncates("a" * 100, {"l2_max_embed_chars": 50}) is True
        assert _embed_input_truncates("a" * 3000, {"l2_max_embed_chars": 0}) is False  # disabled


@pytest.mark.asyncio
class TestG05L2EmbedWindowSkip:
    async def test_l2_lookup_skips_over_window_query(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "a" * 3000}])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock) as mock_embed:
                from middleware.g05_cache import _l2_lookup
                result = await _l2_lookup(ctx, 0.85)
        assert result == (None, 0.0)
        mock_embed.assert_not_awaited()  # short-circuits before embedding/DB

    async def test_l2_store_skips_over_window_query(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "a" * 3000}])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock) as mock_embed:
                from middleware.g05_cache import _l2_store
                await _l2_store(ctx, {"choices": []}, 3600)
        mock_embed.assert_not_awaited()

    async def test_two_long_queries_do_not_collide(self, make_ctx):
        # Distinct long queries sharing a long prefix both skip L2 → neither can
        # be served the other's cached answer (the truncation-collision bug).
        shared = "a" * 2500
        ctx1 = make_ctx([{"role": "user", "content": shared + " first question"}])
        ctx2 = make_ctx([{"role": "user", "content": shared + " second question"}])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}):
            with patch("middleware.g05_cache._embed", new_callable=AsyncMock) as mock_embed:
                from middleware.g05_cache import _l2_lookup
                assert await _l2_lookup(ctx1, 0.85) == (None, 0.0)
                assert await _l2_lookup(ctx2, 0.85) == (None, 0.0)
        mock_embed.assert_not_awaited()


# ── Shared helpers for the tests below ───────────────────────────────────────
def _image_turn(url):
    return {"role": "user", "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": url}},
    ]}


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _l2_pool(execute_result="OK"):
    """A pool whose single connection records its fetchrow/execute calls."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=execute_result)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


def _sql_calls(conn, marker):
    return [c for c in conn.execute.await_args_list if marker in str(c.args[0])]


class _L2Env:
    """DATABASE_URL set, the embedding and the schema step stubbed, the pool given."""

    def __init__(self, pool):
        self._patches = [
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}),
            patch("middleware.g05_cache._embed", new_callable=AsyncMock, return_value=[0.1, 0.2]),
            patch("middleware.g05_cache._ensure_cache_l2_schema", new_callable=AsyncMock),
            patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=pool),
        ]

    def __enter__(self):
        entered = [p.__enter__() for p in self._patches]
        return entered[1]          # the _embed mock

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


# ── L2 matches on user TEXT only (multimodal requests skip it) ───────────────
class TestSemanticTextIsTextOnly:
    """A user turn that carries anything but text gives the semantic tier nothing safe to
    match on: two different images under the same words would embed alike, and one
    caller would get the other image's description."""

    @pytest.mark.parametrize("part", [
        {"type": "image_url", "image_url": {"url": "https://cdn.example.com/p/1001.png"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        {"type": "file", "file": {"file_id": "file-1"}},
        {"type": "text"},                        # malformed: no text
        "a bare string part",
    ])
    def test_a_non_text_part_embeds_nothing(self, part):
        from middleware.g05_cache import _semantic_query_text
        msgs = [{"role": "user", "content": [{"type": "text", "text": "Describe this"}, part]}]
        assert _semantic_query_text(msgs) == ""

    def test_a_user_turn_without_text_content_embeds_nothing(self):
        from middleware.g05_cache import _semantic_query_text
        assert _semantic_query_text([{"role": "user", "content": None}]) == ""
        assert _semantic_query_text([{"role": "user"}]) == ""

    @pytest.mark.parametrize("content", [
        None, {"type": "image_url", "image_url": {"url": "https://x/1.png"}}, 42])
    def test_such_a_turn_beside_a_text_turn_still_embeds_nothing(self, content):
        """Refused, not skipped: the other turn's words alone would match a request that
        never had this part."""
        from middleware.g05_cache import _semantic_query_text
        msgs = [{"role": "user", "content": "Describe this"}, {"role": "user", "content": content}]
        assert _semantic_query_text(msgs) == ""

    def test_one_image_turn_among_text_turns_embeds_nothing(self):
        from middleware.g05_cache import _semantic_query_text
        msgs = [{"role": "user", "content": "hello"}, _image_turn("https://x/1.png")]
        assert _semantic_query_text(msgs) == ""

    def test_text_only_parts_embed_like_a_string(self):
        from middleware.g05_cache import _semantic_query_text
        parts = [{"role": "user", "content": [{"type": "text", "text": "Capital of"},
                                              {"type": "text", "text": "France?"}]}]
        plain = [{"role": "user", "content": "Capital of France?"}]
        assert _semantic_query_text(parts) == _semantic_query_text(plain) == "capital of france?"


@pytest.mark.asyncio
class TestMultimodalSkipsL2:
    async def test_two_images_never_reach_the_embedding_or_the_table(self, make_ctx):
        from middleware.g05_cache import _l2_lookup, _l2_store
        pool, conn = _l2_pool()
        with _L2Env(pool) as embed:
            for url in ("https://cdn.example.com/p/1001.png", "https://cdn.example.com/p/1002.png"):
                ctx = make_ctx([{"role": "system", "content": "s"}, _image_turn(url)])
                assert await _l2_lookup(ctx, 0.9) == (None, 0.0)
                await _l2_store(ctx, {"choices": [{"message": {"content": "a cat"}}]}, 3600)
        embed.assert_not_awaited()
        pool.acquire.assert_not_called()

    @pytest.mark.parametrize("original", [
        [_image_turn("https://x/1.png")],
        [{"role": "user", "content": "fetch the logs"},
         {"role": "assistant", "content": "done"},
         {"role": "user", "content": "now the profile"}],
    ], ids=["image-replaced-by-text", "history-compacted"])
    async def test_the_store_decides_on_the_request_as_sent(self, make_ctx, original):
        """A later stage replaces an image with text (G27) or compacts a multi-turn history
        (G26, G10). The store must skip L2 as its lookup did, not file a single-turn text
        question that a later text-only request would match."""
        from middleware.g05_cache import _l2_store
        pool, conn = _l2_pool()
        ctx = make_ctx(original)
        ctx.messages = [{"role": "user", "content": "describe a picture of a cat"}]
        with _L2Env(pool) as embed:
            await _l2_store(ctx, {"choices": [{"message": {"content": "x"}}]}, 3600)
        embed.assert_not_awaited()
        assert not _sql_calls(conn, "INSERT INTO cache_l2")

    async def test_the_lookup_embeds_the_request_as_sent(self, make_ctx):
        """Lookup and store read the same source, so no stage can make them disagree."""
        from middleware.g05_cache import _l2_lookup
        pool, conn = _l2_pool()
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        ctx.messages = [{"role": "user", "content": "capital France?"}]
        with _L2Env(pool) as embed:
            await _l2_lookup(ctx, 0.9)
        assert embed.await_args.args[0] == "what is the capital of france?"

    async def test_the_store_embeds_the_text_the_lookup_embedded(self, make_ctx):
        from middleware.g05_cache import _l2_lookup, _l2_store
        pool, conn = _l2_pool()
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        with _L2Env(pool) as embed:
            await _l2_lookup(ctx, 0.9)
            ctx.messages = [{"role": "user", "content": "capital France?"}]   # compressed later
            await _l2_store(ctx, {"choices": [{"message": {"content": "Paris"}}]}, 3600)
        assert [c.args[0] for c in embed.await_args_list] == ["what is the capital of france?"] * 2


# ── Expired L2 rows are never served, and the store purges them ──────────────
@pytest.mark.asyncio
class TestExpiredL2Rows:
    """l2_ttl_seconds is enforced on read, and expired rows are deleted with the retention
    job off (the default). The SQL itself runs against a real Postgres in
    tests/integration/test_g05_l2_pg.py."""

    async def test_the_lookup_filters_on_expiry(self, make_ctx):
        from middleware.g05_cache import _l2_lookup
        pool, conn = _l2_pool()
        with _L2Env(pool):
            await _l2_lookup(make_ctx(), 0.9)
        assert "AND expires_at > NOW()" in conn.fetchrow.await_args.args[0]

    async def test_the_store_purges_this_tenants_expired_rows_once_per_interval(
            self, make_ctx, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_l2_next_purge", {})
        pool, conn = _l2_pool(execute_result="DELETE 3")
        ctx = make_ctx()
        ctx.tenant_id = "acme"
        with _L2Env(pool):
            await g05._l2_store(ctx, {"choices": []}, 3600)
            await g05._l2_store(ctx, {"choices": []}, 3600)
        assert len(_sql_calls(conn, "INSERT INTO cache_l2")) == 2
        purges = _sql_calls(conn, "DELETE FROM cache_l2")
        assert len(purges) == 1                     # the second store is inside the interval
        assert purges[0].args[1:] == ("acme", g05._L2_PURGE_BATCH)
        assert "expires_at <= NOW()" in purges[0].args[0]

    async def test_a_full_batch_purges_again_on_the_next_store(self, make_ctx, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_l2_next_purge", {})
        pool, conn = _l2_pool(execute_result=f"DELETE {g05._L2_PURGE_BATCH}")
        with _L2Env(pool):
            for _ in range(3):
                await g05._l2_store(make_ctx(), {"choices": []}, 3600)
        assert len(_sql_calls(conn, "DELETE FROM cache_l2")) == 3

    async def test_each_tenant_has_its_own_interval(self, make_ctx, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_l2_next_purge", {})
        pool, conn = _l2_pool(execute_result="DELETE 0")
        with _L2Env(pool):
            for tenant in ("acme", "globex", "acme"):
                ctx = make_ctx()
                ctx.tenant_id = tenant
                await g05._l2_store(ctx, {"choices": []}, 3600)
        assert [c.args[1] for c in _sql_calls(conn, "DELETE FROM cache_l2")] == ["acme", "globex"]

    async def test_a_failed_purge_leaves_the_stored_row(self, make_ctx, monkeypatch, caplog):
        import logging
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_l2_next_purge", {})
        pool, conn = _l2_pool()

        async def execute(sql, *args):
            if "DELETE FROM cache_l2" in sql:
                raise RuntimeError("permission denied for table cache_l2")
            return "OK"

        conn.execute = AsyncMock(side_effect=execute)
        with _L2Env(pool), caplog.at_level(logging.WARNING, logger="middleware.g05_cache"):
            await g05._l2_store(make_ctx(), {"choices": []}, 3600)
        assert len(_sql_calls(conn, "INSERT INTO cache_l2")) == 1
        assert any("purge failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestL2VectorIndex:
    """The lookup takes the NEAREST row by distance, which an HNSW index on embedding can
    serve, and checks the threshold on that row only; the schema step builds the index in
    the background. Without the index every lookup scanned all of the tenant's rows. The SQL
    runs against a real Postgres in tests/integration/test_g05_l2_pg.py."""

    async def test_the_lookup_orders_by_distance_and_filters_nothing_on_it(self, make_ctx):
        from middleware.g05_cache import _l2_lookup
        pool, conn = _l2_pool()
        with _L2Env(pool):
            await _l2_lookup(make_ctx(), 0.9)
        sql, threshold = conn.fetchrow.await_args.args[0], conn.fetchrow.await_args.args[2]
        where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
        assert "ORDER BY embedding <=> $1::vector" in sql and "LIMIT 1" in sql
        assert "<=>" not in where      # a distance filter would keep an index scan going
        assert ">= $2 AS close_enough" in sql and threshold == 0.9

    @pytest.mark.parametrize("close_enough,expected", [
        (True, ({"a": 1}, 0.95)), (False, (None, 0.0))])
    async def test_only_a_nearest_row_within_the_threshold_is_served(self, make_ctx,
                                                                      close_enough, expected):
        from middleware.g05_cache import _l2_lookup
        pool, conn = _l2_pool()
        conn.fetchrow = AsyncMock(return_value={"response_json": '{"a": 1}', "similarity": 0.95,
                                                "close_enough": close_enough})
        with _L2Env(pool):
            assert await _l2_lookup(make_ctx(), 0.9) == expected

    async def test_the_lookup_asks_for_an_iterative_index_scan(self, make_ctx, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_iterative_scan", True)
        pool, conn = _l2_pool()
        with _L2Env(pool):
            await g05._l2_lookup(make_ctx(), 0.9)
        assert _sql_calls(conn, "SET hnsw.iterative_scan = strict_order")

    async def test_a_pgvector_without_iterative_scans_is_asked_once(self, make_ctx, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_iterative_scan", True)
        pool, conn = _l2_pool()

        async def execute(sql, *args):
            if "hnsw.iterative_scan" in sql:
                raise RuntimeError('unrecognized configuration parameter "hnsw.iterative_scan"')
            return "OK"

        conn.execute = AsyncMock(side_effect=execute)
        with _L2Env(pool):
            await g05._l2_lookup(make_ctx(), 0.9)
            await g05._l2_lookup(make_ctx(), 0.9)
        assert len(_sql_calls(conn, "hnsw.iterative_scan")) == 1
        assert conn.fetchrow.await_count == 2            # both lookups still ran

    async def test_the_schema_step_starts_the_index_build_without_waiting(self, monkeypatch):
        import asyncio
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_cache_l2_schema_ready", False)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_build(pool):
            started.set()
            await release.wait()
            return True

        monkeypatch.setattr(g05, "_build_l2_vector_index", slow_build)
        pool, conn = _l2_pool()
        with patch("cache.pg_pool.may_run_ddl", new_callable=AsyncMock, return_value=True):
            await g05._ensure_cache_l2_schema(pool)        # returns; the build is still running
        await asyncio.wait_for(started.wait(), 1)
        assert len(g05._l2_index_tasks) == 1               # held until it finishes
        release.set()
        await asyncio.gather(*g05._l2_index_tasks)
        assert not g05._l2_index_tasks

    async def test_the_build_replaces_an_index_an_earlier_build_left_invalid(self):
        import middleware.g05_cache as g05
        pool, conn = _l2_pool()
        conn.fetchval = AsyncMock(return_value=False)      # CONCURRENTLY died part-way
        assert await g05._build_l2_vector_index(pool) is True
        sqls = [str(c.args[0]) for c in conn.execute.await_args_list]
        assert sqls[0] == "DROP INDEX CONCURRENTLY IF EXISTS idx_cache_l2_embedding"
        assert sqls[1].startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cache_l2_embedding")
        assert "USING hnsw (embedding vector_cosine_ops)" in sqls[1]

    async def test_a_valid_index_is_left_alone(self):
        import middleware.g05_cache as g05
        pool, conn = _l2_pool()
        conn.fetchval = AsyncMock(return_value=True)
        assert await g05._build_l2_vector_index(pool) is True
        assert conn.execute.await_count == 0

    async def test_a_build_that_lost_a_race_to_another_instance_is_ready(self):
        import middleware.g05_cache as g05
        pool, conn = _l2_pool()
        conn.fetchval = AsyncMock(side_effect=[None, True])    # missing, then built elsewhere
        conn.execute = AsyncMock(side_effect=RuntimeError('relation "idx_cache_l2_embedding" '
                                                          'already exists'))
        assert await g05._build_l2_vector_index(pool) is True

    async def test_a_failed_build_is_logged_not_raised(self, caplog):
        import logging
        import middleware.g05_cache as g05
        pool, conn = _l2_pool()
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(side_effect=RuntimeError('access method "hnsw" does not exist'))
        with caplog.at_level(logging.WARNING, logger="middleware.g05_cache"):
            assert await g05._build_l2_vector_index(pool) is False
        assert "scans the tenant's rows" in caplog.text


# ── The cache key covers the parameters that change the answer ───────────────
_ANSWER_VARIANTS = [
    ("response_format", {"type": "json_schema", "json_schema": {"name": "a", "schema": {}}},
                        {"type": "json_schema", "json_schema": {"name": "b", "schema": {}}}),
    ("tools", [_tool("search")], [_tool("delete_all")]),
    ("tool_choice", "auto", "none"),
    ("n", 1, 3),
    ("max_tokens", 50, 1000),
    ("temperature", 0, 0.9),
    ("stop", ["\n"], ["END"]),
    ("logprobs", False, True),
    ("json_schema", {"type": "object"}, {"type": "array"}),
    ("x_rag_collection", "handbook", "contracts"),
    ("session_id", "s-1", "s-2"),
]


class TestAnswerParamsInTheKey:
    MSGS = [{"role": "user", "content": "List three colours"}]

    def _key(self, ctx):
        from middleware.g05_cache import _normalise, _cache_key, _apply_model_scope
        return _cache_key(_apply_model_scope(_normalise(self.MSGS), ctx))

    @pytest.mark.parametrize("name,a,b", _ANSWER_VARIANTS, ids=[v[0] for v in _ANSWER_VARIANTS])
    def test_different_values_get_different_keys_and_scopes(self, make_ctx, name, a, b):
        from middleware.g05_cache import _scope_value
        ca = make_ctx(messages=self.MSGS, params={name: a})
        cb = make_ctx(messages=self.MSGS, params={name: b})
        unset = make_ctx(messages=self.MSGS)
        assert len({self._key(ca), self._key(cb), self._key(unset)}) == 3
        assert len({_scope_value(ca), _scope_value(cb), _scope_value(unset)}) == 3

    def test_neutral_fields_do_not_split_the_cache(self, make_ctx):
        from middleware.g05_cache import _scope_value
        base = make_ctx(messages=self.MSGS, params={"temperature": 0})
        other = make_ctx(messages=self.MSGS, params={
            "temperature": 0, "stream": True, "stream_options": {"include_usage": True},
            "user": "u-9", "workflow_id": "wf-1", "x_team": "t", "x_no_cache": "false"})
        assert self._key(base) == self._key(other)
        assert _scope_value(base) == _scope_value(other)

    def test_a_request_without_answer_params_keeps_its_key(self, make_ctx):
        from middleware.g05_cache import _normalise, _apply_model_scope, _scope_value
        ctx = make_ctx(messages=self.MSGS, params={"stream": True, "user": "u-1"})
        assert _apply_model_scope(_normalise(self.MSGS), ctx) == _normalise(self.MSGS)
        assert _scope_value(ctx) == ""

    def test_key_order_and_null_values_do_not_matter(self, make_ctx):
        from middleware.g05_cache import _params_scope_tag
        a = make_ctx(params={"temperature": 0, "response_format": {
            "type": "json_schema", "json_schema": {"name": "x", "strict": True}}})
        b = make_ctx(params={"seed": None, "response_format": {
            "json_schema": {"strict": True, "name": "x"}, "type": "json_schema"}, "temperature": 0})
        assert _params_scope_tag(a) == _params_scope_tag(b) != ""

    def test_the_tag_is_fixed_at_the_first_read(self, make_ctx):
        from middleware.g05_cache import _params_scope_tag
        ctx = make_ctx(params={"tools": [_tool("search"), _tool("fetch")], "max_tokens": 1000})
        first = _params_scope_tag(ctx)
        ctx.params["tools"] = [_tool("search")]      # G08 keeps the relevant tool
        ctx.params["max_tokens"] = 200               # G11 tightens the budget
        assert _params_scope_tag(ctx) == first


@pytest.mark.asyncio
class TestStoreFilesUnderTheLookupKey:
    """G08/G16 filter `tools`, G11 sets `max_tokens` and Stage 3 rewrites the messages
    between the lookup and the store. Both tiers must store under the key and scope the
    lookup used, or the answer lands where no identical request will look."""

    async def test_both_tiers_store_under_the_lookup_key(self, make_ctx, monkeypatch):
        import middleware.g05_cache as g05
        monkeypatch.setattr(g05, "_l2_next_purge", {})
        tools = [_tool("search"), _tool("fetch")]
        msgs = [{"role": "user", "content": "Find the invoice"}]
        ctx = make_ctx(msgs, params={"tools": tools, "max_tokens": 1000})
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        pool, conn = _l2_pool()
        with patch("middleware.g05_cache._get_redis", return_value=redis), _L2Env(pool):
            cache = g05.G05Cache()
            await cache.process_request(ctx)
            ctx.params["tools"] = tools[:1]
            ctx.params["max_tokens"] = 200
            ctx.messages = [{"role": "user", "content": "invoice?"}]
            await cache.store_response(ctx, {"choices": [{"message": {"content": "INV-1"}}]})
        lookup_scope = conn.fetchrow.await_args.args[4]
        assert redis.set.await_args.args[0] == redis.get.await_args.args[0]
        assert _sql_calls(conn, "INSERT INTO cache_l2")[0].args[6] == lookup_scope != ""
        # The rewrite would have mattered: the rewritten request has another scope.
        rewritten = make_ctx(msgs, params={"tools": tools[:1], "max_tokens": 200})
        assert g05._scope_value(rewritten) != lookup_scope

    async def test_a_store_without_a_lookup_key_recomputes_it_from_the_request_as_sent(
            self, make_ctx):
        import middleware.g05_cache as g05
        msgs = [{"role": "user", "content": "Find the invoice"}]
        ctx = make_ctx(msgs)
        ctx.messages = [{"role": "user", "content": "invoice?"}]     # rewritten by Stage 3
        redis = AsyncMock()
        with patch("middleware.g05_cache._get_redis", return_value=redis), \
                patch("middleware.g05_cache._l2_store", new_callable=AsyncMock):
            await g05.G05Cache().store_response(ctx, {"choices": [{"message": {"content": "x"}}]})
        assert redis.set.await_args.args[0] == g05._cache_key(
            g05._apply_model_scope(g05._normalise(msgs), make_ctx(msgs)))

    async def test_the_key_is_fixed_even_when_redis_is_down_at_lookup(self, make_ctx):
        import middleware.g05_cache as g05
        ctx = make_ctx(params={"max_tokens": 1000})
        with patch("middleware.g05_cache._get_redis", side_effect=ConnectionError("down")), \
                patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock,
                      return_value=(None, 0.0)):
            await g05.G05Cache().process_request(ctx)
        assert "_g05_l1_cache_key" in ctx.params, "the key must be fixed before Redis is touched"
        assert ctx.params["_g05_l1_cache_key"] == g05._cache_key(g05._apply_model_scope(
            g05._normalise(ctx.messages), make_ctx(params={"max_tokens": 1000})))


# ── L1 keys on tool calls and the call a result answers ──────────────────────
class TestToolCallsInTheL1Key:
    @staticmethod
    def _transcript(args, calls=None, results=None):
        calls = calls or [("call_1", "search", args)]
        results = results or [("call_1", "[]")]
        return [
            {"role": "user", "content": "find the files"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": a}}
                for cid, name, a in calls]},
        ] + [{"role": "tool", "tool_call_id": cid, "content": out} for cid, out in results]

    def test_different_tool_arguments_get_different_keys(self):
        from middleware.g05_cache import _normalise
        assert _normalise(self._transcript('{"q": "invoices"}')) != \
            _normalise(self._transcript('{"q": "payroll"}'))

    def test_the_same_transcript_gets_the_same_key(self):
        from middleware.g05_cache import _normalise
        assert _normalise(self._transcript('{"q": "x"}')) == _normalise(self._transcript('{"q": "x"}'))

    def test_which_call_a_result_answers_is_part_of_the_key(self):
        from middleware.g05_cache import _normalise
        calls = [("call_1", "read", '{"f": "a"}'), ("call_2", "read", '{"f": "b"}')]
        one = self._transcript(None, calls, [("call_1", "alpha"), ("call_2", "beta")])
        two = self._transcript(None, calls, [("call_2", "alpha"), ("call_1", "beta")])
        assert _normalise(one) != _normalise(two)

    def test_arguments_keep_their_case(self):
        from middleware.g05_cache import _normalise
        assert _normalise(self._transcript('{"path": "/Data/A.txt"}')) != \
            _normalise(self._transcript('{"path": "/data/a.txt"}'))

    def test_legacy_function_call_and_name_are_keyed(self):
        from middleware.g05_cache import _normalise

        def legacy(name):
            return [{"role": "user", "content": "weather?"},
                    {"role": "assistant", "content": None,
                     "function_call": {"name": name, "arguments": "{}"}},
                    {"role": "function", "name": name, "content": "sunny"}]

        assert _normalise(legacy("get_weather")) != _normalise(legacy("get_forecast"))

    def test_a_plain_chat_keeps_its_old_key(self):
        from middleware.g05_cache import _normalise
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Hi  there"}]
        assert _normalise(msgs) == "system:s|user:hi there"


# ── Every request field is classified: in the key, or neutral ────────────────
class TestEveryParameterIsClassified:
    """A field that reaches ctx.params either changes the answer (so it is in the cache
    key) or is listed as neutral. A new one fails here until someone decides which."""

    def test_the_two_sets_do_not_overlap(self):
        from middleware.g05_cache import _ANSWER_PARAMS, _ANSWER_NEUTRAL_PARAMS
        assert not (_ANSWER_PARAMS & _ANSWER_NEUTRAL_PARAMS)

    def test_every_field_admitted_at_openai_ingress_is_classified(self):
        from protocols.base import OPENAI_CHAT_PARAMS, TOKENLEAN_CLIENT_PARAMS
        from middleware.g05_cache import _ANSWER_PARAMS, _ANSWER_NEUTRAL_PARAMS
        admitted = OPENAI_CHAT_PARAMS | TOKENLEAN_CLIENT_PARAMS
        assert not admitted - _ANSWER_PARAMS - _ANSWER_NEUTRAL_PARAMS

    def test_every_field_the_other_adapters_produce_is_classified(self):
        from protocols.anthropic_ingress import AnthropicProtocol
        from protocols.gemini_ingress import GeminiProtocol
        from middleware.g05_cache import _ANSWER_PARAMS, _ANSWER_NEUTRAL_PARAMS
        _, _, anthropic = AnthropicProtocol().parse_request({
            "model": "m", "max_tokens": 5, "temperature": 0, "top_p": 1, "top_k": 3,
            "stream": False, "metadata": {"user_id": "u"}, "stop_sequences": ["x"],
            "tools": [{"name": "t", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": "hi"}]})
        _, _, gemini = GeminiProtocol().parse_request({
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 5, "temperature": 0, "topP": 1,
                                 "stopSequences": ["x"]},
            "tools": [{"functionDeclarations": [{"name": "t", "parameters": {}}]}]},
            path_model="m")
        produced = set(anthropic) | set(gemini)
        assert {"tools", "max_tokens", "stop", "metadata"} <= produced   # the probe reached them
        assert not produced - _ANSWER_PARAMS - _ANSWER_NEUTRAL_PARAMS

    def test_every_x_field_the_proxy_reads_is_classified(self):
        import re
        from pathlib import Path
        from middleware.g05_cache import _ANSWER_PARAMS, _ANSWER_NEUTRAL_PARAMS
        src = Path(__file__).resolve().parents[3] / "src" / "proxy"
        names = set()
        for path in src.rglob("*.py"):
            names |= set(re.findall(r"[\"'](x_[a-z0-9_]+)[\"']",
                                    path.read_text(encoding="utf-8", errors="replace")))
        assert "x_rag_collection" in names                          # the scan found the readers
        assert not names - _ANSWER_PARAMS - _ANSWER_NEUTRAL_PARAMS


# ── Adaptive TTL: recent hit rate, bounded, and switchable ───────────────────
class _StatsRedis:
    """Async Redis fake with the string and hash commands G05 uses; records each command."""

    def __init__(self):
        self.strings, self.hashes, self.expiry, self.calls = {}, {}, {}, []

    async def get(self, key):
        self.calls.append("get")
        return self.strings.get(key)

    async def set(self, key, value, ex=None):
        self.calls.append("set")
        self.strings[key] = value
        self.expiry[key] = ex

    async def hincrby(self, key, field, amount=1):
        self.calls.append("hincrby")
        fields = self.hashes.setdefault(key, {})
        fields[field] = fields.get(field, 0) + amount
        return fields[field]

    async def expire(self, key, seconds):
        self.calls.append("expire")
        self.expiry[key] = seconds
        return True

    async def hget(self, key, field):
        self.calls.append("hget")
        value = self.hashes.get(key, {}).get(field)
        return None if value is None else str(value).encode()

    async def hmget(self, key, fields):
        self.calls.append("hmget")
        stored = self.hashes.get(key, {})
        return [None if f not in stored else str(stored[f]).encode() for f in fields]


class TestAutoTTL:
    """A tenant's cache TTLs follow its recent hit rate: above 80% they grow by a quarter and
    below 20% they shrink by a quarter, within auto_ttl_min/max_multiplier of the configured
    TTL. auto_ttl_enabled: false keeps the configured TTLs."""

    @pytest.fixture(autouse=True)
    def _clock(self, monkeypatch):
        import middleware.g05_cache as g05
        self.now = 1_000_000 * g05._TTL_STATS_WINDOW_S + 10
        monkeypatch.setattr(g05, "_clock", lambda: self.now)

    @staticmethod
    async def _record(redis, level, hits=0, misses=0, prefix=""):
        import middleware.g05_cache as g05
        with patch("middleware.g05_cache._get_redis", return_value=redis):
            manager = g05.G05Cache()._get_ttl_manager(prefix)
            for _ in range(hits):
                await manager.record_hit(level)
            for _ in range(misses):
                await manager.record_miss(level)

    @staticmethod
    async def _stored_ttls(ctx, redis):
        import middleware.g05_cache as g05
        with patch("middleware.g05_cache._get_redis", return_value=redis), \
                patch("middleware.g05_cache._l2_store", new_callable=AsyncMock) as l2_store:
            await g05.G05Cache().store_response(ctx, {"choices": [{"message": {"content": "x"}}]})
        (key,) = redis.strings
        return redis.expiry[key], l2_store.await_args.args[2]

    async def test_a_high_hit_rate_extends_both_ttls_by_a_quarter(self, make_ctx):
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20)
        await self._record(redis, "L2", hits=20)
        assert await self._stored_ttls(make_ctx(), redis) == (4500, 108000)

    async def test_each_tier_follows_its_own_hit_rate(self, make_ctx):
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20)
        await self._record(redis, "L2", misses=20)
        assert await self._stored_ttls(make_ctx(), redis) == (4500, 64800)

    async def test_another_tenants_hit_rate_does_not_count(self, make_ctx):
        ctx = make_ctx()
        ctx.redis_prefix = "t:b:"
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20, prefix="t:a:")
        assert (await self._stored_ttls(ctx, redis))[0] == 3600

    async def test_fewer_than_ten_lookups_keep_the_configured_ttl(self, make_ctx):
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=9)
        assert (await self._stored_ttls(make_ctx(), redis))[0] == 3600

    @pytest.mark.parametrize("hits, misses", [(8, 2), (2, 8)])
    async def test_exactly_80_or_20_percent_keeps_the_configured_ttl(self, make_ctx, hits,
                                                                      misses):
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=hits, misses=misses)
        assert (await self._stored_ttls(make_ctx(), redis))[0] == 3600

    async def test_the_max_multiplier_bounds_the_extension(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G5_cache"]["auto_ttl_max_multiplier"] = 1.1
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20)
        await self._record(redis, "L2", hits=20)
        assert await self._stored_ttls(ctx, redis) == (3960, 95040)

    async def test_the_min_multiplier_bounds_the_reduction(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G5_cache"]["auto_ttl_min_multiplier"] = 0.9
        redis = _StatsRedis()
        await self._record(redis, "L1", misses=20)
        await self._record(redis, "L2", misses=20)
        assert await self._stored_ttls(ctx, redis) == (3240, 77760)

    async def test_a_reduced_ttl_is_never_zero(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G5_cache"]["l1_ttl_seconds"] = 1
        redis = _StatsRedis()
        await self._record(redis, "L1", misses=20)
        assert (await self._stored_ttls(ctx, redis))[0] == 1

    async def test_disabled_keeps_the_configured_ttls_and_touches_no_stats(self, make_ctx):
        import middleware.g05_cache as g05
        ctx = make_ctx()
        ctx.config["groups"]["G5_cache"]["auto_ttl_enabled"] = False
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20)
        await self._record(redis, "L2", hits=20)
        redis.calls.clear()
        with patch("middleware.g05_cache._get_redis", return_value=redis), \
                patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock,
                      return_value=(None, 0.0)):
            await g05.G05Cache().process_request(ctx)
        assert await self._stored_ttls(ctx, redis) == (3600, 86400)
        assert set(redis.calls) == {"get", "set"}, redis.calls

    async def test_a_lookup_reads_no_stats(self, make_ctx):
        import middleware.g05_cache as g05
        redis = _StatsRedis()
        with patch("middleware.g05_cache._get_redis", return_value=redis), \
                patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock,
                      return_value=(None, 0.0)):
            await g05.G05Cache().process_request(make_ctx())
        assert "hincrby" in redis.calls                         # the misses were counted
        assert not {"hget", "hmget"} & set(redis.calls), redis.calls

    async def test_hits_older_than_the_previous_window_do_not_count(self, make_ctx):
        import middleware.g05_cache as g05
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20)
        self.now += 2 * g05._TTL_STATS_WINDOW_S
        await self._record(redis, "L1", misses=20)
        assert (await self._stored_ttls(make_ctx(), redis))[0] == 2700

    async def test_hits_in_the_previous_window_still_count(self, make_ctx):
        import middleware.g05_cache as g05
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=20)
        self.now += g05._TTL_STATS_WINDOW_S
        assert (await self._stored_ttls(make_ctx(), redis))[0] == 4500

    async def test_the_stats_expire_after_two_windows(self):
        import middleware.g05_cache as g05
        redis = _StatsRedis()
        await self._record(redis, "L1", hits=1, misses=1)
        assert redis.hashes
        assert all(redis.expiry.get(key) == 2 * g05._TTL_STATS_WINDOW_S for key in redis.hashes)
