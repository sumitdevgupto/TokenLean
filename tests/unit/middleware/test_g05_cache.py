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
        mock_pool, mock_conn = self._make_mock_pool()

        await g05._ensure_cache_l2_schema(mock_pool)

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
        mock_pool, mock_conn = self._make_mock_pool()

        await g05._ensure_cache_l2_schema(mock_pool)
        await g05._ensure_cache_l2_schema(mock_pool)  # guard flag → no-op

        assert mock_conn.execute.await_count == 5  # not 10
        assert mock_pool.acquire.call_count == 1


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

    def test_falls_back_when_no_user_turns(self):
        from middleware.g05_cache import _semantic_query_text, _normalise
        msgs = [self.BIG_SYS, {"role": "assistant", "content": "hello"}]
        assert _semantic_query_text(msgs) == _normalise(msgs)


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

    # ── L3 honours the same contract (future-proofing: L3 is currently inert) ──
    def test_L3_query_isolates_across_system_prompts(self, make_ctx):
        """The L3 semantic query must fold the scope tags like L1/L2 do — else a
        tenant+system operator is protected on L1/L2 but collides on L3 the moment
        the L3 rewrite lands."""
        from middleware.g05_cache import _l3_query
        a = make_ctx(messages=[self.SYS_STRICT, self.USER]); self._scope(a, "tenant+system")
        b = make_ctx(messages=[self.SYS_LAX, self.USER]); self._scope(b, "tenant+system")
        assert _l3_query(a) != _l3_query(b)

    def test_L3_query_unchanged_under_default_scope(self, make_ctx):
        """Default scope → L3 query byte-identical to the pre-feature user-turns
        text (no invalidation of any existing L3 store)."""
        from middleware.g05_cache import _l3_query, _semantic_query_text
        ctx = make_ctx(messages=[self.SYS_STRICT, self.USER])
        assert _l3_query(ctx) == _semantic_query_text(ctx.messages)


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
