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
# Steers the model toward terser answers via a system-prompt suffix — the biggest
# uncovered savings axis (the 54.1% headline is input-only; output tokens cost far
# more per token). Ships bundled preset rulesets (lite/full/ultra) selectable by
# `level`, adapted from caveman-shrink's SKILL.md ruleset (github.com/JuliusBrussee/
# caveman, MIT — attribution in docs/oss-licenses.md), with SAFETY carve-outs so
# security warnings and destructive-action confirmations stay in normal prose.
# Default off → byte-identical when disabled. The headroom.verbosity_model hook
# (future, per-tenant trained model) still takes priority when it ships.

# Bundled terse-output presets. Each is appended to the system message when
# `verbosity_steering.level` selects it and no explicit suffix override is set.
_VERBOSITY_PRESETS: Dict[str, str] = {
    "lite": (
        "Be concise. Drop filler, hedging, and pleasantries; keep full sentences and "
        "technical accuracy. Preserve code, commands, API names, error strings, and "
        "identifiers exactly."
    ),
    "full": (
        "Answer tersely. Drop articles and filler; sentence fragments are fine. No "
        "preamble, no closing pleasantries, no restating the question, no tool-call "
        "narration. Preserve code blocks, commands, API names, and error strings "
        "byte-for-byte. Use normal, complete prose for security warnings and for "
        "confirming irreversible or destructive actions."
    ),
    "ultra": (
        "Answer in the fewest words that stay unambiguous — one word when one word "
        "suffices. Omit conjunctions where cause and effect stay clear. No preamble or "
        "closing. Preserve code, commands, API names, and error strings exactly. Use "
        "normal, complete prose only for security warnings and destructive-action "
        "confirmations."
    ),
}


def _get_verbosity_suffix(tenant_id: str, verbosity_cfg: Dict[str, Any]) -> Optional[str]:
    """Return the verbosity suffix to append, or None if nothing should be added.

    Priority:
    1. headroom.verbosity_model.predict(tenant_id) — future; no-op today
    2. verbosity_cfg["per_tenant_suffix"][tenant_id]  — static per-tenant override
    3. verbosity_cfg["default_suffix"] (non-empty)    — explicit global override
    4. _VERBOSITY_PRESETS[verbosity_cfg["level"]]     — bundled lite/full/ultra preset
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

    explicit = verbosity_cfg.get("default_suffix")
    if explicit:  # non-empty explicit override wins over the bundled preset
        return explicit

    level = str(verbosity_cfg.get("level", "")).lower()
    return _VERBOSITY_PRESETS.get(level)  # None when level unset/unknown


def verbosity_cache_tag(ctx: RequestContext) -> str:
    """Stable cache-scope tag for the active G11 verbosity suffix, or "" when
    verbosity steering is off / unconfigured. G05 folds this into its key so a
    terse-mode answer is never served to a request configured for verbose output
    (or vice-versa). "" keeps cache keys byte-identical to the pre-feature default."""
    try:
        cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G11_output", {})
        vcfg = cfg.get("verbosity_steering", {})
        if not cfg.get("enabled", False) or not vcfg.get("enabled", False):
            return ""
        suffix = _get_verbosity_suffix(getattr(ctx, "tenant_id", None) or "default", vcfg)
        if not suffix:
            return ""
        return "vb" + hashlib.sha256(suffix.encode()).hexdigest()[:8]
    except Exception:
        return ""


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


# ─── Task 4: output JSON-schema validation ────────────────────────────────────
# When the request asked for structured output (response_format json_object/json_schema,
# or a bare json_schema param), validate the model's answer is parseable JSON (and, if a
# schema was supplied, conforms to it). Modes: off (default) | flag | repair | block.
_VALIDATE_MODES = ("off", "flag", "repair", "block")


def _extract_answer(response: Dict[str, Any]) -> Optional[str]:
    """Return the first choice's assistant text, or None if it isn't a plain string
    (tool-call / multimodal answers are not JSON-validated here)."""
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    return content if isinstance(content, str) else None


def _replace_answer(response: Dict[str, Any], new_content: str) -> Dict[str, Any]:
    """Return response with the first choice's assistant content replaced (in place)."""
    try:
        response["choices"][0]["message"]["content"] = new_content
    except Exception:
        pass
    return response


def _wants_structured_output(ctx: RequestContext) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Did the request ask for JSON output? Return (wants_json, json_schema_or_None).

    Recognises the OpenAI ``response_format`` shape (json_object / json_schema) and a
    bare ``json_schema`` param. The schema (when present) is what jsonschema validates."""
    wants = False
    schema: Optional[Dict[str, Any]] = None
    rf = ctx.params.get("response_format")
    if isinstance(rf, dict):
        rtype = rf.get("type")
        if rtype in ("json_object", "json_schema"):
            wants = True
        if rtype == "json_schema":
            js = rf.get("json_schema")
            if isinstance(js, dict):
                schema = js.get("schema") or js
    bare = ctx.params.get("json_schema")
    if isinstance(bare, dict) and schema is None:
        schema, wants = bare, True
    return wants, schema


def _validate_answer(answer: str, schema: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """(is_valid, reason). Parse JSON; if a schema is given, validate against it.
    A malformed schema is treated as a validation miss (never crashes the request)."""
    try:
        parsed = json.loads(answer)
    except Exception as exc:
        return False, f"unparseable JSON ({exc})"
    if schema:
        try:
            import jsonschema  # optional dep; pinned in requirements.txt
            jsonschema.validate(parsed, schema)
        except ImportError:
            logger.warning("G11 validate_output: jsonschema not installed — skipping schema check")
            return True, "ok (schema check skipped: jsonschema missing)"
        except Exception as exc:
            return False, f"schema validation failed ({getattr(exc, 'message', exc)})"
    return True, "ok"


async def _reask(ctx: RequestContext, answer: str, schema: Optional[Dict[str, Any]],
                 max_tokens: Optional[int]) -> Optional[str]:
    """One bounded corrective LLM call to repair a malformed structured answer.

    Returns the repaired answer text, or None on any error (the caller then falls back
    to flag/block — never loops). Injectable: tests monkeypatch this to avoid a live call.
    BYOK: resolves the tenant's provider key when it can, else lets litellm use its env."""
    try:
        import litellm
    except Exception:
        return None
    model = getattr(ctx, "routed_model", None) or ctx.model
    provider_key = None
    try:
        from config_loader import get_provider_model_prefixes
        from providers.key_resolver import resolve_provider_key
        for fragment, prov in (get_provider_model_prefixes() or {}).items():
            if fragment in str(model).lower():
                provider_key = await resolve_provider_key(prov, getattr(ctx, "tenant_id", "default"), ctx)
                break
    except Exception:
        provider_key = None

    schema_hint = json.dumps(schema) if schema else "a single valid JSON object"
    repair_messages = list(ctx.messages) + [
        {"role": "assistant", "content": answer},
        {"role": "user", "content": (
            "Your previous reply was not valid JSON for the required format. Reply again "
            "with ONLY valid JSON — no prose, no markdown fences — conforming to: " + schema_hint
        )},
    ]
    kwargs: Dict[str, Any] = {}
    if provider_key:
        kwargs["api_key"] = provider_key
    rf = ctx.params.get("response_format")
    if isinstance(rf, dict):
        kwargs["response_format"] = rf
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    try:
        resp = await litellm.acompletion(model=model, messages=repair_messages, **kwargs)
        rd = resp.model_dump() if hasattr(resp, "model_dump") else resp
        return _extract_answer(rd if isinstance(rd, dict) else {})
    except Exception as exc:
        logger.warning("[%s] G11 repair re-ask failed: %s", ctx.request_id, exc)
        return None


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

        # 4. Verbosity steering — terse-output suffix (bundled lite/full/ultra presets,
        #    or an explicit per-tenant/default suffix, or a future headroom model).
        #    Appended to the system message to steer the model toward shorter answers;
        #    default off. The suffix is folded into the G05 cache key (verbosity_cache_tag)
        #    so a terse answer is never served to a verbose-configured request.
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

        # ── Task 4: output JSON-schema validation ─────────────────────────────
        # Runs before the feedback loop so a `block` withholds the malformed answer
        # (and a `repair` replaces it) before it is recorded/cached. Off by default.
        block_resp = await self._validate_output(ctx, cfg, response)
        if block_resp is not None:
            return ctx, block_resp   # block / repair-fallback-block → withhold, skip feedback

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

    async def _validate_output(
        self, ctx: RequestContext, cfg: Dict[str, Any], response: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Validate a structured-output answer. Returns a content-filter block response
        when the answer must be WITHHELD (block mode, or repair fell back to block); else
        None (annotating/repairing `response` in place). Off by default → no-op passthrough."""
        mode = str(cfg.get("validate_output", "off")).lower()
        if mode not in ("flag", "repair", "block"):
            return None  # off / unknown → passthrough (no validation, no metric)

        wants, schema = _wants_structured_output(ctx)
        if not wants:
            return None  # request didn't ask for JSON — nothing to validate
        answer = _extract_answer(response)
        if answer is None:
            return None  # tool-call / multimodal / empty answer — not validated here

        ok, reason = _validate_answer(answer, schema)
        if ok:
            return None

        # Invalid structured output.
        self._emit_schema_failure(ctx, mode)

        if mode == "block":
            return self._schema_block(ctx, cfg)

        if mode == "repair":
            max_reask = cfg.get("repair_max_tokens", ctx.params.get("max_tokens"))
            repaired = await _reask(ctx, answer, schema, max_reask)
            if repaired is not None and _validate_answer(repaired, schema)[0]:
                _replace_answer(response, repaired)
                self._annotate(response, {"validated": True, "repaired": True})
                return None
            # Bounded: exactly one re-ask. Still invalid → fall back (no loop).
            fallback = str(cfg.get("repair_fallback", "flag")).lower()
            if fallback == "block":
                return self._schema_block(ctx, cfg)
            self._annotate(response, {"validated": False, "repaired": False, "reason": reason})
            return None

        # flag
        self._annotate(response, {"validated": False, "reason": reason})
        return None

    @staticmethod
    def _annotate(response: Dict[str, Any], info: Dict[str, Any]) -> None:
        response.setdefault("_token_opt", {})["output_validation"] = info

    def _schema_block(self, ctx: RequestContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Withhold a malformed structured answer with a content-filter 200 (not cached)."""
        from guardrails import content_filter_response
        ctx.no_cache = True
        message = cfg.get(
            "validate_block_message",
            "The response did not conform to the required output schema and was withheld.",
        )
        return content_filter_response(ctx.request_id, ctx.routed_model or ctx.model, message)

    def _emit_schema_failure(self, ctx: RequestContext, mode: str) -> None:
        try:
            from middleware.quality_metrics import record_schema_failure
            record_schema_failure(getattr(ctx, "tenant_id", "default"), mode)
        except Exception as exc:  # never let metrics break the response
            logger.debug("[%s] G11 schema-failure metric emit failed: %s", ctx.request_id, exc)
