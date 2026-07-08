import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False

# Configurable constants — override via env vars or config/config.yaml savings section
_CHARS_PER_TOKEN: int = int(os.getenv("CHARS_PER_TOKEN", "4"))
_PER_MESSAGE_OVERHEAD: int = int(os.getenv("PER_MESSAGE_OVERHEAD_TOKENS", "4"))
_PER_TOOL_OVERHEAD: int = int(os.getenv("PER_TOOL_OVERHEAD_TOKENS", "4"))

_TIKTOKEN_CACHE: Dict[str, Any] = {}


def _get_tiktoken_enc(model: str):
    if model not in _TIKTOKEN_CACHE:
        try:
            _TIKTOKEN_CACHE[model] = tiktoken.encoding_for_model(model)
        except Exception:
            try:
                _TIKTOKEN_CACHE[model] = tiktoken.get_encoding("cl100k_base")
            except Exception:
                _TIKTOKEN_CACHE[model] = None
    return _TIKTOKEN_CACHE[model]


def _tiktoken_prefixes() -> list:
    """Lazy-load tiktoken-eligible model prefixes from config."""
    try:
        from config_loader import get_tiktoken_prefixes
        return get_tiktoken_prefixes()
    except Exception:
        return []


def _non_gpt_tiktoken_fallback() -> bool:
    """B2: when enabled (config ``savings.non_gpt_tiktoken_fallback`` or env
    ``NON_GPT_TIKTOKEN_FALLBACK``), non-GPT models use cl100k_base tiktoken locally for a
    closer-than-char/4 ingress estimate. Default OFF → char/4 preserved (zero added
    latency, no provider token-counting API call)."""
    try:
        from config_loader import get_config
        val = (get_config() or {}).get("savings", {}).get("non_gpt_tiktoken_fallback")
        if val is not None:
            return bool(val)
    except Exception:
        pass
    return os.getenv("NON_GPT_TIKTOKEN_FALLBACK", "false").lower() in ("1", "true", "yes")


def estimate_tokens(text: str, model: str) -> int:
    """Provider-agnostic token estimator. tiktoken for configured GPT-family, char/4 fallback."""
    if not text:
        return 0
    model_lower = model.lower()
    prefixes = _tiktoken_prefixes()
    if _TIKTOKEN_AVAILABLE and prefixes and any(f in model_lower for f in prefixes):
        enc = _get_tiktoken_enc(model_lower)
        if enc:
            return len(enc.encode(text))
    # B2: optional, config-gated cl100k_base fallback for non-GPT models — a closer
    # (still-estimated) local count than char/4, with no provider API round-trip.
    if _TIKTOKEN_AVAILABLE and _non_gpt_tiktoken_fallback():
        enc = _get_tiktoken_enc("cl100k_base")
        if enc:
            return len(enc.encode(text))
    # Universal fallback: chars-per-token estimate (configurable, default 4 per playbook G2)
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def count_messages_tokens(
    messages: List[Dict[str, Any]], model: str
) -> int:
    """Count tokens across all messages in an OpenAI-format request."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content, model)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""), model)
        # Phase 2: Count tool_calls (assistant messages that invoke tools)
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            if isinstance(tc, dict):
                total += estimate_tokens(tc.get("id", ""), model)
                fn = tc.get("function", {})
                if isinstance(fn, dict):
                    total += estimate_tokens(fn.get("name", ""), model)
                    total += estimate_tokens(fn.get("arguments", ""), model)
        role = msg.get("role", "")
        total += estimate_tokens(role, model)
        total += _PER_MESSAGE_OVERHEAD
    return total


def count_tools_tokens(tools: List[Dict[str, Any]], model: str) -> int:
    """Count tokens for tool definitions in the request.
    
    Phase 2 fix: Tools consume tokens in the prompt (approximate via JSON length).
    """
    if not tools:
        return 0
    import json
    total = 0
    for tool in tools:
        # Serialize tool definition and count as if it were text
        tool_text = json.dumps(tool, separators=(',', ':'))
        total += estimate_tokens(tool_text, model)
        # Add overhead per tool (similar to message overhead)
        total += _PER_TOOL_OVERHEAD
    return total


def count_request_tokens(
    messages: List[Dict[str, Any]], 
    model: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Count total input tokens including messages and tool definitions.
    
    Phase 2 fix: Honest baseline that matches what the provider bills.
    """
    total = count_messages_tokens(messages, model)
    if tools:
        total += count_tools_tokens(tools, model)
    return total


def messages_to_text(messages: List[Dict[str, Any]]) -> str:
    """Flatten messages to a single text blob for compression/analysis."""
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def get_cost_per_1k(model: str) -> tuple:
    """Return (input_cost, output_cost) per 1k tokens for a model, sourced from config pricing table."""
    try:
        from config_loader import get_pricing_table
        pricing = get_pricing_table()
    except Exception:
        pricing = {}

    model_lower = model.lower()
    # Longest (most specific) matching key wins, independent of dict order — so e.g.
    # "opencode/deepseek-v4-pro" matches its own row rather than the generic "deepseek",
    # and "gpt-4o-mini" beats "gpt-4o".
    best_key = None
    best_costs = None
    for key, costs in pricing.items():
        if key == "default":
            continue
        if key.lower() in model_lower and (best_key is None or len(key) > len(best_key)):
            best_key = key
            best_costs = costs
    if best_costs is not None:
        return (best_costs.get("input", 0.005), best_costs.get("output", 0.015))

    # WS22: pricing miss — this model silently priced at the default (gpt-4o-class)
    # rates, skewing cost_actual/cost_saved estimates. Warn once per model so the
    # operator seeds a pricing row (esp. for BYOK-permitted models).
    if model_lower not in _PRICING_MISS_WARNED:
        _PRICING_MISS_WARNED.add(model_lower)
        logger.warning(
            "pricing table has no row matching model '%s' — falling back to default "
            "rates; add a pricing entry for accurate cost estimates", model)

    default = pricing.get("default", {})
    return (default.get("input", 0.005), default.get("output", 0.015))


_PRICING_MISS_WARNED: set = set()


def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate USD cost for a single LLM call."""
    inp_cost, out_cost = get_cost_per_1k(model)
    return round(
        (input_tokens / 1000.0 * inp_cost) + (output_tokens / 1000.0 * out_cost), 8
    )


def estimate_cost_with_cache(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    model: str,
    cache_read_multiplier: float = 1.0,
    *,
    batch_discount: float = 1.0,
    reasoning_tokens: int = 0,
    reasoning_rate_multiplier: float = 1.0,
) -> float:
    """USD cost crediting the provider cached-input discount.

    ``cached_tokens`` are a subset of ``input_tokens`` and are billed at
    ``cache_read_multiplier`` × the input rate (e.g. 0.5 OpenAI, 0.1 Anthropic,
    0.25 Gemini). With ``cached_tokens == 0`` this reduces exactly to
    ``estimate_cost()``, so it is a safe drop-in for the cost source-of-truth.

    B3 — discount-aware price book (reporting-only). All keyword-only args default to
    the previous behaviour, so this stays a byte-identical drop-in unless opted in:
      * ``batch_discount``            — multiplies the whole bill (e.g. 0.5 for a
                                        provider-native async batch lane). 1.0 = none.
      * ``reasoning_tokens``          — reasoning/thinking tokens that are ALREADY part
                                        of ``output_tokens`` (e.g. OpenAI). Only the
                                        *delta* above the standard output rate is added,
                                        so there is no double counting.
      * ``reasoning_rate_multiplier`` — >1.0 models a reasoning surcharge; 1.0 = none.
    """
    inp_cost, out_cost = get_cost_per_1k(model)
    cached = max(0, min(cached_tokens, input_tokens))
    non_cached = input_tokens - cached
    base = (
        (non_cached / 1000.0 * inp_cost)
        + (cached / 1000.0 * inp_cost * cache_read_multiplier)
        + (output_tokens / 1000.0 * out_cost)
    )
    # Reasoning surcharge: reasoning tokens are already billed within output_tokens,
    # so add only the delta above the standard output rate (no double count).
    reasoning = max(0, min(reasoning_tokens, output_tokens))
    base += reasoning / 1000.0 * out_cost * (reasoning_rate_multiplier - 1.0)
    return round(base * batch_discount, 8)


def effective_token_cost(
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    model_multiplier: float = 1.0,
) -> float:
    """
    Effective Token (ET) metric from G18 playbook:
    ET = m × (1.0 × Input + 0.1 × Cache-read + 4.0 × Output)
    """
    return model_multiplier * (
        1.0 * input_tokens + 0.1 * cache_read_tokens + 4.0 * output_tokens
    )
