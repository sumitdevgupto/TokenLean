"""Tests for the 10-provider refactor: open registry, build_call routing, generic adapter
(modes A/B), param hygiene, usage normalization, and ambient-credential auth.
"""
import pytest

from providers import (
    _REGISTRY,
    get_adapter,
    get_adapter_by_name,
    get_provider_entry,
    build_litellm_call,
    register_adapter,
    UnsupportedProviderError,
)
from providers.generic_adapter import GenericLiteLLMAdapter
from providers.openai_adapter import OpenAIAdapter
from providers.anthropic_adapter import AnthropicAdapter
from providers.gemini_adapter import GeminiAdapter
from providers.bedrock_adapter import BedrockAdapter
from providers.azure_adapter import AzureOpenAIAdapter


TEN = ["openai", "anthropic", "gemini", "azure", "bedrock",
       "mistral", "cohere", "xai", "deepseek", "groq"]


class TestRegistry:
    def test_all_ten_registered(self):
        for name in TEN:
            assert name in _REGISTRY, f"{name} not auto-discovered into the registry"

    def test_unknown_name_raises(self):
        with pytest.raises(UnsupportedProviderError):
            get_adapter_by_name("definitely-not-a-provider")

    def test_register_adapter_decorator_adds_to_registry(self):
        @register_adapter("unit-test-temp")
        class _Tmp(GenericLiteLLMAdapter):
            PROVIDER_NAME = "unit-test-temp"

        try:
            assert isinstance(get_adapter_by_name("unit-test-temp"), _Tmp)
        finally:
            _REGISTRY.pop("unit-test-temp", None)


class TestBuildCallRouting:
    @pytest.mark.parametrize("name,model,expect_model", [
        ("mistral", "mistral-large-latest", "mistral/mistral-large-latest"),
        ("deepseek", "deepseek-chat", "deepseek/deepseek-chat"),
        ("xai", "grok-2", "xai/grok-2"),
        ("groq", "groq/llama-3.3-70b", "groq/llama-3.3-70b"),  # already prefixed → unchanged
        ("cohere", "command-r", "cohere/command-r"),
    ])
    def test_litellm_prefix_providers(self, name, model, expect_model):
        adapter = get_adapter_by_name(name)
        m, kw = adapter.build_call(model, {"name": name}, "KEY")
        assert m == expect_model
        assert kw.get("api_key") == "KEY"
        assert "base_url" not in kw

    def test_openai_default_is_byte_identical(self):
        m, kw = OpenAIAdapter().build_call("gpt-4o-mini", {"name": "openai"}, "sk")
        assert m == "gpt-4o-mini"
        assert kw == {"api_key": "sk"}  # no api_base/custom_llm_provider for native OpenAI

    def test_azure_routing(self):
        cfg = {"name": "azure", "api_base": "https://r.openai.azure.com", "api_version": "2024-10-21"}
        m, kw = AzureOpenAIAdapter().build_call("my-deploy", cfg, "sk")
        assert m == "azure/my-deploy"
        assert kw["custom_llm_provider"] == "azure"
        assert kw["api_base"] == "https://r.openai.azure.com"
        assert kw["api_version"] == "2024-10-21"
        assert kw["api_key"] == "sk"

    def test_gemini_uses_gemini_prefix(self):
        # bare gemini-* makes litellm try Vertex (ADC); the adapter forces the API-key path.
        m, kw = get_adapter_by_name("gemini").build_call("gemini-2.5-flash", {"name": "gemini"}, "K")
        assert m == "gemini/gemini-2.5-flash"
        assert kw.get("api_key") == "K"

    def test_bedrock_routing_no_key(self):
        a = BedrockAdapter()
        assert a.requires_api_key() is False
        m, kw = a.build_call("anthropic.claude-3-5-sonnet", {"name": "bedrock", "aws_region": "us-east-1"}, None)
        assert m == "bedrock/anthropic.claude-3-5-sonnet"
        assert kw["custom_llm_provider"] == "bedrock"
        assert kw["aws_region_name"] == "us-east-1"
        assert "api_key" not in kw


class TestGenericAdapter:
    def test_mode_a_litellm_prefix(self):
        cfg = [{"name": "perplexity", "adapter": "generic",
                "litellm_prefix": "perplexity", "model_prefixes": ["sonar"]}]
        a = get_adapter("sonar-pro", cfg)
        assert isinstance(a, GenericLiteLLMAdapter)
        m, kw = build_litellm_call("sonar-pro", cfg, "K")
        assert m == "perplexity/sonar-pro"

    def test_mode_b_openai_compatible(self):
        cfg = [{"name": "kimi", "openai_compatible": True,
                "api_base": "https://api.moonshot.ai/v1", "model_prefixes": ["kimi"]}]
        a = get_adapter("kimi-k2", cfg)
        assert isinstance(a, GenericLiteLLMAdapter)
        m, kw = build_litellm_call("kimi-k2", cfg, "K")
        assert m == "kimi-k2"
        assert kw["base_url"] == "https://api.moonshot.ai/v1"
        assert kw["custom_llm_provider"] == "openai"

    def test_generic_reasoning_off_by_default(self):
        a = GenericLiteLLMAdapter("kimi", {})
        assert a.supports_reasoning("kimi-k2") is False
        assert a.map_reasoning_effort("high", {}) == {}

    def test_generic_reasoning_opt_in(self):
        a = GenericLiteLLMAdapter("kimi", {"supports_reasoning": True})
        assert a.supports_reasoning("kimi-k2") is True

    def test_route_prefix_stripped_in_openai_compatible(self):
        # OpenCode Zen: route by "opencode/" but send the bare model name upstream.
        cfg = [{"name": "opencode", "openai_compatible": True,
                "api_base": "https://opencode.ai/zen/v1", "route_prefix": "opencode/",
                "model_prefixes": ["opencode/"]}]
        m, kw = build_litellm_call("opencode/deepseek-v4-pro", cfg, "K")
        assert m == "deepseek-v4-pro"  # prefix stripped before the call
        assert kw["base_url"] == "https://opencode.ai/zen/v1"
        assert kw["custom_llm_provider"] == "openai"


def test_longest_match_pricing(monkeypatch):
    """get_cost_per_1k prefers the most specific (longest) matching key, so an OpenCode Zen
    model isn't mispriced as the generic 'deepseek' row."""
    import config_loader
    from savings import calculator
    monkeypatch.setattr(config_loader, "get_pricing_table", lambda: {
        "deepseek": {"input": 0.00027, "output": 0.0011},
        "opencode/deepseek-v4-pro": {"input": 0.00174, "output": 0.00348},
        "default": {"input": 0.005, "output": 0.015},
    })
    assert calculator.get_cost_per_1k("opencode/deepseek-v4-pro") == (0.00174, 0.00348)
    assert calculator.get_cost_per_1k("deepseek-chat") == (0.00027, 0.0011)


class TestProviderEntryResolution:
    def test_matched_prefix(self):
        cfg = [{"name": "mistral", "model_prefixes": ["mistral"]}]
        assert get_provider_entry("mistral-large", cfg)["name"] == "mistral"

    def test_unknown_model_returns_default_entry(self):
        # default_provider falls back to first provider when global config is unset
        cfg = [{"name": "openai", "model_prefixes": ["gpt"]}]
        entry = get_provider_entry("totally-unknown", cfg)
        assert entry is None or entry["name"] == "openai"


class TestParamHygiene:
    def test_openai_strips_thinking(self):
        assert "thinking" in OpenAIAdapter().unsupported_params()

    def test_anthropic_strips_openai_only(self):
        up = AnthropicAdapter().unsupported_params()
        assert {"parallel_tool_calls", "logprobs", "top_logprobs"} <= up

    def test_gemini_strips_thinking_and_openai_only(self):
        up = GeminiAdapter().unsupported_params()
        assert "thinking" in up and "parallel_tool_calls" in up


class TestUsageNormalization:
    def test_base_openai_shape(self):
        resp = {"usage": {"prompt_tokens_details": {"cached_tokens": 7},
                          "completion_tokens_details": {"reasoning_tokens": 3}}}
        assert OpenAIAdapter().extract_usage(resp) == {"cached_tokens": 7, "reasoning_tokens": 3}

    def test_anthropic_native_fields(self):
        resp = {"usage": {"cache_read_input_tokens": 11, "thinking_tokens": 5}}
        out = AnthropicAdapter().extract_usage(resp)
        assert out == {"cached_tokens": 11, "reasoning_tokens": 5}

    def test_gemini_native_fields(self):
        resp = {"usage": {"cached_content_token_count": 9}}
        out = GeminiAdapter().extract_usage(resp)
        assert out["cached_tokens"] == 9

    def test_prefers_normalised_openai_shape_when_present(self):
        # When litellm already normalised to the OpenAI shape, that wins.
        resp = {"usage": {"prompt_tokens_details": {"cached_tokens": 4},
                          "cache_read_input_tokens": 99}}
        assert AnthropicAdapter().extract_usage(resp)["cached_tokens"] == 4


class TestCapReasoningParams:
    def test_anthropic_drops_thinking_when_max_tokens_too_small(self):
        # max_tokens-1 < 1024 (Anthropic min budget) → thinking removed entirely
        p = AnthropicAdapter().cap_reasoning_params(
            {"thinking": {"type": "enabled", "budget_tokens": 5000}}, 100)
        assert "thinking" not in p

    def test_anthropic_caps_below_max_tokens(self):
        p = AnthropicAdapter().cap_reasoning_params(
            {"thinking": {"type": "enabled", "budget_tokens": 5000}}, 2000)
        assert p["thinking"]["budget_tokens"] == 1999

    def test_anthropic_noop_when_budget_already_below(self):
        p = AnthropicAdapter().cap_reasoning_params(
            {"thinking": {"type": "enabled", "budget_tokens": 500}}, 2000)
        assert p["thinking"]["budget_tokens"] == 500

    def test_gemini_caps_below_max_tokens(self):
        p = GeminiAdapter().cap_reasoning_params({"thinking_config": {"thinking_budget": 5000}}, 1000)
        assert p["thinking_config"]["thinking_budget"] == 999

    def test_openai_is_noop(self):
        p = OpenAIAdapter().cap_reasoning_params({"reasoning_effort": "high"}, 100)
        assert p == {"reasoning_effort": "high"}

    def test_no_max_tokens_is_noop(self):
        p = AnthropicAdapter().cap_reasoning_params({"thinking": {"budget_tokens": 5000}}, None)
        assert p["thinking"]["budget_tokens"] == 5000

    def test_anthropic_drops_reasoning_effort_when_max_tokens_too_small(self):
        # G25 sets reasoning_effort; with G12 disabled nothing maps it to `thinking`. A tiny
        # max_tokens can't fit the 1024 min budget → reasoning_effort must ALSO be dropped, or
        # litellm expands it into a conflicting thinking budget downstream (Anthropic 400s).
        p = AnthropicAdapter().cap_reasoning_params({"reasoning_effort": "high"}, 16)
        assert "reasoning_effort" not in p and "thinking" not in p

    def test_anthropic_keeps_reasoning_effort_when_max_tokens_fits(self):
        # Plenty of room for a thinking budget → leave reasoning_effort alone.
        p = AnthropicAdapter().cap_reasoning_params({"reasoning_effort": "high"}, 4096)
        assert p.get("reasoning_effort") == "high"


class TestAllTenProvidersRouteSmoke:
    @pytest.mark.parametrize("name,model,expect", [
        ("openai", "gpt-4o-mini", "gpt-4o-mini"),
        ("anthropic", "claude-haiku-4-5", "claude-haiku-4-5"),
        ("gemini", "gemini-2.5-flash", "gemini/gemini-2.5-flash"),
        ("mistral", "mistral-small-latest", "mistral/mistral-small-latest"),
        ("deepseek", "deepseek-chat", "deepseek/deepseek-chat"),
        ("xai", "grok-2", "xai/grok-2"),
        ("groq", "groq/llama-3.3-70b", "groq/llama-3.3-70b"),
        ("cohere", "command-r", "cohere/command-r"),
        ("azure", "dep", "azure/dep"),
        ("bedrock", "anthropic.claude-3-5-sonnet", "bedrock/anthropic.claude-3-5-sonnet"),
    ])
    def test_each_provider_routes(self, name, model, expect):
        cfg = {"name": name, "api_base": "https://x.example", "api_version": "2024-10-21"}
        m, _kw = get_adapter_by_name(name).build_call(model, cfg, "K")
        assert m == expect
