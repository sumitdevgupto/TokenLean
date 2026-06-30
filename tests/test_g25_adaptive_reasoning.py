"""Tests for G25 — Adaptive Reasoning (complexity classification and effort injection)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock


# ─── Helpers ────────────────────────────────────────────────────────────────

# Provider detection is adapter-driven (production sets ctx.provider_adapter via the
# pipeline). The tests mirror that by attaching the adapter get_adapter() resolves.
_PROVIDERS_CONFIG = [
    {"name": "openai", "model_prefixes": ["gpt", "o1", "o3", "o4", "text-"]},
    {"name": "anthropic", "model_prefixes": ["claude"]},
    {"name": "gemini", "model_prefixes": ["gemini"]},
]


def _make_ctx(messages=None, model="o1-mini", params=None, enabled=True, cfg_extra=None):
    from middleware import RequestContext
    from savings.models import SavingsRecord
    from providers import get_adapter

    savings = SavingsRecord(
        request_id="req-g25",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested=model,
        routed_model=model,
        baseline_tokens=50,
    )
    cfg = {
        "groups": {
            "G25_adaptive_reasoning": {
                "enabled": enabled,
                **(cfg_extra or {}),
            }
        }
    }
    msgs = messages or [{"role": "user", "content": "hello"}]
    ctx = RequestContext(
        request_id="req-g25",
        user_id="u1",
        original_messages=list(msgs),
        messages=list(msgs),
        model=model,
        routed_model=model,
        params=dict(params or {}),
        config=cfg,
        savings=savings,
    )
    ctx.provider_adapter = get_adapter(model, _PROVIDERS_CONFIG)
    return ctx


# ─── _classify_complexity ────────────────────────────────────────────────────

class TestClassifyComplexity:

    def _classify(self, text):
        from middleware.g25_adaptive_reasoning import (
            _classify_complexity, _build_patterns,
            _DEFAULT_HIGH_KEYWORDS, _DEFAULT_MEDIUM_KEYWORDS, _DEFAULT_LOW_KEYWORDS,
        )
        return _classify_complexity(
            text,
            _build_patterns(_DEFAULT_HIGH_KEYWORDS),
            _build_patterns(_DEFAULT_MEDIUM_KEYWORDS),
            _build_patterns(_DEFAULT_LOW_KEYWORDS),
        )

    def test_high_keywords_detected(self):
        effort, reason = self._classify("Can you prove that P ≠ NP?")
        assert effort == "high"
        assert "high" in reason

    def test_dynamic_programming_is_high(self):
        effort, _ = self._classify("Solve this using dynamic programming: coin change problem.")
        assert effort == "high"

    def test_optimise_is_high(self):
        effort, _ = self._classify("Please optimise this SQL query for a 100M row table.")
        assert effort == "high"

    def test_explain_is_medium(self):
        effort, reason = self._classify("Explain why TCP needs a three-way handshake.")
        assert effort == "medium"
        assert "medium" in reason

    def test_debug_is_medium(self):
        effort, _ = self._classify("Debug this Python function — it returns None unexpectedly.")
        assert effort == "medium"

    def test_root_cause_is_medium(self):
        effort, _ = self._classify("What is the root-cause of this 500 error?")
        assert effort == "medium"

    def test_what_is_is_low(self):
        effort, reason = self._classify("What is the capital of France?")
        assert effort == "low"
        assert "low" in reason

    def test_summarise_is_low(self):
        effort, _ = self._classify("Summarise the following paragraph in one sentence.")
        assert effort == "low"

    def test_list_is_low(self):
        effort, _ = self._classify("List all AWS regions that support GPU instances.")
        assert effort == "low"

    def test_no_match_defaults_to_medium(self):
        effort, reason = self._classify("The quick brown fox jumps over the lazy dog.")
        assert effort == "medium"
        assert "defaulting to medium" in reason

    def test_high_takes_priority_over_medium_keywords(self):
        # "prove" (high) and "explain" (medium) both present — high wins
        effort, _ = self._classify("Can you explain how to prove this theorem step by step?")
        assert effort == "high"


# ─── _is_reasoning_model ────────────────────────────────────────────────────

class TestIsReasoningModel:

    def _check(self, model, extra=None):
        from middleware.g25_adaptive_reasoning import _is_reasoning_model
        from providers import get_adapter
        # Production resolves the adapter from the routed model; tests do the same.
        return _is_reasoning_model(model, extra or [], adapter=get_adapter(model, _PROVIDERS_CONFIG))

    def test_o1_is_reasoning(self):
        assert self._check("o1-mini") is True

    def test_o3_is_reasoning(self):
        assert self._check("o3") is True

    def test_o4_is_reasoning(self):
        assert self._check("o4-mini") is True

    def test_claude_is_reasoning(self):
        assert self._check("claude-sonnet-4-5") is True

    def test_gpt4o_is_not_reasoning(self):
        assert self._check("gpt-4o") is False

    def test_gpt35_is_not_reasoning(self):
        assert self._check("gpt-3.5-turbo") is False

    def test_custom_prefix_via_extra(self):
        assert self._check("my-reasoning-model", extra=["my-"]) is True

    def test_extra_prefixes_not_applied_to_unrelated(self):
        assert self._check("gpt-4o", extra=["my-"]) is False


# ─── _extract_user_text ─────────────────────────────────────────────────────

class TestExtractUserText:

    def _extract(self, messages):
        from middleware.g25_adaptive_reasoning import _extract_user_text
        return _extract_user_text(messages)

    def test_extracts_user_content(self):
        msgs = [{"role": "user", "content": "hello world"}]
        assert "hello world" in self._extract(msgs)

    def test_extracts_system_content(self):
        msgs = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]
        text = self._extract(msgs)
        assert "You are helpful." in text
        assert "hi" in text

    def test_skips_assistant_content(self):
        msgs = [
            {"role": "assistant", "content": "previous turn"},
            {"role": "user", "content": "new question"},
        ]
        text = self._extract(msgs)
        assert "previous turn" not in text
        assert "new question" in text

    def test_non_string_content_skipped(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        # Should not raise; non-string content silently skipped
        text = self._extract(msgs)
        assert text == ""


# ─── G25AdaptiveReasoning.process_request ───────────────────────────────────

class TestG25ProcessRequest:

    @pytest.mark.asyncio
    async def test_disabled_skips(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        ctx = _make_ctx(enabled=False, model="o1-mini")
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert "reasoning_effort" not in ctx.params
        assert len(ctx.savings.step_savings) == 0

    @pytest.mark.asyncio
    async def test_non_reasoning_model_skips(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        ctx = _make_ctx(model="gpt-4o", enabled=True)
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert "reasoning_effort" not in ctx.params

    @pytest.mark.asyncio
    async def test_already_set_effort_skips(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        ctx = _make_ctx(model="o1-mini", params={"reasoning_effort": "high"})
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        # Must not change an externally-set effort
        assert ctx.params["reasoning_effort"] == "high"
        assert len(ctx.savings.step_savings) == 0

    @pytest.mark.asyncio
    async def test_high_complexity_sets_high_effort(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "Prove that the halting problem is undecidable using Turing reduction."}]
        ctx = _make_ctx(messages=msgs, model="o3-mini")
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_medium_complexity_sets_medium_effort(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "Explain why database indexes speed up SELECT queries."}]
        ctx = _make_ctx(messages=msgs, model="o1-mini")
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_low_complexity_sets_low_effort(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "What is the capital of Japan?"}]
        ctx = _make_ctx(messages=msgs, model="o1-mini")
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_savings_step_recorded(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "What is Python?"}]
        ctx = _make_ctx(messages=msgs, model="o1-mini")
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert len(ctx.savings.step_savings) == 1
        assert ctx.savings.step_savings[0].group == "G25"

    @pytest.mark.asyncio
    async def test_effort_floor_clamps_low_to_medium(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "What is 2 + 2?"}]
        ctx = _make_ctx(messages=msgs, model="o1-mini", cfg_extra={"effort_floor": "medium"})
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_effort_ceiling_clamps_high_to_medium(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "Prove the Riemann hypothesis using dynamic programming."}]
        ctx = _make_ctx(messages=msgs, model="o1-mini", cfg_extra={"effort_ceiling": "medium"})
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_custom_high_keywords_via_config(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "Please quant-optimise this trading strategy."}]
        ctx = _make_ctx(
            messages=msgs, model="o1-mini",
            cfg_extra={"high_keywords": [r"\bquant-optimise\b"]},
        )
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_langfuse_span_added(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        import middleware.g25_adaptive_reasoning as mod
        msgs = [{"role": "user", "content": "Summarise this text."}]
        ctx = _make_ctx(messages=msgs, model="o3")

        with patch.object(mod, "langfuse_tracing") as mock_lf:
            ctx = await G25AdaptiveReasoning().process_request(ctx)
            mock_lf.add_span.assert_called_once()
            call_kwargs = mock_lf.add_span.call_args[1]
            assert call_kwargs["name"] == "G25-adaptive-reasoning"
            assert "effort" in call_kwargs["output"]

    @pytest.mark.asyncio
    async def test_pattern_cache_invalidates_on_config_change(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        g25 = G25AdaptiveReasoning()
        cfg1 = {"enabled": True, "high_keywords": [r"\bfoo\b"]}
        cfg2 = {"enabled": True, "high_keywords": [r"\bbar\b"]}

        h1, _, _ = g25._get_patterns(cfg1)
        h2, _, _ = g25._get_patterns(cfg2)
        # Different keyword configs → must produce different pattern lists
        assert h1 is not h2

        # Same config requested twice in a row → patterns returned from cache (same object)
        h2b, _, _ = g25._get_patterns(cfg2)
        assert h2b is h2

    @pytest.mark.asyncio
    async def test_claude_model_also_classified(self):
        from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
        msgs = [{"role": "user", "content": "What is the time complexity of merge sort?"}]
        ctx = _make_ctx(messages=msgs, model="claude-sonnet-4-5")
        ctx = await G25AdaptiveReasoning().process_request(ctx)
        assert ctx.params.get("reasoning_effort") in ("high", "medium", "low")
