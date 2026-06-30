"""Unit tests for G17 — Loop Control & Token Budget Propagation."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
class TestG17LoopControl:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx(params={"workflow_id": "wf-1"})
        ctx.config["groups"]["G17_loop"]["enabled"] = False
        from middleware.g17_loop_control import G17LoopControl
        ctx = await G17LoopControl().process_request(ctx)
        assert "_budget_remaining" not in str(ctx.messages)

    async def test_no_workflow_id_skips(self, make_ctx):
        ctx = make_ctx()
        original_messages = [m.copy() for m in ctx.messages]
        from middleware.g17_loop_control import G17LoopControl
        ctx = await G17LoopControl().process_request(ctx)
        assert ctx.messages == original_messages

    async def test_budget_propagated_into_params(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Do the next task."}],
            params={"workflow_id": "wf-budget-test"},
        )
        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=1)       # turn counter
        mock_redis.expire = AsyncMock()
        mock_redis.get = AsyncMock(return_value="800")    # budget remaining
        mock_redis.set = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g17_loop_control._get_redis", return_value=mock_redis):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)

        # _token_budget injected into params
        assert "_token_budget" in ctx.params
        assert ctx.params["_token_budget"]["token_budget_remaining"] >= 0

    async def test_exceeded_iterations_sets_warning(self, make_ctx):
        ctx = make_ctx(params={"workflow_id": "wf-overrun"})
        ctx.config["groups"]["G17_loop"]["max_iterations"] = 3

        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=6)   # 6 > 3 iterations
        mock_redis.expire = AsyncMock()
        mock_redis.get = AsyncMock(return_value="500")
        mock_redis.set = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g17_loop_control._get_redis", return_value=mock_redis):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)

        assert ctx.params.get("_token_opt_loop_limit_reached") is True
        warnings = ctx.params.get("_token_opt_warnings", [])
        assert any("loop limit" in w.lower() or "turns" in w.lower() for w in warnings)

    async def test_low_budget_injects_compact_instruction(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Continue the workflow."}],
            params={"workflow_id": "wf-low-budget"},
        )
        ctx.config["groups"]["G17_loop"]["compact_output_below_tokens"] = 10000

        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock()
        mock_redis.get = AsyncMock(return_value="50")  # only 50 tokens left < 10000 threshold
        mock_redis.set = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g17_loop_control._get_redis", return_value=mock_redis):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)

        all_content = " ".join(str(m.get("content", "")) for m in ctx.messages)
        assert "BUDGET" in all_content or "budget" in all_content.lower()

    async def test_redis_error_fallback(self, make_ctx):
        ctx = make_ctx(params={"workflow_id": "wf-redis-fail"})
        with patch("middleware.g17_loop_control._get_redis", side_effect=Exception("redis down")):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)
        assert ctx is not None

    async def test_shared_redis_pool_survives_concurrent_workflows(self, make_ctx):
        """G17 must never call aclose() on the shared connection-pool client —
        doing so would disconnect the pool out from under other concurrent
        requests. Run two workflows against the same client instance and
        confirm it remains open and usable for both."""
        shared_redis = AsyncMock()
        shared_redis.incr = AsyncMock(return_value=1)
        shared_redis.expire = AsyncMock()
        shared_redis.get = AsyncMock(return_value="800")
        shared_redis.set = AsyncMock()

        ctx_a = make_ctx(
            [{"role": "user", "content": "Task A"}],
            params={"workflow_id": "wf-a"},
        )
        ctx_b = make_ctx(
            [{"role": "user", "content": "Task B"}],
            params={"workflow_id": "wf-b"},
        )

        with patch("middleware.g17_loop_control._get_redis", return_value=shared_redis):
            from middleware.g17_loop_control import G17LoopControl
            g17 = G17LoopControl()
            ctx_a = await g17.process_request(ctx_a)
            ctx_b = await g17.process_request(ctx_b)

        assert "_token_budget" in ctx_a.params
        assert "_token_budget" in ctx_b.params
        shared_redis.aclose.assert_not_called()
        shared_redis.close.assert_not_called()
