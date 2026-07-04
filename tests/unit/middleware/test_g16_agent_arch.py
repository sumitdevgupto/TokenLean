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

    async def test_absent_system_prompt_key_uses_4096_fallback(self, make_ctx):
        # With the config key absent, the code fallback is 4096 (aligned to the template),
        # not the legacy 800 — a ~1000-token system prompt must NOT be truncated.
        from middleware.g16_agent_arch import G16AgentArch, _MAX_SYSTEM_PROMPT_TOKENS
        assert _MAX_SYSTEM_PROMPT_TOKENS == 4096
        system = "word " * 1000  # over the old 800 fallback, well under 4096
        ctx = make_ctx([
            {"role": "system", "content": system},
            {"role": "user", "content": "hi"},
        ])
        del ctx.config["groups"]["G16_agent_arch"]["max_system_prompt_tokens"]
        ctx = await G16AgentArch().process_request(ctx)
        system_msg = next(m for m in ctx.messages if m["role"] == "system")
        assert system_msg["content"] == system  # under the 4096 fallback → untouched

    async def test_absent_tools_key_uses_20_fallback(self, make_ctx):
        from middleware.g16_agent_arch import G16AgentArch, _MAX_TOOLS_COUNT
        assert _MAX_TOOLS_COUNT == 20
        ctx = make_ctx(params={"tools": [{"name": f"t{i}"} for i in range(15)]})
        del ctx.config["groups"]["G16_agent_arch"]["max_tools_per_agent"]
        ctx = await G16AgentArch().process_request(ctx)
        assert len(ctx.params["tools"]) == 15  # 15 < 20 fallback → no pruning

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


def _tool_names(tools):
    """Extract names from either flat {'name': ...} or OpenAI-nested {'function': {'name': ...}} tools."""
    out = []
    for t in tools:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        out.append(fn.get("name"))
    return out


@pytest.mark.asyncio
class TestG16RelevanceToolSelection:
    """When more tools than the cap are supplied, keep the ones relevant to the request
    (not the first N by list order). Reproduces the DS3 failure where get_user_profile,
    sitting late in the list, was silently dropped even though the user asked for it."""

    async def test_relevance_keeps_referenced_tool_late_in_list(self, make_ctx):
        # cap = 3; the relevant tool is LAST — a blind tools[:3] slice would drop it.
        tools = [
            {"type": "function", "function": {"name": "send_email", "description": "Send an email."}},
            {"type": "function", "function": {"name": "list_logs", "description": "List log entries."}},
            {"type": "function", "function": {"name": "calculate_total", "description": "Compute statistics."}},
            {"type": "function", "function": {"name": "search_kb", "description": "Search the knowledge base."}},
            {"type": "function", "function": {"name": "get_user_profile", "description": "Retrieve a user profile."}},
        ]
        ctx = make_ctx([
            {"role": "system", "content": "You are an SRE agent."},
            {"role": "user", "content": "Also get the user profile for the engineer who opened the ticket."},
        ], params={"tools": tools})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        kept = _tool_names(ctx.params["tools"])
        assert len(kept) == 3
        assert "get_user_profile" in kept  # the whole point — not dropped despite being last

    async def test_relevance_is_order_independent(self, make_ctx):
        base = [
            {"function": {"name": "send_email", "description": "Send an email."}},
            {"function": {"name": "get_user_profile", "description": "Retrieve a user profile."}},
            {"function": {"name": "list_logs", "description": "List log entries."}},
            {"function": {"name": "calculate_total", "description": "Compute totals."}},
            {"function": {"name": "search_kb", "description": "Search the knowledge base."}},
        ]
        msgs = [{"role": "user", "content": "get the user profile please"}]
        from middleware.g16_agent_arch import G16AgentArch
        ctx_a = make_ctx(list(msgs), params={"tools": list(base)})
        ctx_a = await G16AgentArch().process_request(ctx_a)
        ctx_b = make_ctx(list(msgs), params={"tools": list(reversed(base))})
        ctx_b = await G16AgentArch().process_request(ctx_b)
        # Same relevant tool survives regardless of input ordering
        assert "get_user_profile" in _tool_names(ctx_a.params["tools"])
        assert "get_user_profile" in _tool_names(ctx_b.params["tools"])

    async def test_relevance_respects_cap(self, make_ctx):
        tools = [{"function": {"name": f"tool_{i}", "description": "generic"}} for i in range(9)]
        ctx = make_ctx([{"role": "user", "content": "do something"}], params={"tools": tools})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        max_tools = ctx.config["groups"]["G16_agent_arch"]["max_tools_per_agent"]
        assert len(ctx.params["tools"]) == max_tools

    async def test_order_strategy_keeps_first_n(self, make_ctx):
        tools = [{"function": {"name": f"tool_{i}", "description": "x"}} for i in range(5)]
        ctx = make_ctx([{"role": "user", "content": "tool_4 please"}], params={"tools": tools})
        ctx.config["groups"]["G16_agent_arch"]["tool_selection_strategy"] = "order"
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        # Explicit opt-out: blind first-N behaviour preserved for backward compat
        assert _tool_names(ctx.params["tools"]) == ["tool_0", "tool_1", "tool_2"]

    async def test_no_query_overlap_falls_back_to_order(self, make_ctx):
        # No token overlap → all scores 0 → stable original order (first N)
        tools = [{"function": {"name": f"alpha{i}", "description": "zzz"}} for i in range(5)]
        ctx = make_ctx([{"role": "user", "content": "unrelated request text"}], params={"tools": tools})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert _tool_names(ctx.params["tools"]) == ["alpha0", "alpha1", "alpha2"]

    async def test_malformed_tool_does_not_crash(self, make_ctx):
        tools = [
            {"function": {"name": "get_user_profile", "description": "Retrieve a user profile."}},
            {"weird": "shape"},
            None,
            {"function": {"name": "send_email"}},
            "not-a-dict",
        ]
        ctx = make_ctx([{"role": "user", "content": "get the user profile"}], params={"tools": tools})
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)  # must not raise
        assert len(ctx.params["tools"]) == 3
