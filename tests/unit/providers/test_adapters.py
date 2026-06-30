"""Unit tests for the provider adapter layer (T35)."""
import pytest

from providers import (
    get_adapter,
    get_adapter_by_name,
    apply_context_management,
    UnsupportedProviderError,
    ProviderAdapter,
)
from providers.openai_adapter import OpenAIAdapter
from providers.anthropic_adapter import AnthropicAdapter
from providers.gemini_adapter import GeminiAdapter


# ---------------------------------------------------------------------------
# Context editing (Workstream C) — Anthropic native context management
# ---------------------------------------------------------------------------

class TestContextManagement:
    def test_anthropic_enabled_injects_field_and_beta(self):
        out = AnthropicAdapter().apply_context_management({}, {"enabled": True})
        assert out["context_management"] == {
            "edits": [{"type": "clear_tool_uses_20250919"}]
        }
        assert out["extra_headers"]["anthropic-beta"] == "context-management-2025-06-27"

    def test_anthropic_disabled_is_noop(self):
        params = {"temperature": 0.2}
        out = AnthropicAdapter().apply_context_management(params, {"enabled": False})
        assert out == params
        assert "context_management" not in out

    def test_anthropic_clear_tool_inputs_and_strategy(self):
        out = AnthropicAdapter().apply_context_management(
            {}, {"enabled": True, "clear_tool_inputs": True}
        )
        assert out["context_management"]["edits"][0]["clear_tool_inputs"] is True

        out2 = AnthropicAdapter().apply_context_management(
            {}, {"enabled": True, "strategy": "clear_thinking_20251015"}
        )
        assert out2["context_management"]["edits"][0]["type"] == "clear_thinking_20251015"

    def test_anthropic_merges_existing_beta_header(self):
        out = AnthropicAdapter().apply_context_management(
            {"extra_headers": {"anthropic-beta": "other-beta"}}, {"enabled": True}
        )
        beta = out["extra_headers"]["anthropic-beta"]
        assert "other-beta" in beta and "context-management-2025-06-27" in beta

    def test_openai_adapter_is_noop_even_when_enabled(self):
        # OpenAI-first gate: no Anthropic fields injected on the OpenAI adapter.
        out = OpenAIAdapter().apply_context_management({"a": 1}, {"enabled": True})
        assert out == {"a": 1}
        assert "context_management" not in out
        assert "extra_headers" not in out

    def test_per_tenant_override_gates_the_call(self):
        # Replicates the main.py wiring: global off, tenant on → injected;
        # default tenant → no-op. Covers step 2 (resolution + gate) without FastAPI.
        config = {
            "groups": {"context_editing": {"enabled": False}},
            "tenants": {"acme": {"groups": {"context_editing": {"enabled": True}}}},
        }
        adapter = AnthropicAdapter()

        enabled = apply_context_management({}, adapter, config, tenant_id="acme")
        assert "context_management" in enabled

        default = apply_context_management({}, adapter, config, tenant_id="default")
        assert "context_management" not in default

    def test_openai_only_config_produces_no_provider_fields(self):
        # Gate 3 mandatory: even with context editing enabled, the OpenAI adapter
        # path emits no Anthropic-specific fields.
        config = {"groups": {"context_editing": {"enabled": True}}}
        out = apply_context_management({"model": "gpt-4o"}, OpenAIAdapter(), config)
        assert "context_management" not in out
        assert "anthropic-beta" not in str(out)


# ---------------------------------------------------------------------------
# Cache policy engine v2 (P1) — prompt_cache_key + cache-read cost multiplier
# ---------------------------------------------------------------------------

class TestCachePolicyParams:
    def test_base_default_is_noop(self):
        # Gemini/Anthropic inherit the base no-op (no request-side cache key).
        assert GeminiAdapter().cache_policy_params("gemini-2.5-pro", "t1", "seed", {}) == {}
        assert AnthropicAdapter().cache_policy_params("claude-sonnet-4-5", "t1", "seed", {}) == {}

    def test_openai_emits_deterministic_prompt_cache_key(self):
        a = OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "sys-prefix-hash", {})
        b = OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "sys-prefix-hash", {})
        assert a == b                      # deterministic
        assert "prompt_cache_key" in a
        assert len(a["prompt_cache_key"]) == 32
        assert "prompt_cache_retention" not in a  # unset by default

    def test_openai_cache_key_varies_by_tenant_and_seed(self):
        base = OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "seedA", {})
        diff_tenant = OpenAIAdapter().cache_policy_params("gpt-4o", "other", "seedA", {})
        diff_seed = OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "seedB", {})
        assert base["prompt_cache_key"] != diff_tenant["prompt_cache_key"]
        assert base["prompt_cache_key"] != diff_seed["prompt_cache_key"]

    def test_openai_retention_and_key_len_configurable(self):
        cfg = {"providers": {"openai": {"prompt_cache_retention": "24h", "prompt_cache_key_len": 16}}}
        out = OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "seed", cfg)
        assert out["prompt_cache_retention"] == "24h"
        assert len(out["prompt_cache_key"]) == 16

    def test_openai_disabled_via_config(self):
        cfg = {"providers": {"openai": {"prompt_cache_key": False}}}
        assert OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "seed", cfg) == {}

    def test_openai_only_emits_openai_fields(self):
        # Gate 3: no Anthropic/Gemini fields leak from the OpenAI cache policy.
        out = OpenAIAdapter().cache_policy_params("gpt-4o", "acme", "seed", {})
        blob = str(out)
        assert "cache_control" not in blob
        assert "thinking" not in blob
        assert "response_schema" not in blob


class TestCacheReadCostMultiplier:
    def test_defaults_per_provider(self):
        assert OpenAIAdapter().cache_read_cost_multiplier({}) == 0.5
        assert AnthropicAdapter().cache_read_cost_multiplier({}) == 0.1
        assert GeminiAdapter().cache_read_cost_multiplier({}) == 0.25

    def test_config_override(self):
        cfg = {
            "groups": {
                "G21_cache_alignment": {
                    "providers": {
                        "openai": {"cache_read_multiplier": 0.4},
                        "anthropic": {"cache_read_multiplier": 0.05},
                        "gemini": {"cache_read_multiplier": 0.2},
                    }
                }
            }
        }
        assert OpenAIAdapter().cache_read_cost_multiplier(cfg) == 0.4
        assert AnthropicAdapter().cache_read_cost_multiplier(cfg) == 0.05
        assert GeminiAdapter().cache_read_cost_multiplier(cfg) == 0.2


# ---------------------------------------------------------------------------
# Provider-native async batch lane (P2)
# ---------------------------------------------------------------------------

class _BareAdapter(ProviderAdapter):
    """Minimal adapter to exercise base-class defaults."""
    @property
    def name(self):
        return "bare"

    def map_structured_output(self, format_type, schema=None):
        return {}

    def map_reasoning_effort(self, tier, config):
        return {}


class TestNativeBatchInterface:
    def test_base_default_is_opt_in_off(self):
        # A new adapter is not native and not service-tier-capable until it opts in.
        bare = _BareAdapter()
        assert bare.supports_native_batch() is False
        assert bare.supports_service_tier() is False

    def test_known_adapters_support_native_batch(self):
        # OpenAI = direct SDK; Anthropic/Gemini = litellm unified batch lane.
        assert OpenAIAdapter().supports_native_batch() is True
        assert AnthropicAdapter().supports_native_batch() is True
        assert GeminiAdapter().supports_native_batch() is True

    def test_service_tier_support_matrix(self):
        # Flex: only OpenAI accepts service_tier; others get it stripped.
        assert OpenAIAdapter().supports_service_tier() is True
        assert AnthropicAdapter().supports_service_tier() is False
        assert GeminiAdapter().supports_service_tier() is False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROVIDERS_CONFIG = [
    {"name": "openai", "model_prefixes": ["gpt", "o1", "o3", "text-"]},
    {"name": "anthropic", "model_prefixes": ["claude"]},
    {"name": "gemini", "model_prefixes": ["gemini"]},
]

G12_CONFIG = {
    "groups": {
        "G12_reasoning": {
            "effort_map": {
                "low":    {"openai": "low",    "anthropic_tokens": 512,   "gemini_thinking_budget": 512},
                "medium": {"openai": "medium", "anthropic_tokens": 4096,  "gemini_thinking_budget": 4096},
                "high":   {"openai": "high",   "anthropic_tokens": 16000, "gemini_thinking_budget": 16000},
            }
        }
    }
}


# ---------------------------------------------------------------------------
# Adapter selection via get_adapter
# ---------------------------------------------------------------------------

class TestGetAdapter:
    def test_gpt_model_returns_openai(self):
        adapter = get_adapter("gpt-4o-mini", PROVIDERS_CONFIG)
        assert isinstance(adapter, OpenAIAdapter)
        assert adapter.name == "openai"

    def test_o_series_returns_openai(self):
        adapter = get_adapter("o3-mini", PROVIDERS_CONFIG)
        assert isinstance(adapter, OpenAIAdapter)

    def test_claude_model_returns_anthropic(self):
        adapter = get_adapter("claude-sonnet-4-5", PROVIDERS_CONFIG)
        assert isinstance(adapter, AnthropicAdapter)
        assert adapter.name == "anthropic"

    def test_gemini_model_returns_gemini(self):
        adapter = get_adapter("gemini-2.5-pro", PROVIDERS_CONFIG)
        assert isinstance(adapter, GeminiAdapter)
        assert adapter.name == "gemini"

    def test_unknown_model_falls_back_to_openai(self):
        adapter = get_adapter("some-unknown-model-v1", PROVIDERS_CONFIG)
        assert isinstance(adapter, OpenAIAdapter)

    def test_empty_providers_config_returns_openai(self):
        adapter = get_adapter("gpt-4o", [])
        assert isinstance(adapter, OpenAIAdapter)


class TestGetAdapterByName:
    def test_openai_by_name(self):
        assert isinstance(get_adapter_by_name("openai"), OpenAIAdapter)

    def test_anthropic_by_name(self):
        assert isinstance(get_adapter_by_name("anthropic"), AnthropicAdapter)

    def test_gemini_by_name(self):
        assert isinstance(get_adapter_by_name("gemini"), GeminiAdapter)

    def test_unknown_provider_raises(self):
        # cohere/mistral/etc. are now first-class; use a genuinely-unregistered name.
        with pytest.raises(UnsupportedProviderError, match="no-such-provider"):
            get_adapter_by_name("no-such-provider")


# ---------------------------------------------------------------------------
# Base interface compliance
# ---------------------------------------------------------------------------

class TestBaseInterface:
    @pytest.mark.parametrize("adapter_cls", [OpenAIAdapter, AnthropicAdapter, GeminiAdapter])
    def test_all_implement_base(self, adapter_cls):
        adapter = adapter_cls()
        assert isinstance(adapter, ProviderAdapter)
        assert isinstance(adapter.name, str)
        assert callable(adapter.map_structured_output)
        assert callable(adapter.map_reasoning_effort)
        assert callable(adapter.supports_reasoning)
        assert callable(adapter.inject_cache_control)


class TestSupportsReasoning:
    def test_openai_o1_supported(self):
        assert OpenAIAdapter().supports_reasoning("o1-mini") is True

    def test_openai_o3_supported(self):
        assert OpenAIAdapter().supports_reasoning("o3-mini") is True

    def test_openai_o4_supported(self):
        assert OpenAIAdapter().supports_reasoning("o4-mini") is True

    def test_openai_gpt4o_not_supported(self):
        assert OpenAIAdapter().supports_reasoning("gpt-4o") is False

    def test_openai_gpt4o_mini_not_supported(self):
        assert OpenAIAdapter().supports_reasoning("gpt-4o-mini") is False

    def test_anthropic_all_models_supported(self):
        for model in ("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4"):
            assert AnthropicAdapter().supports_reasoning(model) is True

    def test_gemini_all_models_supported(self):
        for model in ("gemini-2.5-pro", "gemini-2.5-flash"):
            assert GeminiAdapter().supports_reasoning(model) is True


# ---------------------------------------------------------------------------
# OpenAI adapter
# ---------------------------------------------------------------------------

class TestOpenAIAdapter:
    def setup_method(self):
        self.adapter = OpenAIAdapter()

    def test_json_object_format(self):
        result = self.adapter.map_structured_output("json_object")
        assert result == {"response_format": {"type": "json_object"}}

    def test_json_schema_format(self):
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        result = self.adapter.map_structured_output("json_schema", schema)
        assert result["response_format"]["type"] == "json_schema"
        assert result["response_format"]["json_schema"] == schema

    def test_json_schema_without_schema_returns_empty(self):
        result = self.adapter.map_structured_output("json_schema")
        assert result == {}

    def test_text_format_returns_empty(self):
        assert self.adapter.map_structured_output("text") == {}

    def test_reasoning_effort_low(self):
        result = self.adapter.map_reasoning_effort("low", G12_CONFIG)
        assert result == {"reasoning_effort": "low"}

    def test_reasoning_effort_high(self):
        result = self.adapter.map_reasoning_effort("high", G12_CONFIG)
        assert result == {"reasoning_effort": "high"}

    def test_reasoning_effort_no_config_uses_tier_name(self):
        result = self.adapter.map_reasoning_effort("medium", {})
        assert result == {"reasoning_effort": "medium"}

    def test_inject_cache_control_is_noop(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out = self.adapter.inject_cache_control(msgs)
        assert out == msgs
        assert "cache_control" not in out[0]


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------

class TestAnthropicAdapter:
    def setup_method(self):
        self.adapter = AnthropicAdapter()

    def test_json_object_returns_tool_use(self):
        result = self.adapter.map_structured_output("json_object")
        assert "tools" in result
        assert result["tool_choice"]["name"] == "structured_output"

    def test_json_schema_uses_provided_schema(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result = self.adapter.map_structured_output("json_schema", schema)
        assert result["tools"][0]["input_schema"] == schema

    def test_text_format_returns_empty(self):
        assert self.adapter.map_structured_output("text") == {}

    def test_reasoning_effort_low(self):
        result = self.adapter.map_reasoning_effort("low", G12_CONFIG)
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 512}}

    def test_reasoning_effort_high(self):
        result = self.adapter.map_reasoning_effort("high", G12_CONFIG)
        assert result["thinking"]["budget_tokens"] == 16000

    def test_reasoning_effort_defaults_without_config(self):
        # No config → falls back to module-level defaults (1024 for "low")
        result = self.adapter.map_reasoning_effort("low", {})
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 1024}}

    def test_inject_cache_control_adds_to_system(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out = self.adapter.inject_cache_control(msgs)
        assert out[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in out[1]

    def test_inject_cache_control_no_system_message(self):
        msgs = [{"role": "user", "content": "hi"}]
        out = self.adapter.inject_cache_control(msgs)
        assert out == msgs

    def test_inject_cache_control_does_not_mutate_original(self):
        msgs = [{"role": "system", "content": "sys"}]
        out = self.adapter.inject_cache_control(msgs)
        assert "cache_control" not in msgs[0]
        assert "cache_control" in out[0]


# ---------------------------------------------------------------------------
# Gemini adapter
# ---------------------------------------------------------------------------

class TestGeminiAdapter:
    def setup_method(self):
        self.adapter = GeminiAdapter()

    def test_json_object_returns_mime_type(self):
        result = self.adapter.map_structured_output("json_object")
        assert result == {"response_mime_type": "application/json"}

    def test_json_schema_includes_schema_and_mime(self):
        schema = {"type": "object"}
        result = self.adapter.map_structured_output("json_schema", schema)
        assert result["response_schema"] == schema
        assert result["response_mime_type"] == "application/json"

    def test_text_format_returns_empty(self):
        assert self.adapter.map_structured_output("text") == {}

    def test_reasoning_effort_low(self):
        result = self.adapter.map_reasoning_effort("low", G12_CONFIG)
        assert result == {"thinking_config": {"thinking_budget": 512}}

    def test_reasoning_effort_defaults_without_config(self):
        result = self.adapter.map_reasoning_effort("medium", {})
        assert result == {"thinking_config": {"thinking_budget": 4096}}

    def test_inject_cache_control_is_noop(self):
        msgs = [{"role": "system", "content": "sys"}]
        out = self.adapter.inject_cache_control(msgs)
        assert out == msgs
        assert "cache_control" not in out[0]
