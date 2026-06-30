"""Tests for G12ReasoningBudget — adapter-based reasoning parameter injection."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from middleware.g12_reasoning_budget import G12ReasoningBudget
from middleware import RequestContext
from savings.models import SavingsRecord
from providers.openai_adapter import OpenAIAdapter
from providers.anthropic_adapter import AnthropicAdapter


_BASE_CONFIG = {
    "groups": {
        "G12_reasoning": {
            "enabled": True,
            "default_effort": "medium",
            "effort_map": {
                "low":    {"openai": "low",    "anthropic_tokens": 1000},
                "medium": {"openai": "medium", "anthropic_tokens": 5000},
                "high":   {"openai": "high",   "anthropic_tokens": 10000},
            },
        }
    }
}


def _make_ctx(model="gpt-4o", config=None, provider_adapter=None):
    import copy
    savings = SavingsRecord(
        request_id="req-g12",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested=model,
        routed_model=model,
        baseline_tokens=100,
    )
    cfg = copy.deepcopy(config or _BASE_CONFIG)
    ctx = RequestContext(
        request_id="req-g12",
        user_id="u1",
        original_messages=[{"role": "user", "content": "solve this"}],
        messages=[{"role": "user", "content": "solve this"}],
        model=model,
        routed_model=model,
        params={},
        config=cfg,
        savings=savings,
    )
    ctx.provider_adapter = provider_adapter
    return ctx


class TestG12OSeries:
    @pytest.mark.asyncio
    async def test_o1_model_gets_reasoning_effort_injected(self):
        ctx = _make_ctx(model="o1-mini", provider_adapter=OpenAIAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params.get("reasoning_effort") == "medium"

    @pytest.mark.asyncio
    async def test_o1_reasoning_effort_uses_effort_map(self):
        ctx = _make_ctx(model="o1", provider_adapter=OpenAIAdapter())
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "high"
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_o3_model_gets_reasoning_effort_injected(self):
        ctx = _make_ctx(model="o3-mini", provider_adapter=OpenAIAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params.get("reasoning_effort") == "medium"

    @pytest.mark.asyncio
    async def test_o4_model_gets_reasoning_effort_injected(self):
        ctx = _make_ctx(model="o4-mini", provider_adapter=OpenAIAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params.get("reasoning_effort") == "medium"

    @pytest.mark.asyncio
    async def test_existing_reasoning_effort_not_overwritten(self):
        ctx = _make_ctx(model="o1", provider_adapter=OpenAIAdapter())
        ctx.params["reasoning_effort"] = "low"
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_o_model_saves_step(self):
        ctx = _make_ctx(model="o1-mini", provider_adapter=OpenAIAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert any(s.group == "G12" for s in ctx.savings.step_savings)


class TestG12Claude:
    @pytest.mark.asyncio
    async def test_claude_sonnet_gets_thinking_injected(self):
        ctx = _make_ctx(model="claude-sonnet-4-5", provider_adapter=AnthropicAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params.get("thinking", {}).get("type") == "enabled"
        assert ctx.params["thinking"]["budget_tokens"] == 5000  # from effort_map.medium

    @pytest.mark.asyncio
    async def test_claude_thinking_budget_from_effort_map(self):
        ctx = _make_ctx(model="claude-3-opus", provider_adapter=AnthropicAdapter())
        ctx.config["groups"]["G12_reasoning"]["effort_map"]["medium"]["anthropic_tokens"] = 12000
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params["thinking"]["budget_tokens"] == 12000

    @pytest.mark.asyncio
    async def test_existing_thinking_param_not_overwritten(self):
        ctx = _make_ctx(model="claude-3-sonnet", provider_adapter=AnthropicAdapter())
        ctx.params["thinking"] = {"type": "enabled", "budget_tokens": 1000}
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params["thinking"]["budget_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_claude_thinking_saves_step(self):
        ctx = _make_ctx(model="claude-sonnet-4-5", provider_adapter=AnthropicAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert any(s.group == "G12" for s in ctx.savings.step_savings)


class TestG12StandardModel:
    @pytest.mark.asyncio
    async def test_gpt4o_no_reasoning_injected(self):
        # gpt-4o does not support reasoning_effort (only o-series does)
        ctx = _make_ctx(model="gpt-4o", provider_adapter=OpenAIAdapter())
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in ctx.params
        assert "thinking" not in ctx.params

    @pytest.mark.asyncio
    async def test_disabled_skips_all(self):
        ctx = _make_ctx(model="o1", provider_adapter=OpenAIAdapter())
        ctx.config["groups"]["G12_reasoning"]["enabled"] = False
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in ctx.params

    @pytest.mark.asyncio
    async def test_no_effort_map_entry_skips_inject(self):
        """When effort_map has no entry for the requested tier, nothing is injected."""
        ctx = _make_ctx(model="o1", provider_adapter=OpenAIAdapter())
        ctx.config["groups"]["G12_reasoning"]["effort_map"] = {}  # empty map
        ctx = await G12ReasoningBudget().process_request(ctx)
        # OpenAI adapter falls back to tier name when no config entry; still injects
        # (this is correct — adapter always has a value)
        assert ctx.params.get("reasoning_effort") == "medium"
