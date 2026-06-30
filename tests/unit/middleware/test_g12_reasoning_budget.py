"""Unit tests for G12 — Reasoning Budget Control."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from providers.anthropic_adapter import AnthropicAdapter


@pytest.mark.asyncio
class TestG12ReasoningBudget:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx(model="o1")
        ctx.config["groups"]["G12_reasoning"]["enabled"] = False
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in ctx.params

    async def test_o1_model_injects_reasoning_effort(self, make_ctx):
        ctx = make_ctx(model="o1")
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" in ctx.params
        assert ctx.params["reasoning_effort"] == "medium"

    async def test_o3_model_injects_reasoning_effort(self, make_ctx):
        ctx = make_ctx(model="o3")
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" in ctx.params

    async def test_claude_model_injects_thinking_budget(self, make_ctx):
        ctx = make_ctx(model="claude-sonnet-4-5")
        ctx.provider_adapter = AnthropicAdapter()
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "thinking" in ctx.params

    async def test_non_reasoning_model_unchanged(self, make_ctx):
        # gpt-4o-mini: OpenAI adapter's supports_reasoning returns False
        ctx = make_ctx(model="gpt-4o-mini")
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in ctx.params
        assert "thinking" not in ctx.params

    async def test_effort_low_overrides_default(self, make_ctx):
        ctx = make_ctx(model="o1")
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params.get("reasoning_effort") == "low"
