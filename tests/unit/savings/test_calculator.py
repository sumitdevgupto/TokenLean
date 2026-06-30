"""Unit tests for savings/calculator.py."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from savings.calculator import (
    estimate_tokens,
    count_messages_tokens,
    estimate_cost,
    estimate_cost_with_cache,
    effective_token_cost,
    get_cost_per_1k,
    messages_to_text,
)


class TestEstimateTokens:
    def test_empty_string_returns_zero(self):
        assert estimate_tokens("", model="gpt-4o") == 0

    def test_non_empty_returns_positive(self):
        assert estimate_tokens("Hello world", model="gpt-4o") > 0

    def test_fallback_approx_four_chars_per_token(self):
        # Using Gemini (non-GPT) forces fallback: ceil(len/4)
        text = "a" * 40
        result = estimate_tokens(text, model="gemini-2.0-flash")
        assert result == 10  # 40 chars / 4 = 10

    def test_longer_text_more_tokens(self):
        short = estimate_tokens("Hi", model="gemini-pro")
        long_t = estimate_tokens("Hi " * 100, model="gemini-pro")
        assert long_t > short

    def test_gpt_model_uses_tiktoken_or_fallback(self):
        result = estimate_tokens("The quick brown fox", model="gpt-4o")
        assert isinstance(result, int) and result > 0

    def test_unknown_model_uses_fallback(self):
        result = estimate_tokens("Hello", model="unknown-model-xyz")
        assert result > 0


class TestNonGptTiktokenFallback:
    """B2 — config-gated cl100k_base fallback for non-GPT models (default OFF)."""

    def test_default_off_uses_char_div_4(self):
        # No config flag set → char/4 (40 chars / 4 = 10), unchanged behaviour.
        assert estimate_tokens("a" * 40, model="gemini-2.0-flash") == 10

    def test_enabled_uses_local_tiktoken_for_non_gpt(self, monkeypatch):
        import savings.calculator as calc
        if not calc._TIKTOKEN_AVAILABLE:
            pytest.skip("tiktoken not installed")
        monkeypatch.setattr(calc, "_non_gpt_tiktoken_fallback", lambda: True)
        text = "The quick brown fox jumps over the lazy dog. " * 5
        char_div_4 = max(1, (len(text) + 3) // 4)
        result = estimate_tokens(text, model="claude-sonnet-4-5")
        # A real tokenizer count, distinct from the naive char/4 estimate.
        assert result > 0
        assert result != char_div_4

    def test_enabled_still_falls_back_to_char_when_tiktoken_unavailable(self, monkeypatch):
        import savings.calculator as calc
        monkeypatch.setattr(calc, "_TIKTOKEN_AVAILABLE", False)
        monkeypatch.setattr(calc, "_non_gpt_tiktoken_fallback", lambda: True)
        assert estimate_tokens("a" * 40, model="claude-3-5-sonnet") == 10


class TestCountMessagesTokens:
    def test_single_message(self):
        msgs = [{"role": "user", "content": "Hello"}]
        result = count_messages_tokens(msgs, model="gemini-pro")
        assert result > 0

    def test_adds_four_overhead_per_message(self):
        # Two messages should have 2×4=8 overhead tokens added
        msgs_1 = [{"role": "user", "content": "x"}]
        msgs_2 = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "x"}]
        single = count_messages_tokens(msgs_1, model="gemini-pro")
        double = count_messages_tokens(msgs_2, model="gemini-pro")
        # double should have at least 4 more tokens (overhead from second message)
        assert double > single

    def test_multipart_content_counted(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "Hello world"}]}]
        result = count_messages_tokens(msgs, model="gemini-pro")
        assert result > 0

    def test_empty_messages_returns_zero(self):
        assert count_messages_tokens([], model="gpt-4o") == 0

    def test_role_contributes_tokens(self):
        msgs_short = [{"role": "u", "content": "x"}]
        msgs_long = [{"role": "system", "content": "x"}]
        # system > u in length → more tokens
        assert count_messages_tokens(msgs_long, "gemini-pro") >= count_messages_tokens(msgs_short, "gemini-pro")


class TestEstimateCost:
    def test_gpt4o_mini_lower_than_gpt4o(self):
        cost_mini = estimate_cost(1000, 200, "gpt-4o-mini")
        cost_full = estimate_cost(1000, 200, "gpt-4o")
        assert cost_mini <= cost_full

    def test_zero_tokens_zero_cost(self):
        assert estimate_cost(0, 0, "gpt-4o") == 0.0

    def test_positive_cost_for_nonzero_tokens(self):
        assert estimate_cost(100, 50, "gpt-4o-mini") > 0.0

    def test_unknown_model_uses_default_rates(self):
        cost_unknown = estimate_cost(1000, 100, "unknown-model-xyz")
        cost_default = estimate_cost(1000, 100, "default")
        # Both use default rates → identical
        assert cost_unknown == cost_default

    def test_partial_model_name_matches(self):
        # "gpt-4o-mini-2024-07-18" should match "gpt-4o-mini" rates
        c1 = estimate_cost(1000, 100, "gpt-4o-mini-2024-07-18")
        c2 = estimate_cost(1000, 100, "gpt-4o-mini")
        assert c1 == c2

    def test_output_tokens_cost_more_than_input(self):
        # For GPT-4o: output = $0.015/1k, input = $0.005/1k
        cost_input_heavy = estimate_cost(1000, 10, "gpt-4o")
        cost_output_heavy = estimate_cost(10, 1000, "gpt-4o")
        assert cost_output_heavy > cost_input_heavy


class TestEstimateCostWithCache:
    def test_no_cached_tokens_equals_estimate_cost(self):
        # Safe drop-in: cached=0 must be byte-identical to estimate_cost.
        for model in ("gpt-4o", "gpt-4o-mini", "default"):
            assert estimate_cost_with_cache(100, 0, 20, model, 0.5) == estimate_cost(100, 20, model)

    def test_multiplier_one_equals_estimate_cost_even_with_cache(self):
        # mult=1.0 → no discount, even when cached>0.
        assert estimate_cost_with_cache(100, 80, 20, "gpt-4o", 1.0) == estimate_cost(100, 20, "gpt-4o")

    def test_cached_tokens_reduce_cost(self):
        full = estimate_cost(100, 20, "gpt-4o")
        discounted = estimate_cost_with_cache(100, 80, 20, "gpt-4o", 0.5)
        assert discounted < full

    def test_cached_clamped_to_input(self):
        # cached > input must not over-credit / go below the fully-cached floor.
        floor = estimate_cost_with_cache(100, 100, 20, "gpt-4o", 0.1)
        over = estimate_cost_with_cache(100, 999, 20, "gpt-4o", 0.1)
        assert over == floor

    def test_lower_multiplier_cheaper(self):
        anthropic_like = estimate_cost_with_cache(100, 100, 20, "gpt-4o", 0.1)
        openai_like = estimate_cost_with_cache(100, 100, 20, "gpt-4o", 0.5)
        assert anthropic_like < openai_like

    # ── B3: discount-aware price book ───────────────────────────────────────────

    def test_new_kwargs_default_to_existing_behaviour(self):
        # Safe drop-in: explicit defaults must equal the legacy call.
        for model in ("gpt-4o", "gpt-4o-mini", "default"):
            assert estimate_cost_with_cache(100, 80, 20, model, 0.5) == estimate_cost_with_cache(
                100, 80, 20, model, 0.5,
                batch_discount=1.0, reasoning_tokens=0, reasoning_rate_multiplier=1.0,
            )

    def test_batch_discount_halves_cost(self):
        full = estimate_cost_with_cache(100, 0, 20, "gpt-4o")
        batched = estimate_cost_with_cache(100, 0, 20, "gpt-4o", batch_discount=0.5)
        assert batched == pytest.approx(full * 0.5)

    def test_reasoning_default_no_surcharge(self):
        base = estimate_cost_with_cache(100, 0, 50, "gpt-4o")
        # reasoning_rate_multiplier defaults to 1.0 → reasoning_tokens add nothing.
        same = estimate_cost_with_cache(100, 0, 50, "gpt-4o", reasoning_tokens=20)
        assert same == base

    def test_reasoning_surcharge_adds_delta_only(self):
        base = estimate_cost_with_cache(100, 0, 50, "gpt-4o")
        surcharged = estimate_cost_with_cache(
            100, 0, 50, "gpt-4o", reasoning_tokens=20, reasoning_rate_multiplier=2.0
        )
        _, out_cost = get_cost_per_1k("gpt-4o")
        # Only the delta above the standard output rate is added (mult − 1 = 1.0).
        expected = round(base + 20 / 1000.0 * out_cost * 1.0, 8)
        assert surcharged == pytest.approx(expected)

    def test_reasoning_clamped_to_output(self):
        a = estimate_cost_with_cache(100, 0, 50, "gpt-4o", reasoning_tokens=999, reasoning_rate_multiplier=2.0)
        b = estimate_cost_with_cache(100, 0, 50, "gpt-4o", reasoning_tokens=50, reasoning_rate_multiplier=2.0)
        assert a == b


class TestEffectiveTokenCost:
    def test_basic_formula(self):
        # ET = 1.0 × input + 0.1 × cache + 4.0 × output
        result = effective_token_cost(100, 50, 25)
        expected = 1.0 * 100 + 0.1 * 50 + 4.0 * 25
        assert abs(result - expected) < 1e-9

    def test_zero_inputs_zero_et(self):
        assert effective_token_cost(0, 0, 0) == 0.0

    def test_cache_weighted_less_than_input(self):
        et_input = effective_token_cost(100, 0, 0)
        et_cache = effective_token_cost(0, 100, 0)
        assert et_input > et_cache

    def test_output_weighted_most(self):
        et_input = effective_token_cost(100, 0, 0)
        et_output = effective_token_cost(0, 0, 100)
        assert et_output > et_input

    def test_model_multiplier_scales_result(self):
        base = effective_token_cost(100, 0, 50)
        doubled = effective_token_cost(100, 0, 50, model_multiplier=2.0)
        assert abs(doubled - 2 * base) < 1e-9


class TestGetCostPer1k:
    def test_known_model(self):
        inp, out = get_cost_per_1k("gpt-4o-mini")
        assert inp > 0 and out > 0

    def test_unknown_model_returns_default(self):
        inp, out = get_cost_per_1k("nonexistent-model")
        default_inp, default_out = get_cost_per_1k("default")
        # Unknown falls back to default
        assert inp == default_inp
        assert out == default_out


class TestMessagesToText:
    def test_flattens_to_string(self):
        msgs = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "World"}]
        result = messages_to_text(msgs)
        assert "user" in result
        assert "Hello" in result
        assert "assistant" in result
        assert "World" in result

    def test_handles_list_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]
        result = messages_to_text(msgs)
        assert "Hello" in result
