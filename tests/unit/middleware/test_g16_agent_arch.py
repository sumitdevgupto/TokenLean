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

    async def test_oversized_system_prompt_is_left_intact_by_default(self, make_ctx):
        """CHANGED BY DESIGN 2026-09-06 (E15). This test previously asserted the prompt was
        truncated. Silently deleting the end of a customer's instructions to save tokens is
        not an optimisation: on DS3's real SRE playbook the cut removed the whole "Safety
        Constraints & Guardrails" section, including the rule about when a ticket may be
        closed. The default no longer edits the prompt at all; it warns and leaves it byte-
        identical. Enforcement is available, and lossless at both ends, via
        `system_prompt_overflow: compact`.
        """
        huge_system = "You are a helpful assistant that handles many tasks. " * 15
        ctx = make_ctx([
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "ok"},
        ])
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)

        system_msg = next(m for m in ctx.messages if m["role"] == "system")
        assert system_msg["content"] == huge_system
        assert any("left intact" in w for w in ctx.params["_token_opt_warnings"])
        assert not any(s.group == "G16" and s.absolute_saving > 0 for s in ctx.savings.step_savings)

    async def test_compact_mode_enforces_the_cap(self, make_ctx):
        huge_system = "You are a helpful assistant that handles many tasks.\n\n" * 15
        ctx = make_ctx([
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "ok"},
        ])
        ctx.config["groups"]["G16_agent_arch"]["system_prompt_overflow"] = "compact"
        from middleware.g16_agent_arch import G16AgentArch
        from savings.calculator import count_messages_tokens
        ctx = await G16AgentArch().process_request(ctx)

        system_msg = next(m for m in ctx.messages if m["role"] == "system")
        cap = ctx.config["groups"]["G16_agent_arch"]["max_system_prompt_tokens"]
        assert count_messages_tokens([system_msg], ctx.model) <= cap
        step = next(s for s in ctx.savings.step_savings if s.group == "G16")
        assert step.absolute_saving > 0

    async def test_an_unknown_overflow_mode_leaves_the_prompt_intact(self, make_ctx):
        """Fail SAFE on a typo. `fnmatch`-style silent misbehaviour is the precedent being
        avoided: an operator who writes `truncat` must not get an unannounced edit."""
        huge_system = "You are a helpful assistant that handles many tasks. " * 15
        ctx = make_ctx([
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "ok"},
        ])
        ctx.config["groups"]["G16_agent_arch"]["system_prompt_overflow"] = "truncat"
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert next(m for m in ctx.messages if m["role"] == "system")["content"] == huge_system

    async def test_compact_does_not_claim_to_compact_a_multimodal_system_message(self, make_ctx):
        """G16 only rewrites string content. With a list-content system message there is
        nothing to compact, so the warning must not say it compacted anything."""
        parts = [{"type": "text", "text": "You are an assistant. " * 40}]
        ctx = make_ctx([
            {"role": "system", "content": parts},
            {"role": "user", "content": "ok"},
        ])
        ctx.config["groups"]["G16_agent_arch"]["system_prompt_overflow"] = "compact"
        from middleware.g16_agent_arch import G16AgentArch
        ctx = await G16AgentArch().process_request(ctx)
        assert next(m for m in ctx.messages if m["role"] == "system")["content"] == parts
        assert not any("compacted" in w for w in ctx.params.get("_token_opt_warnings", []))

    async def test_compact_holds_the_cap_across_several_system_messages(self, make_ctx):
        """Regression, 2026-09-06. Each system message was budgeted the FULL cap, so a prompt
        split across messages tripped the threshold and was returned unchanged — while the
        warning said "truncated to fit". Same customer, same tokens, opposite treatment
        decided only by how many system messages they happened to write."""
        block = ("policy line for the agent to follow\n\n" * 12)
        ctx = make_ctx(
            [{"role": "system", "content": block} for _ in range(3)]
            + [{"role": "user", "content": "ok"}]
        )
        ctx.config["groups"]["G16_agent_arch"]["system_prompt_overflow"] = "compact"
        from middleware.g16_agent_arch import G16AgentArch
        from savings.calculator import count_messages_tokens
        cap = ctx.config["groups"]["G16_agent_arch"]["max_system_prompt_tokens"]
        before = sum(count_messages_tokens([m], ctx.model)
                     for m in ctx.messages if m["role"] == "system")
        assert before > cap  # the fixture must actually trip the threshold
        ctx = await G16AgentArch().process_request(ctx)
        after = sum(count_messages_tokens([m], ctx.model)
                    for m in ctx.messages if m["role"] == "system")
        assert after <= cap, "the cap must hold however the customer splits their system prompt"

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


class TestCompactionKeepsBothEnds:
    """E15. A system prompt's END carries operating policy — closure rules, escalation-audit
    rules, "never include raw credentials". A tail cut deletes exactly those. Compaction drops
    the MIDDLE instead, which costs the same tokens and keeps both the role and the rules.

    Compaction is still lossy — a rule sitting in the middle is still dropped, which is why it
    is opt-in and the default leaves the prompt alone.
    """

    def _prompt(self):
        return (
            "You are an SRE assistant for the payments platform.\n\n"
            + "".join(f"{i}. Routine background paragraph about tooling.\n\n" for i in range(2, 40))
            + "40. Safety Constraints. Do not close an incident ticket until the incident "
              "record reflects a clear resolution status.\n"
        )

    def test_both_ends_survive_and_the_middle_goes(self):
        from middleware.g16_agent_arch import _compact_to_tokens
        from savings.calculator import estimate_tokens
        text = self._prompt()
        out = _compact_to_tokens(text, 120, "gpt-4o-mini")
        assert estimate_tokens(out, "gpt-4o-mini") <= 120
        assert "You are an SRE assistant" in out, "the role must survive"
        assert "Do not close an incident ticket" in out, (
            "the closing policy must survive — deleting it is the defect this replaces"
        )
        assert "20. Routine background" not in out, "the middle is what should go"

    def test_it_actually_spends_the_budget_it_is_given(self):
        """Regression, 2026-09-06 (found while measuring E22, hours after shipping E15).
        Units are kept or dropped WHOLE, so a single oversized unit is dropped entire and its
        budget goes unspent. On the benchmark's agentic system prompt — three paragraphs of
        81 / 924 / 36 tokens — a 796-token budget kept only 152 tokens: 644 tokens of the
        customer's instructions deleted for nothing. Every token of budget left unused is a
        sentence of theirs thrown away, so the floor is a property worth pinning, not a
        nice-to-have.
        """
        from middleware.g16_agent_arch import _compact_to_tokens
        from savings.calculator import estimate_tokens
        text = ("You are an operations agent.\n\n"
                + " ".join(f"Step {i} explains a procedure in some detail." for i in range(400))
                + "\n\nAlways end by summarising the outcome.\n")
        assert estimate_tokens(text, "gpt-4o-mini") > 2000  # fixture must be over budget
        out = _compact_to_tokens(text, 800, "gpt-4o-mini")
        used = estimate_tokens(out, "gpt-4o-mini")
        assert used <= 800
        assert used >= 800 * 0.8, f"only {used}/800 tokens of budget used — the rest is deleted content"

    def test_a_single_oversized_paragraph_does_not_waste_the_budget(self):
        """The exact shape that failed: one paragraph far larger than the budget, between two
        small ones. Dropping it whole is correct; dropping it whole and then stopping is not."""
        from middleware.g16_agent_arch import _compact_to_tokens
        from savings.calculator import estimate_tokens
        text = ("Opening role line.\n\n"
                + " ".join(f"Sentence number {i} carries a policy detail." for i in range(300))
                + "\n\nClosing policy line.\n")
        out = _compact_to_tokens(text, 600, "gpt-4o-mini")
        used = estimate_tokens(out, "gpt-4o-mini")
        assert used <= 600
        assert used >= 600 * 0.8, f"only {used}/600 tokens of budget used"
        assert "Opening role line" in out and "Closing policy line" in out

    def test_the_elision_is_declared_to_the_model(self):
        """A truncated policy that looks complete is worse than one marked incomplete."""
        from middleware.g16_agent_arch import _compact_to_tokens
        out = _compact_to_tokens(self._prompt(), 120, "gpt-4o-mini")
        assert "omitted from the middle" in out

    def test_a_prompt_under_budget_is_returned_byte_identical(self):
        from middleware.g16_agent_arch import _compact_to_tokens
        text = self._prompt()
        assert _compact_to_tokens(text, 100_000, "gpt-4o-mini") == text

    def test_a_prompt_with_no_line_structure_still_keeps_both_ends(self):
        from middleware.g16_agent_arch import _compact_to_tokens
        from savings.calculator import estimate_tokens
        text = "START " + ("filler " * 4000) + " END-OF-POLICY"
        out = _compact_to_tokens(text, 200, "gpt-4o-mini")
        assert estimate_tokens(out, "gpt-4o-mini") <= 200
        assert out.startswith("START")
        assert out.endswith("END-OF-POLICY")

    def test_a_budget_too_small_for_a_marker_keeps_the_end(self):
        """When there is not even room to say content was removed, keep the policy, not the
        greeting — the opposite of what a tail cut does."""
        from middleware.g16_agent_arch import _compact_to_tokens
        from savings.calculator import estimate_tokens
        text = "hello there friend " * 200 + "NEVER LOG CREDENTIALS"
        out = _compact_to_tokens(text, 3, "gpt-4o-mini")
        assert estimate_tokens(out, "gpt-4o-mini") <= 3
        assert "CREDENTIALS" in out

    def test_dense_text_is_not_duplicated_by_the_char_fallback(self):
        """`keep` is a character budget derived from a token budget. Dense scripts run well
        under 4 chars/token, so the head and tail slices could overlap and return MORE text
        than the input while claiming to have elided some."""
        from middleware.g16_agent_arch import _compact_to_tokens
        from savings.calculator import estimate_tokens
        dense = "中文漢字" * 300 + "END"
        out = _compact_to_tokens(dense, 40, "gpt-4o-mini")
        assert estimate_tokens(out, "gpt-4o-mini") <= 40
        assert len(out) < len(dense)
        assert out.endswith("END")

    def test_zero_budget_is_empty(self):
        from middleware.g16_agent_arch import _compact_to_tokens
        assert _compact_to_tokens("anything at all", 0, "gpt-4o-mini") == ""

    def test_the_blind_tail_cut_is_gone(self):
        """Gate 9: the harmful path is removed, not left one config flip away."""
        import middleware.g16_agent_arch as g16
        assert not hasattr(g16, "_truncate_to_tokens")
