"""
Integration tests for OptimisationPipeline — full G4→…→G17 + response path.
All external I/O (Redis, Qdrant, httpx, Langfuse, litellm) is mocked.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


_LLM_RESPONSE = {
    "id": "chatcmpl-integration",
    "choices": [{"message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
}


def _patch_all_external():
    """Context managers that mock every external service the pipeline touches."""
    return [
        # Redis (G05, G10, G17)
        patch("middleware.g05_cache._get_redis", return_value=_mock_redis(hit=False)),
        patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock, return_value=(None, 0.0)),
        patch("middleware.g05_cache._l2_store", new_callable=AsyncMock),
        patch("middleware.g10_memory._get_redis", return_value=_mock_redis()),
        patch("middleware.g10_memory._summarise", new_callable=AsyncMock, return_value="Summary."),
        patch("middleware.g17_loop_control._get_redis", return_value=_mock_redis()),
        # LLMLingua sidecar (G01) — error → fallback (no change)
        patch("middleware.g01_compression._call_llmlingua", new_callable=AsyncMock,
              side_effect=Exception("mock: no sidecar")),
        # Qdrant (G07) — error → fallback (no change)
        patch("middleware.g07_retrieval._hybrid_search",
              new_callable=AsyncMock, side_effect=Exception("mock: no qdrant")),
        # Tool registry (G08) — empty registry → skip
        patch("middleware.g08_tool_loading._load_registry", return_value=[]),
        # Langfuse (G18)
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
    ]


def _mock_redis(hit=False):
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    r.expire = AsyncMock()
    r.incr = AsyncMock(return_value=1)   # G17 turn counter
    r.aclose = AsyncMock()
    return r


@pytest.mark.asyncio
class TestPipelineRequestPath:
    async def test_pipeline_executes_without_error(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            from middleware.pipeline import OptimisationPipeline
            pipeline = OptimisationPipeline()
            ctx = await pipeline.process_request(ctx)
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)
        assert ctx is not None

    async def test_final_tokens_sent_set(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Quick question."}])
        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            from middleware.pipeline import OptimisationPipeline
            pipeline = OptimisationPipeline()
            ctx = await pipeline.process_request(ctx)
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)
        assert ctx.savings.final_tokens_sent >= 0

    async def test_g04_bypass_short_circuits(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello there"}])
        g05_called = []

        async def _track_g05(self_inst, c):
            g05_called.append(True)
            return c

        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            with patch("middleware.g05_cache.G05Cache.process_request", _track_g05):
                from middleware.pipeline import OptimisationPipeline
                pipeline = OptimisationPipeline()
                ctx = await pipeline.process_request(ctx)
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)

        assert ctx.bypassed is True
        assert len(g05_called) == 0, "G05 should not run after G04 bypass"

    async def test_g05_cache_hit_short_circuits(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is 2+2?"}])
        cached = json.dumps(_LLM_RESPONSE)

        patches = _patch_all_external()
        # Override G05 to return a cache hit
        patches[0] = patch("middleware.g05_cache._get_redis",
                           return_value=_mock_redis_with_hit(cached))
        active = [p.__enter__() for p in patches]
        g06_called = []

        async def _track_g06(self_inst, c):
            g06_called.append(True)
            return c

        try:
            with patch("middleware.g06_routing.G06Routing.process_request", _track_g06):
                from middleware.pipeline import OptimisationPipeline
                pipeline = OptimisationPipeline()
                ctx = await pipeline.process_request(ctx)
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)

        assert ctx.cache_hit is True
        assert len(g06_called) == 0, "G06 should not run after G05 cache hit"


def _mock_redis_with_hit(cached_value: str):
    r = AsyncMock()
    r.get = AsyncMock(return_value=cached_value)
    r.aclose = AsyncMock()
    return r


@pytest.mark.asyncio
class TestPipelineResponsePath:
    async def test_response_path_runs_g14_g15_g18(self, make_ctx):
        ctx = make_ctx()
        ctx.savings.final_tokens_sent = 20
        ctx.savings.response_tokens = 5

        g14_called = []
        g15_called = []
        g18_called = []

        async def _track_g14(self_inst, c, r): g14_called.append(True); return r
        async def _track_g15(self_inst, c, r): g15_called.append(True); return r
        async def _track_g18(self_inst, c, r): g18_called.append(True)

        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            with patch("middleware.g14_tool_output.G14ToolOutput.process_response", _track_g14), \
                 patch("middleware.g15_server_compute.G15ServerCompute.process_response", _track_g15), \
                 patch("middleware.g18_observability.G18Observability.record", _track_g18), \
                 patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock):
                from middleware.pipeline import OptimisationPipeline
                pipeline = OptimisationPipeline()
                ctx, response = await pipeline.process_response(ctx, dict(_LLM_RESPONSE))
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)

        assert len(g14_called) == 1, "G14 not called"
        assert len(g15_called) == 1, "G15 not called"
        assert len(g18_called) == 1, "G18 not called"

    async def test_store_response_called_after_g18(self, make_ctx):
        ctx = make_ctx()
        store_called = []

        async def _track_store(self_inst, c, r): store_called.append(True)

        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            with patch("middleware.g05_cache.G05Cache.store_response", _track_store):
                from middleware.pipeline import OptimisationPipeline
                pipeline = OptimisationPipeline()
                await pipeline.process_response(ctx, dict(_LLM_RESPONSE))
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)

        assert len(store_called) == 1, "store_response not called"


@pytest.mark.asyncio
class TestSavingsDataIntegrity:
    async def test_total_pct_saving_in_range(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
        ctx.savings.baseline_tokens = 50
        ctx.savings.final_tokens_sent = 30
        assert 0.0 <= ctx.savings.total_pct_saving <= 100.0

    async def test_step_savings_tokens_after_lte_tokens_before(self, make_ctx):
        ctx = make_ctx()
        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            from middleware.pipeline import OptimisationPipeline
            pipeline = OptimisationPipeline()
            ctx = await pipeline.process_request(ctx)
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)

        for step in ctx.savings.step_savings:
            assert step.absolute_saving >= 0, (
                f"{step.group} has negative saving: before={step.tokens_before} after={step.tokens_after}"
            )

    async def test_g06_routing_defers_cost_accounting_to_g18(self, make_ctx):
        """G06 (request path) must NOT pre-seed cost_{baseline,actual}_usd.

        Cost is owned solely by G18 (response path), which computes baseline and
        actual on a consistent input+output basis. The input-only figure G06 used to
        write left baseline output-less while G18 overwrote actual *with* output —
        producing the nonsensical 'actual ≫ baseline' cost line (the −21093% bug).
        """
        ctx = make_ctx([{"role": "user", "content": "What is 2+2?"}], model="gpt-4o")
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4o"],
        }
        patches = _patch_all_external()
        active = [p.__enter__() for p in patches]
        try:
            from middleware.pipeline import OptimisationPipeline
            pipeline = OptimisationPipeline()
            ctx = await pipeline.process_request(ctx)
        finally:
            for p, a in zip(patches, active):
                p.__exit__(None, None, None)

        # Request path leaves both cost fields untouched — G18 fills them in later.
        assert ctx.savings.cost_baseline_usd == 0.0
        assert ctx.savings.cost_actual_usd == 0.0
        # If routing did fire, it must surface as routed_model + a G06 step (not a cost mutation).
        if ctx.routed_model != "gpt-4o":
            assert any(s.group == "G06" for s in ctx.savings.step_savings)

    async def test_metadata_step_savings_pct_within_100(self, make_ctx):
        ctx = make_ctx()
        ctx.savings.baseline_tokens = 100
        ctx.savings.final_tokens_sent = 60
        ctx.savings.add_step("G01", "compressed", 100, 60)
        meta = ctx.savings.to_langfuse_metadata()
        for group, step in meta["step_savings"].items():
            assert 0.0 <= step["pct_saving_vs_baseline"] <= 100.0, (
                f"{group} pct_saving_vs_baseline out of range: {step['pct_saving_vs_baseline']}"
            )
