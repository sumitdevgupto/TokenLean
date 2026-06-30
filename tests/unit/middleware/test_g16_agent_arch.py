"""Unit tests for G16 — Agent Architecture Advisories."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest


@pytest.mark.asyncio
class TestG16AgentArch:
    async def test_disabled_no_warnings(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G16_agent_arch"]["enabled"] = False
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert "_token_opt_warnings" not in ctx.params

    async def test_small_system_prompt_no_warning(self, make_ctx):
        ctx = make_ctx([
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "hi"},
        ])
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        warnings = ctx.params.get("_token_opt_warnings", [])
        assert len(warnings) == 0

    async def test_oversized_system_prompt_warns(self, make_ctx):
        # Threshold is 50 tokens in minimal_config
        huge_system = "You are a helpful assistant that handles many tasks. " * 15
        ctx = make_ctx([
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "ok"},
        ])
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        warnings = ctx.params.get("_token_opt_warnings", [])
        assert len(warnings) >= 1
        assert any("system prompt" in w.lower() or "threshold" in w.lower() for w in warnings)

    async def test_too_many_tools_warns(self, make_ctx):
        # Threshold is 3 in minimal_config
        ctx = make_ctx(params={"tools": [{"name": f"tool_{i}"} for i in range(10)]})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        warnings = ctx.params.get("_token_opt_warnings", [])
        assert any("tool" in w.lower() for w in warnings)

    async def test_warnings_recorded_as_step_saving(self, make_ctx):
        huge_system = "Very long system prompt content. " * 15
        ctx = make_ctx([
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "hi"},
        ])
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert any(s.group == "G16" for s in ctx.savings.step_savings)

    async def test_within_limits_no_step_saving(self, make_ctx):
        ctx = make_ctx([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ], params={"tools": [{"name": "tool_1"}]})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert not any(s.group == "G16" for s in ctx.savings.step_savings)

    async def test_oversized_system_prompt_is_truncated(self, make_ctx):
        # Threshold is 50 tokens in minimal_config
        huge_system = "You are a helpful assistant that handles many tasks. " * 15
        ctx = make_ctx([
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "ok"},
        ])
        from middleware.g16_agent_arch import G16AgentArch
        from savings.calculator import count_messages_tokens
        ctx = await G16AgentArch().process_request(ctx)

        system_msg = next(m for m in ctx.messages if m["role"] == "system")
        assert len(system_msg["content"]) < len(huge_system)
        assert count_messages_tokens([system_msg], ctx.model) <= ctx.config["groups"]["G16_agent_arch"]["max_system_prompt_tokens"]

        step = next(s for s in ctx.savings.step_savings if s.group == "G16")
        assert step.tokens_before > step.tokens_after
        assert step.absolute_saving > 0

    async def test_system_prompt_exactly_at_threshold_not_truncated(self, make_ctx):
        max_sys = 50  # from minimal_config
        # Build a system message whose token count is <= threshold
        system_content = "Be helpful and concise in all your responses please."
        ctx = make_ctx([
            {"role": "system", "content": system_content},
            {"role": "user", "content": "hi"},
        ])
        from middleware.g16_agent_arch import G16AgentArch
        from savings.calculator import count_messages_tokens
        original_tokens = count_messages_tokens([{"role": "system", "content": system_content}], ctx.model)
        assert original_tokens <= max_sys  # sanity check on fixture content

        ctx = await G16AgentArch().process_request(ctx)
        system_msg = next(m for m in ctx.messages if m["role"] == "system")
        assert system_msg["content"] == system_content
        assert not any(s.group == "G16" for s in ctx.savings.step_savings)

    async def test_too_many_tools_are_pruned(self, make_ctx):
        # Threshold is 3 in minimal_config
        tools = [{"name": f"tool_{i}", "description": "A test tool."} for i in range(10)]
        ctx = make_ctx(params={"tools": tools})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)

        max_tools = ctx.config["groups"]["G16_agent_arch"]["max_tools_per_agent"]
        assert len(ctx.params["tools"]) == max_tools

        step = next(s for s in ctx.savings.step_savings if s.group == "G16")
        assert step.tokens_before > step.tokens_after
        assert step.absolute_saving > 0

    async def test_zero_tools_no_tool_warning(self, make_ctx):
        ctx = make_ctx([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ], params={"tools": []})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        warnings = ctx.params.get("_token_opt_warnings", [])
        assert not any("tool" in w.lower() for w in warnings)

    async def test_tools_at_threshold_not_pruned(self, make_ctx):
        # Threshold is 3 in minimal_config — exactly 3 tools must not be pruned
        tools = [{"name": f"tool_{i}"} for i in range(3)]
        ctx = make_ctx([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ], params={"tools": tools})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert len(ctx.params["tools"]) == 3
        assert not any(s.group == "G16" for s in ctx.savings.step_savings)
