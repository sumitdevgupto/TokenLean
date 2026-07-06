"""
G06 · Model Routing & Cascading
Stage: At the Gate
Saving: 60–80% average per-query cost
Technique: Classify request complexity → route to cheapest capable model tier.
           Pluggable classifiers: heuristic | llm_judge | cascade (default) | routellm.
           User can override complexity tier per-request via params.
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import litellm

from middleware import RequestContext
from middleware import langfuse_tracing
from savings.calculator import count_messages_tokens, estimate_cost
from config_loader import get_default_model, get_known_models, get_providers
from providers import build_litellm_call

logger = logging.getLogger(__name__)
GROUP = "G06"


async def _timed_llm(ctx: RequestContext, coro):
    """Await an LLM-bearing coroutine and add its wall-clock time to
    ``ctx.llm_elapsed_ms``.

    G06's cascade tiers and judge calls are real provider calls made *inside*
    the request pipeline. Without this, that time would be booked as proxy
    overhead in the SLA split (elapsed − llm_elapsed_ms) — inflating the
    "Proxy only" latency with what is actually LLM-layer time.
    """
    t0 = time.time()
    try:
        return await coro
    finally:
        try:
            ctx.llm_elapsed_ms += (time.time() - t0) * 1000.0
        except Exception:
            pass


def _openai_key_available() -> bool:
    """True if an OpenAI key is configured (env or Secret Manager)."""
    try:
        from auth.api_key_manager import get_llm_provider_key
        return bool(get_llm_provider_key("openai"))
    except Exception:
        return False


def _is_configured_model(model: str) -> bool:
    """True if ``model`` is explicitly configured — listed in ``providers[].models`` OR
    matching a configured provider's ``model_prefixes``.

    Used by the routing-disabled / no-tiers paths so a deliberately-chosen provider model
    (e.g. ``gemini-2.5-flash``, ``mistral-small-latest``) is preserved rather than
    downgraded to ``default_model``. Without the prefix check, any non-OpenAI/Anthropic
    model not enumerated in a ``models:`` list would be silently served by the default
    OpenAI model.
    """
    if model in get_known_models():
        return True
    from config_loader import get_provider_model_prefixes
    return any(model.startswith(p) for p in get_provider_model_prefixes().keys())


def _tier_provider(model: str) -> Optional[str]:
    """Provider name that owns ``model`` via ``startswith(model_prefixes)`` — the SAME
    resolution as ``providers.get_provider_entry`` / ``main._resolve_provider``. A substring
    match is wrong here: ``openrouter/openai/…`` contains the ``openai``/``gpt`` fragment and
    would mis-resolve to OpenAI (→ the wrong / an absent key)."""
    try:
        from providers import get_provider_entry
        entry = get_provider_entry(model, get_providers())
        return entry.get("name") if entry else None
    except Exception:
        return None


def _tier_reachable(model: str) -> bool:
    """True if ``model``'s provider has a usable credential — an API key, OR ambient
    credentials (Bedrock SigV4 / Vertex ADC, whose adapter ``requires_api_key()`` is False).
    Used to avoid routing to a tier whose provider isn't configured (→ a downstream 503)."""
    if not model:
        return False
    try:
        from auth.api_key_manager import get_llm_provider_key
        from providers import get_adapter
        provider = _tier_provider(model)
        if not provider:
            return False
        if get_llm_provider_key(provider):
            return True
        # No bearer key — reachable only if the adapter uses ambient credentials.
        return not get_adapter(model, get_providers()).requires_api_key()
    except Exception as exc:
        logger.warning("G06: reachability check errored for %s: %s", model, exc)
        return False


async def _resolve_provider_key(model: str) -> Optional[str]:
    """Resolve the LLM provider API key for a given model name (startswith resolution,
    consistent with ``_tier_provider`` / ``providers.get_provider_entry``)."""
    try:
        from auth.api_key_manager import get_llm_provider_key
        provider = _tier_provider(model)
        if not provider:
            logger.warning("G06: cannot resolve provider for %s", model)
            return None
        key = get_llm_provider_key(provider)
        if not key:
            logger.warning("G06: no API key for provider %s (model %s)", provider, model)
            return None
        return key
    except Exception as exc:
        logger.warning("G06: provider key resolution error for %s: %s", model, exc)
        return None


# Deduped so the tier-reachability audit logs once per tier-config signature, not per request.
_CASCADE_TIERS_LOGGED: set = set()


def _log_cascade_tier_reachability(ctx: RequestContext, cfg: Dict[str, Any]) -> None:
    """Once per tier-config signature, log which cascade tier models are reachable, so a
    misconfigured (e.g. OpenAI-free) deployment is flagged loudly at first use — not just
    silently degraded per request. Covers the classifier tiers AND the routellm weak/strong."""
    tiers = cfg.get("tiers", {}) or {}
    routellm = cfg.get("routellm", {}) or {}
    models: List[str] = []
    for _k in ("simple", "medium", "complex"):
        models += tiers.get(_k, []) or []
    for _k in ("weak_model", "strong_model"):
        if routellm.get(_k):
            models.append(routellm[_k])
    models = [m for m in dict.fromkeys(models) if m]  # dedupe, preserve order
    if not models:
        return
    sig = tuple(models)
    if sig in _CASCADE_TIERS_LOGGED:
        return
    _CASCADE_TIERS_LOGGED.add(sig)
    unreachable = [m for m in models if not _tier_reachable(m)]
    on_unreach = str(cfg.get("on_unreachable_tier", "fallback")).lower()
    if unreachable:
        logger.warning(
            "[%s] G06 cascade tier check: %d/%d tier model(s) UNREACHABLE (no API key / ambient "
            "creds for their provider): %s. on_unreachable_tier=%s → requests routed to these will "
            "%s. Fix: point the tier at a keyed provider's model, or configure the missing key.",
            ctx.request_id, len(unreachable), len(models), ", ".join(unreachable), on_unreach,
            "return a clean 503" if on_unreach == "error" else "fall back to the requested model",
        )
    else:
        logger.info(
            "[%s] G06 cascade tier check: all %d tier model(s) reachable",
            ctx.request_id, len(models),
        )

_COMPLEX_KEYWORDS = re.compile(
    r"\b(analyse|analyze|explain|compare|evaluate|synthesise|synthesize|"
    r"design|architect|critique|debate|research|hypothesis|strategy|"
    r"algorithm|implement|refactor|optimise|optimize)\b",
    re.IGNORECASE,
)
_SIMPLE_KEYWORDS = re.compile(
    r"\b(what is|who is|when|where|define|list|translate|summarise|summarize|"
    r"convert|format|spell|count|calculate|yes or no)\b",
    re.IGNORECASE,
)


def _classify_heuristic(messages: List[Dict], params: Dict) -> Tuple[str, float]:
    """
    Heuristic complexity classifier with confidence score.
    Returns (tier, confidence) where tier is 'simple' | 'medium' | 'complex'.

    Complexity is judged from the user/non-system turns only. The system prompt is
    fixed infrastructure (role, policies, formatting rules) whose length says nothing
    about how hard the current query is. Counting it made every request with a long
    system prompt cross the word-count threshold and escalate to the most expensive
    tier — e.g. a one-line FAQ behind a 700-token policy prompt being routed to o1,
    which inflated cost instead of saving it.
    """
    query_messages = [
        m for m in messages
        if m.get("role") != "system" and isinstance(m.get("content"), str)
    ]
    # Fall back to all string content if there are no non-system turns.
    if not query_messages:
        query_messages = [m for m in messages if isinstance(m.get("content"), str)]
    text = " ".join(m.get("content", "") for m in query_messages)
    word_count = len(text.split())

    if word_count > 500 or _COMPLEX_KEYWORDS.search(text):
        return "complex", 0.90
    if word_count < 80 or _SIMPLE_KEYWORDS.search(text):
        return "simple", 0.90
    return "medium", 0.50


def _classify_complexity(messages: List[Dict], params: Dict) -> str:
    """Backward-compatible alias: returns tier string only."""
    tier, _ = _classify_heuristic(messages, params)
    return tier


_JUDGE_SYSTEM_PROMPT = (
    "You are a request-complexity classifier.\n"
    "- simple: straightforward queries, factual lookups, definitions, simple arithmetic\n"
    "- medium: comparisons, moderate analysis, structured reasoning\n"
    "- complex: deep analysis, synthesis, design, architecture, strategy, research\n\n"
    "Respond ONLY with JSON: {\"tier\":\"simple|medium|complex\",\"confidence\":0.0-1.0}"
)


async def _classify_llm_judge(
    messages: List[Dict], params: Dict, cfg: Dict[str, Any]
) -> str:
    """
    Call a cheap judge model to classify complexity.
    Returns tier string; falls back to heuristic on any error/timeout.
    """
    judge_model = cfg.get("judge_model", "")
    if not judge_model:
        tier, _ = _classify_heuristic(messages, params)
        return tier

    timeout_ms = cfg.get("judge_timeout_ms", 2000)

    # Build compact user prompt from messages
    user_text = " ".join(
        m.get("content", "") for m in messages if isinstance(m.get("content"), str)
    )
    user_text = (user_text[:800] + "...") if len(user_text) > 800 else user_text

    judge_messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": f"Classify this request:\n{user_text}"},
    ]

    try:
        _judge_key = await _resolve_provider_key(judge_model)
        _judge_model, _judge_kwargs = build_litellm_call(judge_model, get_providers(), _judge_key)
        response = await asyncio.wait_for(
            litellm.acompletion(
                model=_judge_model,
                messages=judge_messages,
                **_judge_kwargs,
                max_tokens=60,
                temperature=0.0,
            ),
            timeout=timeout_ms / 1000,
        )
        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        # Some models wrap JSON in markdown code blocks
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        parsed = json.loads(content)
        tier = parsed.get("tier", "").lower().strip()
        if tier in ("simple", "medium", "complex"):
            return tier
    except asyncio.TimeoutError:
        logger.warning("G06 llm_judge timeout after %dms, falling back to heuristic", timeout_ms)
    except json.JSONDecodeError as exc:
        logger.warning("G06 llm_judge JSON parse error: %s, falling back to heuristic", exc)
    except Exception as exc:
        logger.warning("G06 llm_judge error: %s, falling back to heuristic", exc)

    tier, _ = _classify_heuristic(messages, params)
    return tier


async def _classify_cascade(
    messages: List[Dict], params: Dict, cfg: Dict[str, Any]
) -> str:
    """
    Fast heuristic first; escalate to llm_judge only when confidence is low
    and a judge_model is configured.
    """
    tier, confidence = _classify_heuristic(messages, params)
    threshold = cfg.get("cascade_confidence_threshold", 0.70)
    judge_model = cfg.get("judge_model", "")
    if confidence >= threshold or not judge_model:
        return tier
    return await _classify_llm_judge(messages, params, cfg)


async def _execute_three_tier_cascade(
    ctx: RequestContext, tiers: Dict[str, List[str]], cfg: Dict[str, Any]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    True 3-tier cascading execution:
    1. Try cheap model (simple tier)
    2. Evaluate confidence score
    3. Escalate to medium/complex if confidence low
    4. Cost-based rollback if escalation fails

    Returns (final_model, response_dict) or (None, error_dict) on failure.
    """
    simple_models = tiers.get("simple", [])
    medium_models = tiers.get("medium", [])
    complex_models = tiers.get("complex", [])

    if not simple_models:
        logger.warning("G06 cascade: no simple models configured, skipping cascade")
        return None, {"error": "no_simple_models"}

    confidence_threshold = cfg.get("cascade_confidence_threshold", 0.70)
    judge_model = cfg.get("judge_model", "")
    max_escalation_cost = cfg.get("max_escalation_cost_usd", 0.01)

    # Tier 1: Try cheap model
    tier1_model = simple_models[0]
    tier1_cost = estimate_cost(ctx.current_token_count, 0, tier1_model)

    try:
        provider_key = await _resolve_provider_key(tier1_model)
        if not provider_key:
            return None, {"error": "provider_resolution_failed"}

        # Call tier1 model — preserve original max_tokens and temperature from user request
        tier1_max_tokens = ctx.params.get("max_tokens") or 512
        tier1_temperature = ctx.params.get("temperature", 0.0)
        _t1_model, _t1_kwargs = build_litellm_call(tier1_model, get_providers(), provider_key)
        tier1_response = await _timed_llm(ctx, litellm.acompletion(
            model=_t1_model,
            messages=ctx.messages,
            **_t1_kwargs,
            max_tokens=tier1_max_tokens,
            temperature=tier1_temperature,
        ))

        # Evaluate confidence using judge model or heuristic
        if judge_model:
            confidence = await _timed_llm(ctx, _evaluate_response_confidence(
                ctx.messages, tier1_response, judge_model
            ))
        else:
            # Fallback to heuristic confidence (returns tier, confidence)
            _, confidence = _classify_heuristic(ctx.messages, ctx.params)

        logger.debug(
            "G06 cascade tier1: model=%s confidence=%.2f threshold=%.2f",
            tier1_model, confidence, confidence_threshold
        )

        # If confidence is high, return tier1 result
        if confidence >= confidence_threshold:
            logger.info("G06 cascade: using tier1 model %s (confidence %.2f)", tier1_model, confidence)
            return tier1_model, tier1_response.model_dump() if hasattr(tier1_response, "model_dump") else dict(tier1_response)

        # Track best response so far — tier3 failure falls back to this, not always tier1
        best_model = tier1_model
        best_response = tier1_response

        # Tier 2: Escalate to medium model
        if medium_models:
            tier2_model = medium_models[0]
            tier2_cost = estimate_cost(ctx.current_token_count, 0, tier2_model)
            escalation_cost = tier2_cost - tier1_cost

            if escalation_cost > max_escalation_cost:
                logger.warning(
                    "G06 cascade: tier2 cost $%.6f exceeds threshold $%.6f, using tier1",
                    escalation_cost, max_escalation_cost
                )
                return tier1_model, tier1_response.model_dump() if hasattr(tier1_response, "model_dump") else dict(tier1_response)

            try:
                tier2_provider_key = await _resolve_provider_key(tier2_model) or provider_key
                _t2_model, _t2_kwargs = build_litellm_call(tier2_model, get_providers(), tier2_provider_key)
                tier2_response = await _timed_llm(ctx, litellm.acompletion(
                    model=_t2_model,
                    messages=ctx.messages,
                    **_t2_kwargs,
                    **{k: v for k, v in ctx.params.items() if not k.startswith("_") and not k.startswith("x_")},
                ))
                logger.info("G06 cascade: escalated to tier2 model %s", tier2_model)
                # Tier2 succeeded — promote it as the best fallback before confidence check
                best_model = tier2_model
                best_response = tier2_response
                # Re-evaluate confidence after tier2; if still low, try tier3
                if judge_model:
                    confidence2 = await _timed_llm(ctx, _evaluate_response_confidence(ctx.messages, tier2_response, judge_model))
                else:
                    _, confidence2 = _classify_heuristic(ctx.messages, ctx.params)
                if confidence2 >= confidence_threshold:
                    logger.info("G06 cascade: using tier2 model %s (confidence %.2f)", tier2_model, confidence2)
                    return tier2_model, tier2_response.model_dump() if hasattr(tier2_response, "model_dump") else dict(tier2_response)
            except Exception as exc:
                logger.warning("G06 cascade tier2 failed: %s, rolling back to tier1", exc)
                return tier1_model, tier1_response.model_dump() if hasattr(tier1_response, "model_dump") else dict(tier1_response)

        # Tier 3: Escalate to complex model
        if complex_models:
            tier3_model = complex_models[0]
            tier3_cost = estimate_cost(ctx.current_token_count, 0, tier3_model)
            escalation_cost = tier3_cost - tier1_cost

            if escalation_cost > max_escalation_cost:
                logger.warning(
                    "G06 cascade: tier3 cost $%.6f exceeds threshold $%.6f, using tier1",
                    escalation_cost, max_escalation_cost
                )
                return tier1_model, tier1_response.model_dump() if hasattr(tier1_response, "model_dump") else dict(tier1_response)

            try:
                tier3_provider_key = await _resolve_provider_key(tier3_model) or provider_key
                _t3_model, _t3_kwargs = build_litellm_call(tier3_model, get_providers(), tier3_provider_key)
                tier3_response = await _timed_llm(ctx, litellm.acompletion(
                    model=_t3_model,
                    messages=ctx.messages,
                    **_t3_kwargs,
                    **{k: v for k, v in ctx.params.items() if not k.startswith("_") and not k.startswith("x_")},
                ))
                logger.info("G06 cascade: escalated to tier3 model %s", tier3_model)
                return tier3_model, tier3_response.model_dump() if hasattr(tier3_response, "model_dump") else dict(tier3_response)
            except Exception as exc:
                logger.warning("G06 cascade tier3 failed: %s, rolling back to best tier (%s)", exc, best_model)
                return best_model, best_response.model_dump() if hasattr(best_response, "model_dump") else dict(best_response)

        # No escalation path available, return tier1
        return tier1_model, tier1_response.model_dump() if hasattr(tier1_response, "model_dump") else dict(tier1_response)

    except Exception as exc:
        logger.error("G06 cascade tier1 failed: %s", exc)
        return None, {"error": str(exc)}


async def _evaluate_response_confidence(
    messages: List[Dict], response: Any, judge_model: str
) -> float:
    """
    Use a judge model to evaluate confidence in the tier1 response.
    Returns confidence score 0.0-1.0.
    """
    try:
        response_text = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        ) if isinstance(response, dict) else str(response)

        judge_prompt = (
            "Evaluate the confidence of this LLM response on a scale of 0.0 to 1.0.\n"
            "Consider: factual accuracy, completeness, and coherence.\n"
            "Respond ONLY with a JSON number: {\"confidence\": 0.0-1.0}\n\n"
            f"Response to evaluate:\n{response_text[:500]}"
        )

        _judge_key = await _resolve_provider_key(judge_model)
        _judge_model, _judge_kwargs = build_litellm_call(judge_model, get_providers(), _judge_key)
        judge_response = await asyncio.wait_for(
            litellm.acompletion(
                model=_judge_model,
                messages=[{"role": "user", "content": judge_prompt}],
                **_judge_kwargs,
                max_tokens=30,
                temperature=0.0,
            ),
            timeout=2.0,
        )

        content = (
            judge_response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        parsed = json.loads(content)
        confidence = float(parsed.get("confidence", 0.5))
        return max(0.0, min(1.0, confidence))

    except Exception as exc:
        logger.warning("G06 confidence evaluation failed: %s, defaulting to 0.5", exc)
        return 0.5


async def _classify_routellm(
    messages: List[Dict], params: Dict, cfg: Dict[str, Any]
) -> str:
    """
    Call RouteLLM sidecar to get routing decision, then map to tier.
    Returns tier string; falls back to heuristic on any error/timeout.
    """
    routellm_cfg = cfg.get("routellm", {})
    sidecar_url = routellm_cfg.get("url", "")
    timeout_ms = routellm_cfg.get("timeout_ms", 500)
    router = routellm_cfg.get("router", "mf")
    threshold = routellm_cfg.get("threshold", 0.11593)
    model_map = routellm_cfg.get("model_map", {"weak": "simple", "strong": "complex"})
    weak_model = routellm_cfg.get("weak_model", "")
    strong_model = routellm_cfg.get("strong_model", "")

    if not sidecar_url or not weak_model or not strong_model:
        logger.warning(
            "G06 RouteLLM missing config: url=%r, weak_model=%r, strong_model=%r — falling back to heuristic",
            sidecar_url, weak_model, strong_model,
        )
        tier, _ = _classify_heuristic(messages, params)
        return tier

    # P8: the mf/sw_ranking routers compute OpenAI embeddings in the sidecar. If no OpenAI
    # key is configured (e.g. an Anthropic/Gemini-only deployment), degrade to the causal_llm
    # router (a local classifier, no embeddings) so routing still works. See docs/config-reference.md.
    if router in ("mf", "sw_ranking") and not _openai_key_available():
        logger.warning(
            "G06 RouteLLM: router=%r needs OpenAI embeddings but no OpenAI key is set — "
            "falling back to the causal_llm router", router,
        )
        router = "causal_llm"

    payload = {
        "messages": messages,
        "router": router,
        "threshold": threshold,
        "strong_model": strong_model,
        "weak_model": weak_model,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
            response = await client.post(f"{sidecar_url}/route", json=payload)
            response.raise_for_status()
            result = response.json()
            routed_model = result.get("routed_model")
            confidence = result.get("confidence", 0.0)
            reason = result.get("reason", "")

            logger.debug(
                "G06 RouteLLM routed to %s (confidence=%.2f, reason=%s)",
                routed_model,
                confidence,
                reason,
            )

            # Map binary output to tier
            if routed_model == weak_model:
                return model_map.get("weak", "simple")
            elif routed_model == strong_model:
                return model_map.get("strong", "complex")
            else:
                logger.warning("G06 RouteLLM returned unknown model %s, falling back to heuristic", routed_model)

    except httpx.TimeoutException:
        logger.warning("G06 RouteLLM sidecar timeout after %dms, falling back to heuristic", timeout_ms)
    except httpx.HTTPError as exc:
        logger.warning("G06 RouteLLM sidecar HTTP error: %s, falling back to heuristic", exc)
    except Exception as exc:
        logger.warning("G06 RouteLLM sidecar error: %s, falling back to heuristic", exc)

    tier, _ = _classify_heuristic(messages, params)
    return tier


async def _dispatch_classifier(
    ctx: RequestContext, cfg: Dict[str, Any]
) -> str:
    """Route to the configured classifier."""
    classifier = cfg.get("classifier", "cascade")
    if classifier == "heuristic":
        tier, _ = _classify_heuristic(ctx.messages, ctx.params)
        return tier
    if classifier == "llm_judge":
        return await _classify_llm_judge(ctx.messages, ctx.params, cfg)
    if classifier == "cascade":
        return await _classify_cascade(ctx.messages, ctx.params, cfg)
    if classifier == "routellm":
        return await _classify_routellm(ctx.messages, ctx.params, cfg)
    # Unknown classifier → default to cascade
    logger.warning("G06 unknown classifier %s, defaulting to cascade", classifier)
    return await _classify_cascade(ctx.messages, ctx.params, cfg)




class G06Routing:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G6_routing", {})
        if not cfg.get("enabled", False):
            # Routing disabled → no-op for explicitly-chosen, config-known models
            # (preserves the developer's model, e.g. o4-mini, so reasoning stays
            # measurable). Only unknown models fall back to the configured default.
            if not _is_configured_model(ctx.model):
                default_model = get_default_model()
                # Only downgrade to a real configured default; never blank the
                # model when default is unset/misconfigured (preserve original).
                if default_model and ctx.model != default_model:
                    ctx.model = default_model
                    ctx.routed_model = default_model
                    logger.debug(
                        "[%s] G06 routing disabled, unknown model → default: %s",
                        ctx.request_id,
                        default_model,
                    )
            return ctx

        tiers: Dict[str, List[str]] = cfg.get("tiers", {})
        if not tiers:
            # No tiers configured → same no-op rule for config-known models.
            if not _is_configured_model(ctx.model):
                default_model = get_default_model()
                # Only downgrade to a real configured default; never blank the
                # model when default is unset/misconfigured (preserve original).
                if default_model and ctx.model != default_model:
                    ctx.model = default_model
                    ctx.routed_model = default_model
                    logger.debug(
                        "[%s] G06 no tiers configured, unknown model → default: %s",
                        ctx.request_id,
                        default_model,
                    )
            return ctx

        # Flag any unreachable cascade tier models loudly (once per config) at first use.
        _log_cascade_tier_reachability(ctx, cfg)

        model_requested = ctx.model  # snapshot before any mutation
        selected_model = ctx.model
        user_override = False
        complexity = ""  # populated when a classifier runs

        # 1. User override: bypass classifier entirely
        user_complexity = ctx.params.get("complexity") or ctx.params.get("x_complexity")
        if isinstance(user_complexity, str) and user_complexity in ("simple", "medium", "complex"):
            candidates = tiers.get(user_complexity, [])
            if candidates:
                selected_model = candidates[0]
                ctx.savings.routing_mode = "user_override"
                user_override = True
                logger.debug(
                    "[%s] G06 user override: complexity=%s → %s",
                    ctx.request_id,
                    user_complexity,
                    selected_model,
                )

        # 2. Classifier dispatch
        else:
            classifier = cfg.get("classifier", "cascade")
            
            # Use true 3-tier cascade when classifier=cascade and cascade_execution=true
            if classifier == "cascade" and cfg.get("cascade_execution", False):
                cascade_model, cascade_response = await _execute_three_tier_cascade(ctx, tiers, cfg)
                if cascade_model and "error" not in cascade_response:
                    selected_model = cascade_model
                    ctx.savings.routing_mode = "cascade_execution"
                    ctx.cascade_response = cascade_response  # Store for direct return in main.py
                    logger.debug(
                        "[%s] G06 cascade execution: %s",
                        ctx.request_id,
                        selected_model,
                    )
                else:
                    # Fallback to standard classification
                    complexity = await _timed_llm(ctx, _dispatch_classifier(ctx, cfg))
                    candidates = tiers.get(complexity, tiers.get("medium", []))
                    if candidates:
                        selected_model = candidates[0]
                    ctx.savings.routing_mode = f"{classifier}_fallback"
            else:
                complexity = await _timed_llm(ctx, _dispatch_classifier(ctx, cfg))
                candidates = tiers.get(complexity, tiers.get("medium", []))
                if candidates:
                    selected_model = candidates[0]
                ctx.savings.routing_mode = classifier

        # 2b. Reachability guard (P8 hardening): never hand main.py a routed tier model whose
        # provider has no usable credential — that becomes a downstream 503. Per
        # G6_routing.on_unreachable_tier: 'fallback' (default) serves the caller's OWN model
        # (they chose it, so its key is present); 'error' keeps the unreachable model so main.py's
        # provider-key guard returns a clean 503 with the reason logged here.
        if selected_model and selected_model != model_requested and not _tier_reachable(selected_model):
            _provider = _tier_provider(selected_model) or "?"
            _mode = str(cfg.get("on_unreachable_tier", "fallback")).lower()
            if _mode == "error":
                logger.error(
                    "[%s] G06: routed tier model %r is UNREACHABLE — provider %r has no API key and no "
                    "ambient credentials; on_unreachable_tier=error → the request will 503. Configure the "
                    "key, or point this tier at a keyed provider's model.",
                    ctx.request_id, selected_model, _provider,
                )
                # keep selected_model as-is → main.py's provider-key guard raises a clean 503
            else:
                logger.warning(
                    "[%s] G06: routed tier model %r is UNREACHABLE — provider %r has no API key/ambient "
                    "creds; falling back to the requested model %r (cost-routing no-op this request). "
                    "Configure the key or set that tier to a keyed provider's model to restore routing.",
                    ctx.request_id, selected_model, _provider, model_requested,
                )
                selected_model = model_requested
                ctx.savings.routing_mode = (
                    getattr(ctx.savings, "routing_mode", None) or "route"
                ) + "+tier_unreachable_fallback"

        # 3. Savings logic
        if selected_model != ctx.model:
            # Input-only estimate, used ONLY for the human-readable step description below.
            # Do NOT mutate ctx.savings.cost_{baseline,actual}_usd here: those are owned by
            # G18, which computes both on a consistent input+output basis at response time
            # (baseline at model_requested, actual at routed_model). Writing an input-only
            # value here previously left baseline output-less while G18 overwrote actual
            # with output included → "actual" looked ~200x "baseline".
            baseline_cost = estimate_cost(ctx.current_token_count, 0, ctx.model)
            routed_cost = estimate_cost(ctx.current_token_count, 0, selected_model)
            cost_saving = max(0.0, baseline_cost - routed_cost)

            ctx.routed_model = selected_model
            ctx.savings.routed_model = selected_model

            if user_override:
                routing_detail = "user_override"
            else:
                routing_detail = f"classifier={cfg.get('classifier', 'cascade')}, complexity={complexity}"

            ctx.savings.add_step(
                GROUP,
                f"Routed {ctx.model} → {selected_model} ({routing_detail}, "
                f"cost saving ≈ ${cost_saving:.6f})",
                ctx.current_token_count,
                ctx.current_token_count,  # token count unchanged, cost changes
            )
            logger.debug(
                "[%s] G06 routed %s → %s (%s)",
                ctx.request_id,
                ctx.model,
                selected_model,
                routing_detail,
            )

        langfuse_tracing.add_span(
            ctx,
            name="G06-routing",
            span_input={"model_requested": model_requested},
            output={"routed_model": ctx.routed_model},
            metadata={
                "routing_mode": ctx.savings.routing_mode,
                "complexity": complexity if not user_override else "user_override",
                "user_override": user_override,
                "routellm_confidence": ctx.savings.routellm_confidence,
            },
        )

        return ctx
