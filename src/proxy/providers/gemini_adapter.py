from typing import Any, Dict, List, Optional

from providers import ProviderAdapter, register_adapter

_THINKING_DEFAULTS = {"low": 1024, "medium": 4096, "high": 16000}


@register_adapter("gemini")
class GeminiAdapter(ProviderAdapter):
    @property
    def name(self) -> str:
        return "gemini"

    def unsupported_params(self) -> set:
        """Gemini rejects OpenAI-only params and Anthropic's ``thinking``."""
        return {"parallel_tool_calls", "logprobs", "top_logprobs", "thinking"}

    def build_call(self, model: str, provider_cfg: Dict, api_key: Optional[str]) -> tuple:
        """Route via the ``gemini/`` namespace so litellm uses the API-key Google AI Studio
        endpoint (``LLM_KEY_GEMINI``) — bare ``gemini-*`` makes litellm try Vertex AI, which
        needs Google Application Default Credentials and fails with an ADC error.
        """
        cfg = dict(provider_cfg or {})
        cfg.setdefault("litellm_prefix", "gemini")
        return super().build_call(model, cfg, api_key)

    def extract_usage(self, response: Dict) -> Dict:
        """Prefer litellm's normalised OpenAI shape; fall back to Gemini-native fields."""
        usage = response.get("usage", {}) or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        if not cached:
            cached = (
                usage.get("cached_content_token_count", 0)
                or usage.get("cached_content_input_tokens", 0)
                or 0
            )
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        if not reasoning:
            reasoning = usage.get("thinking_tokens", 0) or 0
        return {"cached_tokens": cached, "reasoning_tokens": reasoning}

    def map_structured_output(
        self,
        format_type: str,
        schema: Optional[Dict] = None,
    ) -> Dict:
        if format_type == "json_schema" and schema:
            return {"response_schema": schema, "response_mime_type": "application/json"}
        if format_type == "json_object":
            return {"response_mime_type": "application/json"}
        return {}

    def map_reasoning_effort(self, tier: str, config: Dict) -> Dict:
        tier_cfg = (
            config.get("groups", {})
            .get("G12_reasoning", {})
            .get("effort_map", {})
            .get(tier, {})
        )
        # Config key is `gemini_mode`; fall back to module defaults if absent.
        budget = tier_cfg.get("gemini_thinking_budget", _THINKING_DEFAULTS.get(tier, 1024))
        return {"thinking_config": {"thinking_budget": budget}}

    def cap_reasoning_params(self, params: Dict, max_tokens: Optional[int]) -> Dict:
        """Keep ``thinking_config.thinking_budget`` below ``max_tokens`` (0 disables thinking)."""
        if not max_tokens:
            return params
        tc = params.get("thinking_config")
        if not isinstance(tc, dict):
            return params
        budget = tc.get("thinking_budget")
        if budget is None or budget < max_tokens:
            return params
        tc["thinking_budget"] = max(0, max_tokens - 1)
        return params

    def cache_read_cost_multiplier(self, config: Dict) -> float:
        """Gemini implicit caching bills cache-hit tokens at ~25% (config-overridable)."""
        pcfg = (
            config.get("groups", {})
            .get("G21_cache_alignment", {})
            .get("providers", {})
            .get("gemini", {})
        )
        return float(pcfg.get("cache_read_multiplier", 0.25))

    def supports_native_batch(self) -> bool:
        """Opt into the native batch lane via litellm's unified batch API
        (custom_llm_provider="gemini"). Falls back to the per-item loop if
        litellm cannot batch this provider."""
        return True
