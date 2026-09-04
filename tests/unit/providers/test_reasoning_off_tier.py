"""The `off` reasoning tier, and why it cannot be one uniform thing (backlog #42).

Measured on DS8 2026-09-04: the all-on Anthropic block billed 4,869 reasoning tokens --
61% of the output bill, 41% of the total -- for answers no longer than the non-reasoning
arm delivered, on a workload that is FAQ lookup against a fixed policy document. The cause
was not the tier being too high (observed usage was ~157 tokens against a 5,000 budget); it
was that EVERY tier, including `low`, returned an enabling parameter. There was no way to
say a request needs no reasoning at all.

The fix cannot be uniform, because the providers are not:

  * Anthropic -- extended thinking is OPT-IN; omitting `thinking` is its own default.
  * Gemini    -- thinking is on by default; `thinking_budget: 0` disables it explicitly.
  * OpenAI    -- reasoning is intrinsic to the o-series. Omitting `reasoning_effort` picks
                 the model's default. It does NOT turn anything off.

So `off` promises "do not opt this request into optional reasoning", never "zero reasoning
tokens", and `can_disable_reasoning()` is what stops us claiming the stronger thing.
"""
import pytest

from providers import REASONING_OFF, REASONING_TIERS
from providers.anthropic_adapter import AnthropicAdapter
from providers.gemini_adapter import GeminiAdapter
from providers.generic_adapter import GenericLiteLLMAdapter
from providers.openai_adapter import OpenAIAdapter

_CFG = {"groups": {"G12_reasoning": {"effort_map": {
    "low": {"openai": "low", "anthropic_tokens": 1000, "gemini_thinking_budget": 512},
    "medium": {"openai": "medium", "anthropic_tokens": 5000, "gemini_thinking_budget": 4096},
}}}}


class TestOffIsRealisedPerProvider:
    def test_anthropic_omits_thinking_entirely(self):
        """Omission IS Anthropic's off switch -- its default is no extended thinking."""
        assert AnthropicAdapter().map_reasoning_effort(REASONING_OFF, _CFG) == {}

    def test_gemini_sends_an_explicit_zero_budget(self):
        """Gemini differs from Anthropic: thinking is ON by default, so omitting the
        param would leave it on. It needs an explicit 0."""
        assert GeminiAdapter().map_reasoning_effort(REASONING_OFF, _CFG) == {
            "thinking_config": {"thinking_budget": 0}}

    def test_openai_omits_the_effort_string(self):
        assert OpenAIAdapter().map_reasoning_effort(REASONING_OFF, _CFG) == {}

    def test_generic_sends_nothing(self):
        assert GenericLiteLLMAdapter("acme", {}).map_reasoning_effort(REASONING_OFF, _CFG) == {}


class TestWhoCanActuallyHonourIt:
    """This is the honesty gate -- it decides whether a saving may be claimed."""

    def test_anthropic_and_gemini_can(self):
        assert AnthropicAdapter().can_disable_reasoning("claude-sonnet-4-5") is True
        assert GeminiAdapter().can_disable_reasoning("gemini-2.5-pro") is True

    def test_openai_o_series_cannot(self):
        assert OpenAIAdapter().can_disable_reasoning("o4-mini") is False

    def test_a_non_reasoning_openai_model_trivially_can(self):
        """Nothing to disable, so True is the honest answer, not a claim."""
        assert OpenAIAdapter().can_disable_reasoning("gpt-4o-mini") is True

    def test_generic_defaults_to_cannot(self):
        """Under-claim for an unknown endpoint rather than credit ourselves."""
        assert GenericLiteLLMAdapter("acme", {}).can_disable_reasoning("x") is False
        assert GenericLiteLLMAdapter(
            "acme", {"can_disable_reasoning": True}).can_disable_reasoning("x") is True


class TestAnUnknownTierMustNeverEnableReasoning:
    """The pre-#42 behaviour, and the reason `off` could not be done in config alone.

    `tier_cfg.get("anthropic_tokens", _THINKING_DEFAULTS.get(tier, 1024))` meant an
    unrecognised tier produced a 1024-token thinking budget -- so a config typo, or the
    portal's fictional `minimal`, silently turned reasoning ON.
    """

    @pytest.mark.parametrize("tier", ["minimal", "none", "OFF ", "turbo", "", None])
    def test_anthropic_emits_nothing(self, tier):
        assert AnthropicAdapter().map_reasoning_effort(tier, _CFG) == {}

    @pytest.mark.parametrize("tier", ["minimal", "turbo", ""])
    def test_gemini_emits_nothing(self, tier):
        assert GeminiAdapter().map_reasoning_effort(tier, _CFG) == {}

    @pytest.mark.parametrize("tier", ["minimal", "turbo", ""])
    def test_openai_does_not_forward_an_invalid_enum(self, tier):
        """It used to echo the tier string straight through, so `minimal` reached the
        o-series as an invalid `reasoning_effort` and 400'd."""
        assert OpenAIAdapter().map_reasoning_effort(tier, _CFG) == {}

    def test_a_zero_or_negative_anthropic_budget_is_off_not_a_400(self):
        cfg = {"groups": {"G12_reasoning": {"effort_map": {"low": {"anthropic_tokens": 0}}}}}
        assert AnthropicAdapter().map_reasoning_effort("low", cfg) == {}

    def test_a_junk_budget_does_not_raise(self):
        cfg = {"groups": {"G12_reasoning": {"effort_map": {"low": {"anthropic_tokens": "lots"}}}}}
        assert AnthropicAdapter().map_reasoning_effort("low", cfg) == {}


class TestKnownTiersAreUnchanged:
    """`off` must be purely additive -- every shipped tier keeps its exact behaviour."""

    def test_anthropic(self):
        assert AnthropicAdapter().map_reasoning_effort("medium", _CFG) == {
            "thinking": {"type": "enabled", "budget_tokens": 5000}}

    def test_gemini(self):
        assert GeminiAdapter().map_reasoning_effort("low", _CFG) == {
            "thinking_config": {"thinking_budget": 512}}

    def test_openai(self):
        assert OpenAIAdapter().map_reasoning_effort("low", _CFG) == {"reasoning_effort": "low"}

    def test_off_is_the_lowest_rung_not_a_sentinel(self):
        assert REASONING_TIERS[0] == REASONING_OFF
        assert list(REASONING_TIERS) == ["off", "low", "medium", "high"]


class TestGeminiThinkingConfigIsStrippable:
    def test_thinking_config_is_a_reasoning_param_key(self):
        """It was missing, so on a G06 downgrade to a non-reasoning model Gemini's
        thinking param was never stripped from the outgoing request."""
        assert "thinking_config" in GeminiAdapter().reasoning_param_keys()


class TestConfigDrivenReasoningModels:
    """The only way to express 'my simple tier must not think' on a provider where
    reasoning is a request param: every Claude model reports reasoning-capable."""

    def test_default_is_unchanged(self):
        assert AnthropicAdapter().supports_reasoning("claude-haiku-4-5") is True
        assert AnthropicAdapter().supports_reasoning("claude-haiku-4-5", {}) is True

    def test_a_list_narrows_it(self):
        cfg = {"providers": [{"name": "anthropic",
                              "reasoning_models": ["claude-sonnet-4-5"]}]}
        a = AnthropicAdapter()
        assert a.supports_reasoning("claude-sonnet-4-5", cfg) is True
        assert a.supports_reasoning("claude-haiku-4-5", cfg) is False

    def test_dict_shaped_providers_block_also_works(self):
        cfg = {"providers": {"anthropic": {"reasoning_models": ["claude-sonnet-4-5"]}}}
        assert AnthropicAdapter().supports_reasoning("claude-haiku-4-5", cfg) is False

    def test_a_list_never_widens_openai(self):
        """gpt-4o rejects reasoning_effort outright; config must not be able to force it."""
        cfg = {"providers": [{"name": "openai", "reasoning_models": ["gpt-4o"]}]}
        assert OpenAIAdapter().supports_reasoning("gpt-4o", cfg) is False

    def test_a_malformed_providers_block_is_ignored(self):
        for bad in ({"providers": "nope"}, {"providers": [None, 3]}, {"providers": None}, None):
            assert AnthropicAdapter().supports_reasoning("claude-x", bad) is True
