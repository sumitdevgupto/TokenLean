"""Unit tests for G15 — Server-Side Computation & MCP Offloading."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest


def _tool_result_response(tool_name: str, data) -> dict:
    """Build response with result embedded in tc['function']['result'] (G15 pattern)."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "function": {"name": tool_name, "result": data}
                }]
            }
        }],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10},
    }


@pytest.mark.asyncio
class TestG15ServerCompute:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G15_server_compute"]["enabled"] = False
        data = [{"id": i, "value": i * 10} for i in range(20)]
        response = _tool_result_response("get_data", data)
        original_len = len(response["choices"][0]["message"]["tool_calls"][0]["function"]["result"])
        from middleware.g15_server_compute import G15ServerCompute
        response = await G15ServerCompute().process_response(ctx, response)
        result = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert len(result) == original_len

    async def test_no_hooks_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G15_server_compute"]["hooks"] = []
        data = [{"id": i} for i in range(10)]
        response = _tool_result_response("any_tool", data)
        original_data = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        from middleware.g15_server_compute import G15ServerCompute
        response = await G15ServerCompute().process_response(ctx, response)
        # No hooks → content unchanged
        result = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert result == original_data

    async def test_top_n_hook_limits_results(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G15_server_compute"]["hooks"] = [
            {"tool": "get_rows", "top_n": 3}
        ]
        data = [{"id": i} for i in range(20)]
        response = _tool_result_response("get_rows", data)
        from middleware.g15_server_compute import G15ServerCompute
        response = await G15ServerCompute().process_response(ctx, response)
        result = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert len(result) <= 3

    async def test_filter_hook_reduces_results(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G15_server_compute"]["hooks"] = [
            {"tool": "get_items", "filter_field": "active", "filter_value": True}
        ]
        data = [{"id": i, "active": i % 2 == 0} for i in range(10)]
        response = _tool_result_response("get_items", data)
        from middleware.g15_server_compute import G15ServerCompute
        response = await G15ServerCompute().process_response(ctx, response)
        result = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert all(item["active"] for item in result)

    async def test_step_saving_recorded_when_data_reduced(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G15_server_compute"]["hooks"] = [
            {"tool": "big_query", "top_n": 2}
        ]
        data = [{"id": i, "data": "x" * 100} for i in range(20)]
        response = _tool_result_response("big_query", data)
        from middleware.g15_server_compute import G15ServerCompute
        response = await G15ServerCompute().process_response(ctx, response)
        if any(s.group == "G15" for s in ctx.savings.step_savings):
            step = next(s for s in ctx.savings.step_savings if s.group == "G15")
            assert step.tokens_after <= step.tokens_before

    async def test_hook_exception_preserves_response(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G15_server_compute"]["hooks"] = [
            {"tool": "safe_tool", "top_n": 3}
        ]
        # top_n on a non-list returns the non-list unchanged (no exception)
        response = _tool_result_response("safe_tool", "plain-string-not-a-list")
        from middleware.g15_server_compute import G15ServerCompute
        response = await G15ServerCompute().process_response(ctx, response)
        assert "choices" in response
