"""Unit tests for savings/calculator.py."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from savings.calculator import (
    estimate_tokens,
    count_messages_tokens,
    count_tools_tokens,
    count_request_tokens,
    estimate_cost,
    estimate_cost_with_cache,
    cache_cost_split,
    effective_token_cost,
    get_cost_per_1k,
    messages_to_text,
    _render_tool_signature,
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


class TestCountToolsTokens:
    """Packed-signature tool counting (2026-08-08 fix).

    Counting raw json.dumps overestimated tool tokens ~2.4-2.8x vs provider billing
    (measured live on DS13: 755 estimated vs ~267 billed for 11 tools), inflating
    baselines on every tool-bearing dataset and G16's recorded savings. Tools are now
    rendered in OpenAI's packed TypeScript-namespace form and counted as text.
    """

    _TOOL = {
        "type": "function",
        "function": {
            "name": "get_service_health",
            "description": "Return the health status of a deployed service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service name."},
                    "verbose": {"type": "boolean"},
                    "env": {"type": "string", "enum": ["dev", "staging", "prod"]},
                    "limits": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["service"],
            },
        },
    }

    def test_empty_tools_zero(self):
        assert count_tools_tokens([], "gpt-4o-mini") == 0
        assert count_tools_tokens(None, "gpt-4o-mini") == 0

    def test_packed_render_shape(self):
        sig = _render_tool_signature(self._TOOL["function"])
        assert "// Return the health status" in sig            # descriptions kept
        assert "type get_service_health = (_: {" in sig        # TS-signature form
        assert "service: string," in sig                       # required → no '?'
        assert "verbose?: boolean," in sig                     # optional → '?'
        assert '"dev" | "staging" | "prod"' in sig             # enum → union
        assert "number[]," in sig                              # array<int> → number[]
        assert "{" in sig and '"parameters"' not in sig        # raw JSON schema NOT counted

    def test_packed_is_far_below_raw_json(self):
        import json as _json
        raw = estimate_tokens(_json.dumps(self._TOOL), "gpt-4o-mini")
        packed = count_tools_tokens([self._TOOL], "gpt-4o-mini")
        assert packed < raw * 0.75, f"packed {packed} should be well under raw {raw}"

    def test_no_parameters_function(self):
        tool = {"type": "function", "function": {"name": "ping", "description": "Ping."}}
        n = count_tools_tokens([tool], "gpt-4o-mini")
        assert 0 < n < 40
        assert "type ping = () => any;" in _render_tool_signature(tool["function"])

    def test_non_function_tool_falls_back_to_json(self):
        weird = {"type": "retrieval", "config": {"index": "docs"}}
        assert count_tools_tokens([weird], "gpt-4o-mini") > 0

    def test_count_request_tokens_includes_tools(self):
        msgs = [{"role": "user", "content": "check the api please"}]
        with_tools = count_request_tokens(msgs, "gpt-4o-mini", [self._TOOL])
        without = count_request_tokens(msgs, "gpt-4o-mini")
        assert with_tools > without

    def test_g16_tools_tokens_delegates(self):
        """g16's helper and the calculator must agree — one estimator, one truth."""
        from middleware.g16_agent_arch import _tools_tokens
        assert _tools_tokens([self._TOOL], "gpt-4o-mini") == count_tools_tokens(
            [self._TOOL], "gpt-4o-mini")

    def test_ds13_magnitude_when_dataset_present(self):
        """Anchor to the live measurement: DS13's 11 tools billed ~267-298 tokens.
        The packed estimate must land within +/-40% of that (raw JSON was +180%)."""
        import json as _json, os as _os
        path = _os.path.join(_os.path.dirname(__file__), "..", "..", "..",
                             "pitch-test-plan", "datasets", "DS13", "requests.jsonl")
        if not _os.path.exists(path):
            pytest.skip("DS13 dataset not present in this checkout (commercial-only)")
        with open(path, encoding="utf-8") as fh:
            req = _json.loads(fh.readline())
        est = count_tools_tokens(req["params"]["tools"], "gpt-4o-mini")
        assert 160 <= est <= 420, f"tool estimate {est} drifted from the billed ~267-298"


class TestMalformedToolSchemas:
    """Review S1: malformed-but-JSON schemas the old json.dumps path tolerated must
    never crash — count_tools_tokens runs at RequestContext creation, OUTSIDE the
    pipeline try, so an exception here is a raw 500."""

    @pytest.mark.parametrize("params", [
        ["a"],                       # parameters as a list
        "not-a-dict",                # parameters as a string
        {"properties": ["x"]},       # properties as a list
        {"properties": {"p": {"enum": 5}}},        # enum as an int
        {"properties": {"p": {"enum": None}}},     # enum as None
        {"properties": {"p": {"enum": {"a": 1}}}}, # enum as a dict
        {"required": "service"},     # required as a string
    ])
    def test_malformed_schema_never_crashes(self, params):
        tool = {"type": "function", "function": {"name": "f", "parameters": params}}
        assert count_tools_tokens([tool], "gpt-4o-mini") > 0

    def test_unserializable_tool_never_crashes(self):
        assert count_tools_tokens([{"type": "x", "blob": object()}], "gpt-4o-mini") > 0

    def test_g08_delegates_to_shared_estimator(self):
        """Review S8: G08 and G16 must count tools with the SAME estimator as the
        baseline, or per-group savings can exceed the tools' entire contribution."""
        import middleware.g08_tool_loading as g08
        import savings.calculator as calc
        assert g08.count_tools_tokens is calc.count_tools_tokens


class TestCacheWriteCostAccounting:
    """#34 — cache-write pricing, and the accounting trap that had to be verified first.

    Anthropic reports input_tokens / cache_read_input_tokens / cache_creation_input_tokens
    as SIBLINGS, but litellm folds the two cache counts INTO prompt_tokens before the proxy
    sees them. These fixtures pin that: if a future litellm release stops folding, the
    subset assumption breaks and cost is silently misreported, so these must fail loudly.
    """

    # Captured usage shapes, one per provider (post-litellm normalisation).
    OPENAI_USAGE = {"prompt_tokens": 1000, "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 800}}
    ANTHROPIC_USAGE = {"prompt_tokens": 1000, "completion_tokens": 100,
                       "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 300}}
    GEMINI_USAGE = {"prompt_tokens": 1000, "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 500}}

    def test_defaults_are_a_byte_identical_drop_in(self):
        model = "gpt-4o"
        assert estimate_cost_with_cache(1000, 0, 100, model) == estimate_cost(1000, 100, model)

    def test_cache_spans_are_subsets_not_additions(self):
        """Reads + writes + plain input must partition prompt_tokens, never exceed it."""
        model = "gpt-4o"
        u = self.ANTHROPIC_USAGE
        cost = estimate_cost_with_cache(
            u["prompt_tokens"], u["prompt_tokens_details"]["cached_tokens"],
            u["completion_tokens"], model, 0.1,
            cache_write_tokens=u["prompt_tokens_details"]["cache_write_tokens"],
            cache_write_multiplier=1.25,
        )
        # Every input token billed at full rate is the ceiling; discounted reads must
        # make the real figure strictly cheaper, proving they were not double-counted.
        assert cost < estimate_cost(u["prompt_tokens"], u["completion_tokens"], model)

    def test_writes_cost_more_than_reads(self):
        model = "gpt-4o"
        reads = estimate_cost_with_cache(1000, 500, 100, model, 0.1)
        writes = estimate_cost_with_cache(1000, 0, 100, model, 0.1,
                                          cache_write_tokens=500, cache_write_multiplier=1.25)
        assert writes > reads, "a cache write must never look cheaper than a cache read"

    def test_1h_tier_is_dearer_than_5m_tier(self):
        model = "gpt-4o"
        base = dict(cache_write_tokens=400, cache_write_multiplier=1.25)
        five_m = estimate_cost_with_cache(1000, 0, 100, model, 0.1, **base)
        one_h = estimate_cost_with_cache(1000, 0, 100, model, 0.1,
                                         cache_write_1h_tokens=400, cache_write_1h_multiplier=2.0,
                                         **base)
        assert one_h > five_m

    def test_overrunning_counts_cannot_double_bill(self):
        """A provider reporting more cache tokens than input must not inflate the bill."""
        model = "gpt-4o"
        cost = estimate_cost_with_cache(1000, 900, 100, model, 0.1,
                                        cache_write_tokens=900, cache_write_multiplier=1.25)
        assert cost <= estimate_cost(1000, 100, model)

    def test_split_components_never_exceed_the_total(self):
        model = "gpt-4o"
        kw = dict(cache_write_tokens=300, cache_write_multiplier=1.25,
                  cache_write_1h_tokens=100, cache_write_1h_multiplier=2.0)
        total = estimate_cost_with_cache(1000, 200, 100, model, 0.1, **kw)
        read_usd, write_usd = cache_cost_split(1000, 200, model, 0.1, **kw)
        assert read_usd + write_usd <= total + 1e-9
        assert write_usd > read_usd  # discounted reads, premium writes

    def test_split_is_zero_when_no_cache_activity(self):
        assert cache_cost_split(1000, 0, "gpt-4o", 0.5) == (0.0, 0.0)

    def test_batch_discount_applies_to_both_halves(self):
        model = "gpt-4o"
        kw = dict(cache_write_tokens=300, cache_write_multiplier=1.25)
        full = cache_cost_split(1000, 200, model, 0.1, **kw)
        half = cache_cost_split(1000, 200, model, 0.1, batch_discount=0.5, **kw)
        assert half[0] == pytest.approx(full[0] / 2)
        assert half[1] == pytest.approx(full[1] / 2)

