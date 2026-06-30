"""
G11 · Output Length & Format Control
Stage: Inside the LLM (parameter injection)
Saving: 30–60% output tokens
Technique:
  1. Enforce max_tokens on every call (default: 2× expected if not set).
  2. Inject JSON schema / response_format via ctx.provider_adapter.map_structured_output().
"""
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing

logger = logging.getLogger(__name__)
GROUP = "G11"

_DEFAULT_MAX_TOKENS_MULTIPLIER = 2.0
_ABSOLUTE_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_MODEL_MAX_TOKENS = 4096  # Safe default for unknown models


def _get_model_max_tokens(model: Optional[str], cfg: Optional[Dict[str, Any]] = None) -> int:
    """
    Return the max completion token limit for model from config.
    Falls back to cfg['default_model_max_tokens'] (default 4096) for unknown models.
    Model limits live entirely in config/config.yaml under G11_output.model_max_tokens.
    """
    if not model:
        return _DEFAULT_MODEL_MAX_TOKENS

    config_limits = (cfg or {}).get("model_max_tokens", {})
    if model in config_limits:
        return config_limits[model]
    for prefix, max_tokens in config_limits.items():
        if model.startswith(prefix):
            return max_tokens

    return (cfg or {}).get("default_model_max_tokens", _DEFAULT_MODEL_MAX_TOKENS)


def _get_adapter(ctx: RequestContext):
    """Return ctx.provider_adapter, falling back to OpenAIAdapter when not set."""
    if ctx.provider_adapter is not None:
        return ctx.provider_adapter
    from providers.openai_adapter import OpenAIAdapter
    return OpenAIAdapter()


def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


def _history_key(ctx: RequestContext) -> str:
    workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id") or "default"
    template_id = ctx.params.get("template_id") or ctx.params.get("x_template_id") or "default"
    return f"tok_opt:max_tokens_history:{workflow_id}:{template_id}"


async def _get_historical_p95(
    redis, key: str, quantile: float = 0.95, min_entries: int = 5
) -> Optional[int]:
    """Fetch historical max_tokens values from Redis ZSET and compute p95."""
    try:
        entries = await redis.zrevrange(key, 0, min_entries * 2 - 1, withscores=False)
        if not entries or len(entries) < min_entries:
            return None
        max_tokens_values: List[int] = []
        for raw in entries:
            try:
                data = json.loads(raw)
                mt = data.get("max_tokens")
                if isinstance(mt, int):
                    max_tokens_values.append(mt)
            except Exception:
                continue
        if len(max_tokens_values) < min_entries:
            return None
        sorted_vals = sorted(max_tokens_values)
        idx = int((len(sorted_vals) - 1) * quantile)
        return sorted_vals[idx]
    except Exception as exc:
        logger.warning("G11 failed to compute historical p95: %s", exc)
        return None


async def _record_max_tokens_pair(
    redis, key: str, max_tokens: int, completion_tokens: int, ttl_seconds: int
) -> None:
    """Record a (max_tokens, completion_tokens) pair to Redis ZSET with TTL."""
    try:
        member = json.dumps({"max_tokens": max_tokens, "completion_tokens": completion_tokens})
        score = time.time()
        await redis.zadd(key, {member: score})
        await redis.expire(key, ttl_seconds)
    except Exception as exc:
        logger.warning("G11 failed to record max_tokens history: %s", exc)



# ─── Verbosity steering (T20) ────────────────────────────────────────────────
# Ships as a config-suffix-only no-op. The headroom.verbosity_model hook below
# will activate once headroom ships a trained per-tenant model; until then the
# try/except silently skips that path and falls through to the static suffix.

def _get_verbosity_suffix(tenant_id: str, verbosity_cfg: Dict[str, Any]) -> Optional[str]:
    """Return the verbosity suffix to append, or None if nothing should be added.

    Priority:
    1. headroom.verbosity_model.predict(tenant_id) — future; no-op today
    2. verbosity_cfg["per_tenant_suffix"][tenant_id]  — static per-tenant override
    3. verbosity_cfg["default_suffix"]                — global default
    """
    try:
        import headroom as _hm  # type: ignore
        model = getattr(_hm, "verbosity_model", None)
        if model is not None:
            predicted = model.predict(tenant_id)
            if predicted:
                return predicted
    except (ImportError, AttributeError, Exception):
        pass  # headroom verbosity model not available yet — fall through

    per_tenant = verbosity_cfg.get("per_tenant_suffix", {})
    if tenant_id in per_tenant:
        return per_tenant[tenant_id]

    return verbosity_cfg.get("default_suffix")  # None if not configured


def _append_verbosity_suffix(messages: List[Dict], suffix: str) -> List[Dict]:
    """Append the terse suffix to the last system message, or add a new one."""
    messages = list(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "system":
            messages[i] = {
                **messages[i],
                "content": messages[i].get("content", "") + "\n" + suffix,
            }
            return messages
    return [{"role": "system", "content": suffix}] + messages


# ─── A3: output-savings holdout ───────────────────────────────────────────────
_HOLDOUT_BUCKETS = 10000


def _assign_cohort(ctx: RequestContext, holdout_cfg: Dict[str, Any]) -> str:
    """Deterministic, sticky cohort for the output-shaping holdout.

    Returns "holdout" (control — G11 shaping skipped) or "treatment". Sticky on a
    stable key so a multi-turn conversation stays in one cohort; hash-based so it
    is reproducible and testable.
    """
    fraction = holdout_cfg.get("fraction", 0.0)
    if fraction <= 0:
        return "treatment"
    if fraction >= 1:
        return "holdout"
    sticky_key = holdout_cfg.get("sticky_key", "workflow_id")
    stable = (
        ctx.params.get(sticky_key)
        or ctx.params.get(f"x_{sticky_key}")
        or getattr(ctx, "user_id", None)
        or ctx.request_id
    )
    bucket = int(hashlib.sha256(str(stable).encode()).hexdigest()[:8], 16) % _HOLDOUT_BUCKETS
    return "holdout" if bucket < fraction * _HOLDOUT_BUCKETS else "treatment"


def _record_holdout_metric(ctx: RequestContext, completion_tokens: int) -> None:
    """Emit the cohort-labelled completion-token Histogram for the A3 holdout.

    Lazy-imports the G18 metric to avoid any import cycle; never raises.
    """
    cohort = ctx.params.get("_g11_cohort", "treatment")
    try:
        from middleware.g18_observability import OUTPUT_HOLDOUT_COMPLETION_TOKENS
        OUTPUT_HOLDOUT_COMPLETION_TOKENS.labels(
            cohort=cohort,
            tenant_id=(getattr(ctx, "tenant_id", None) or "default"),
        ).observe(completion_tokens)
    except Exception as exc:
        logger.debug("[%s] G11 holdout metric emit failed: %s", ctx.request_id, exc)


class G11OutputFormat:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G11_output", {})
        if not cfg.get("enabled", False):
            return ctx

        tokens_before = ctx.current_token_count
        changed = False
        notes = []

        # A3 output-savings holdout: a control cohort skips G11 shaping so the real
        # reduction can be measured (treatment vs holdout) in process_response.
        holdout_cfg = cfg.get("output_holdout", {})
        in_holdout = False
        if holdout_cfg.get("enabled", False):
            cohort = _assign_cohort(ctx, holdout_cfg)
            ctx.params["_g11_cohort"] = cohort
            in_holdout = cohort == "holdout"
            if in_holdout:
                notes.append("output-shaping held out (control cohort)")

        # 1. Enforce max_tokens
        # Skip if max_completion_tokens already set — o-series models use that param
        # instead of max_tokens and OpenAI rejects requests that set both simultaneously.
        if cfg.get("enforce_max_tokens", True) and not in_holdout:
            # Reasoning models spend the token budget on HIDDEN reasoning tokens;
            # tightening max_tokens starves the visible answer to empty (and that
            # empty answer then poisons the response cache). Detect via the provider
            # adapter (no model-name strings here) and skip enforcement for them.
            _rmodel = getattr(ctx, "routed_model", None) or ctx.params.get("model")
            _skip_reasoning = (
                cfg.get("skip_max_tokens_for_reasoning", True)
                and bool(_rmodel)
                and _get_adapter(ctx).supports_reasoning(_rmodel)
            )
            if _skip_reasoning:
                notes.append(
                    "max_tokens enforcement skipped (reasoning model — budget kept "
                    "for reasoning + visible output)"
                )
            elif ("max_tokens" not in ctx.params or ctx.params.get("max_tokens") is None) and \
                    "max_completion_tokens" not in ctx.params:
                auto_tighten = cfg.get("max_tokens_auto_tighten", False)
                historical_max = None
                if auto_tighten:
                    try:
                        redis = _get_redis()
                        key = _history_key(ctx)
                        tighten_q = cfg.get("tighten_quantile", 0.95)
                        historical_max = await _get_historical_p95(redis, key, quantile=tighten_q)
                    except Exception as exc:
                        logger.warning("G11 auto_tighten lookup failed: %s", exc)

                if historical_max:
                    tighten_mult = cfg.get("tighten_multiplier", 1.2)
                    model_limit = _get_model_max_tokens(ctx.params.get("model"), cfg)
                    ctx.params["max_tokens"] = min(max(64, int(historical_max * tighten_mult)), model_limit)
                    notes.append(f"max_tokens tightened to {ctx.params['max_tokens']} (p95×{tighten_mult}, cap={model_limit})")
                else:
                    multiplier = cfg.get("default_max_tokens_multiplier", _DEFAULT_MAX_TOKENS_MULTIPLIER)
                    # Estimate expected output ≈ 30% of input as a conservative baseline
                    expected_output = max(64, int(tokens_before * 0.3))
                    model_limit = _get_model_max_tokens(ctx.params.get("model"), cfg)
                    # Without historical data to tighten against, cap the heuristic
                    # at _ABSOLUTE_DEFAULT_MAX_TOKENS too — otherwise a single huge
                    # input (e.g. a large document) inflates max_tokens far past any
                    # reasonable default just because 30%-of-input*2 is still big.
                    absolute_cap = cfg.get("absolute_default_max_tokens", _ABSOLUTE_DEFAULT_MAX_TOKENS)
                    ctx.params["max_tokens"] = min(
                        int(expected_output * multiplier),
                        model_limit,
                        absolute_cap,
                    )
                    notes.append(f"max_tokens set to {ctx.params['max_tokens']} (cap={model_limit}, absolute_cap={absolute_cap})")
                changed = True

        # 2. Apply provider-specific structured output mapping via adapter
        adapter = _get_adapter(ctx)
        if cfg.get("provider_structured_output", True):
            schema = ctx.params.get("json_schema")
            if schema:
                format_type = "json_schema"
            elif ctx.params.get("x_json_output") or cfg.get("force_json_for_all", False):
                format_type = "json_object"
            else:
                format_type = "text"

            if format_type != "text":
                original_keys = set(ctx.params.keys())
                adapter_params = adapter.map_structured_output(format_type, schema)
                for key, value in adapter_params.items():
                    if key not in ctx.params:
                        ctx.params[key] = value
                new_keys = set(ctx.params.keys()) - original_keys
                if new_keys:
                    notes.append(
                        f"{adapter.name} structured output applied ({', '.join(sorted(new_keys))})"
                    )
                    changed = True

        # 3. Some providers (OpenAI) require the literal word "json" somewhere in the
        #    messages when response_format type is json_object/json_schema, or the API
        #    rejects the request. Capability-gated via the adapter (no provider-name check).
        response_format = ctx.params.get("response_format")
        if (
            adapter.requires_json_keyword()
            and response_format
            and response_format.get("type") in ("json_object", "json_schema")
        ):
            has_json_word = any(
                "json" in str(msg.get("content", "")).lower() for msg in ctx.messages
            )
            if not has_json_word:
                ctx.messages.append({
                    "role": "system",
                    "content": "Respond with a valid JSON object.",
                })
                notes.append("appended JSON instruction to satisfy response_format requirement")
                changed = True

        # 4. Verbosity steering — Headroom terse suffix (no-op until verbosity model trained)
        #    When headroom ships a per-tenant verbosity model, this injects a
        #    configurable suffix that steers the model toward shorter responses.
        #    Until the model exists, the block resolves to a static config suffix only.
        verbosity_cfg = cfg.get("verbosity_steering", {})
        if verbosity_cfg.get("enabled", False) and not in_holdout:
            suffix = _get_verbosity_suffix(ctx.tenant_id, verbosity_cfg)
            if suffix:
                ctx.messages = _append_verbosity_suffix(ctx.messages, suffix)
                notes.append(f"verbosity suffix injected (tenant={ctx.tenant_id})")
                changed = True

        if changed:
            ctx.savings.add_step(
                GROUP,
                "Output format: " + "; ".join(notes),
                tokens_before,
                tokens_before,  # input tokens unchanged — output token savings realised at response
            )
            langfuse_tracing.add_span(
                ctx,
                name="G11-output-format",
                span_input={"max_tokens_before": ctx.params.get("max_tokens")},
                output={"notes": notes, "params_changed": list(ctx.params.keys())},
                metadata={"notes": notes, "cohort": ctx.params.get("_g11_cohort", "treatment")},
            )
            logger.debug("[%s] G11: %s", ctx.request_id, "; ".join(notes))

        return ctx

    async def process_response(self, ctx: RequestContext, response: Dict[str, Any]) -> Tuple[RequestContext, Dict[str, Any]]:
        """
        Process response to implement max_tokens feedback loop.
        Record (max_tokens, completion_tokens) pair to Redis ZSET for future tightening.
        """
        cfg = ctx.config.get("groups", {}).get("G11_output", {})
        if not cfg.get("enabled", False):
            return ctx, response

        usage = response.get("usage", {})
        completion_tokens = usage.get("completion_tokens", 0)

        # A3: output-savings holdout — record completion tokens by cohort so the
        # treatment-vs-holdout reduction can be computed in Grafana/PromQL.
        if cfg.get("output_holdout", {}).get("enabled", False) and completion_tokens:
            _record_holdout_metric(ctx, completion_tokens)

        if not cfg.get("max_tokens_feedback_loop", False):
            return ctx, response

        max_tokens = ctx.params.get("max_tokens")

        if max_tokens and completion_tokens:
            utilization = completion_tokens / max_tokens
            logger.debug(
                "[%s] G11 max_tokens feedback: %d/%d used (%.1f%% utilization)",
                ctx.request_id,
                completion_tokens,
                max_tokens,
                utilization * 100,
            )

            # Record to Redis ZSET for historical p95 analysis
            try:
                redis = _get_redis()
                key = _history_key(ctx)
                ttl_days = cfg.get("max_tokens_history_ttl_days", 7)
                await _record_max_tokens_pair(
                    redis, key, max_tokens, completion_tokens, ttl_days * 86400
                )
            except Exception as exc:
                logger.warning("[%s] G11 failed to record max_tokens pair: %s", ctx.request_id, exc)

            # Retain ephemeral feedback for backward compatibility
            ctx.params.setdefault("_token_opt_feedback", {})["max_tokens_utilization"] = utilization

        return ctx, response
