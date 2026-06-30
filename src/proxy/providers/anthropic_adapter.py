import copy
from typing import Any, Dict, List, Optional

from providers import ProviderAdapter, register_adapter

_THINKING_DEFAULTS = {"low": 1024, "medium": 4096, "high": 16000}


@register_adapter("anthropic")
class AnthropicAdapter(ProviderAdapter):
    @property
    def name(self) -> str:
        return "anthropic"

    def unsupported_params(self) -> set:
        """Anthropic rejects OpenAI-only sampling/tool params."""
        return {"parallel_tool_calls", "logprobs", "top_logprobs"}

    def extract_usage(self, response: Dict) -> Dict:
        """Prefer litellm's normalised OpenAI shape; fall back to Anthropic-native fields."""
        usage = response.get("usage", {}) or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        if not cached:
            cached = usage.get("cache_read_input_tokens", 0) or 0
        reasoning = usage.get("thinking_tokens", 0) or 0
        if not reasoning:
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        return {"cached_tokens": cached, "reasoning_tokens": reasoning}

    def align_prefix(self, ctx, system_msgs, variable_msgs, cfg) -> bool:
        """G21: Anthropic prompt caching — mark the last system message (and last tool)
        with cache_control. Honours ``providers.anthropic.marker`` (default off). Only ever
        called when this adapter is the routed one, so no isinstance guard is needed.
        """
        provider_cfg = cfg.get("providers", {}).get("anthropic", {})
        if not provider_cfg.get("marker", False):
            return False
        cache_type = provider_cfg.get("cache_type", "ephemeral")
        changed = False
        if system_msgs:
            system_msgs = copy.deepcopy(system_msgs)
            system_msgs[-1]["cache_control"] = {"type": cache_type}
            ctx.messages = system_msgs + variable_msgs
            changed = True
        tools = ctx.params.get("tools")
        if tools and isinstance(tools, list) and len(tools) > 0:
            tools = copy.deepcopy(tools)
            tools[-1]["cache_control"] = {"type": cache_type}
            ctx.params["tools"] = tools
            changed = True
        return changed

    def map_structured_output(
        self,
        format_type: str,
        schema: Optional[Dict] = None,
    ) -> Dict:
        if format_type in ("json_object", "json_schema"):
            return {
                "tools": [
                    {
                        "name": "structured_output",
                        "description": "Return the response as structured JSON",
                        "input_schema": schema or {"type": "object"},
                    }
                ],
                "tool_choice": {"type": "tool", "name": "structured_output"},
            }
        return {}

    def map_reasoning_effort(self, tier: str, config: Dict) -> Dict:
        tier_cfg = (
            config.get("groups", {})
            .get("G12_reasoning", {})
            .get("effort_map", {})
            .get(tier, {})
        )
        # Config key is `anthropic_tokens`; fall back to module defaults if absent.
        budget = tier_cfg.get("anthropic_tokens", _THINKING_DEFAULTS.get(tier, 1024))
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    def cap_reasoning_params(self, params: Dict, max_tokens: Optional[int]) -> Dict:
        """Keep ``thinking.budget_tokens`` strictly below ``max_tokens`` — Anthropic 400s
        when ``max_tokens <= thinking.budget_tokens``. If ``max_tokens`` is too small to fit
        Anthropic's 1024-token minimum thinking budget, drop thinking entirely.
        """
        if not max_tokens:
            return params
        thinking = params.get("thinking")
        if not isinstance(thinking, dict):
            return params
        budget = thinking.get("budget_tokens")
        if budget is None or budget < max_tokens:
            return params
        new_budget = max_tokens - 1
        if new_budget < 1024:
            params.pop("thinking", None)
        else:
            thinking["budget_tokens"] = new_budget
        return params

    def inject_cache_control(self, messages: List[Dict]) -> List[Dict]:
        """
        Annotate the first system message with cache_control so the Anthropic
        API can reuse the prefix-cached KV from prior calls in the same session.
        Only modifies the first system turn; user/assistant turns are unchanged.
        """
        messages = copy.deepcopy(messages)
        for msg in messages:
            if msg.get("role") == "system":
                msg["cache_control"] = {"type": "ephemeral"}
                break
        return messages

    # Anthropic native context editing — clears stale tool results / thinking
    # blocks server-side as the context fills. Beta as of 2025-06-27.
    _CONTEXT_MGMT_BETA = "context-management-2025-06-27"

    def apply_context_management(self, params: Dict, cfg: Dict) -> Dict:
        """
        Inject Anthropic context-editing when ``cfg.enabled`` is true.

        Adds the ``context_management`` request field and the context-editing beta
        flag (via ``extra_headers``, which LiteLLM passes through to the Anthropic
        API). Off by default → returns params untouched.
        """
        if not cfg.get("enabled", False):
            return params

        strategy_type = cfg.get("strategy", "clear_tool_uses_20250919")
        strategy: Dict[str, Any] = {"type": strategy_type}
        # Only the clear_tool_uses strategy carries clear_tool_inputs.
        if strategy_type == "clear_tool_uses_20250919" and cfg.get("clear_tool_inputs", False):
            strategy["clear_tool_inputs"] = True

        out = dict(params)
        out["context_management"] = {"edits": [strategy]}

        extra = dict(out.get("extra_headers") or {})
        existing = extra.get("anthropic-beta", "")
        if self._CONTEXT_MGMT_BETA not in existing:
            extra["anthropic-beta"] = (
                f"{existing},{self._CONTEXT_MGMT_BETA}" if existing else self._CONTEXT_MGMT_BETA
            )
        out["extra_headers"] = extra
        return out

    def cache_read_cost_multiplier(self, config: Dict) -> float:
        """Anthropic bills cache-read tokens at ~10% (config-overridable)."""
        pcfg = (
            config.get("groups", {})
            .get("G21_cache_alignment", {})
            .get("providers", {})
            .get("anthropic", {})
        )
        return float(pcfg.get("cache_read_multiplier", 0.1))

    def supports_native_batch(self) -> bool:
        """Opt into the native batch lane via litellm's unified batch API
        (custom_llm_provider="anthropic"). Falls back to the per-item loop if
        litellm cannot batch this provider."""
        return True
