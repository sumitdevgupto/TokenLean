"""
G11 · Output Length & Format Control
Stage: Inside the LLM (parameter injection)
Saving: not measured as a token saving. The ablation measured G11 at +0.00% input
  tokens, and the only output reduction ever observed from the deleted auto-tighten
  heuristic was missing answer content on billed 200s. Treat the cap as a spend
  CEILING an operator opts into, not as a saving this group earns.
Technique:
  1. Enforce max_tokens from OBSERVED completion-size evidence: p95 of completed
     answers × tighten_multiplier. No evidence → no cap (output length is not
     derivable from input length), unless `fallback_max_tokens` is configured.
     Ships OFF (`max_tokens_auto_tighten: false`) since 2026-09-08: capping from
     evidence a request cannot be matched to is how it cut 7-22% of answers
     mid-sentence on billed 200s. When an operator opts in, a cap is only derived
     from a bucket the caller identified (`workflow_id`/`template_id`), over the
     whole retained history rather than the last ten answers, and never below a
     sticky floor that any truncation of ours raises.
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

_DEFAULT_MODEL_MAX_TOKENS = 4096  # Safe default for unknown models
_DEFAULT_TRUNCATION_BACKOFF = 2.0
# Headroom over the observed quantile. 1.2 shipped until 2026-09-08 and was BELOW the
# measured same-prompt spread: on DS1 at temperature 0 + seed 42, gpt-4o-mini's answer to
# an IDENTICAL prompt exceeded 1.2x another repeat's length in 15 of 108 ordered pairs
# (13.9%), with a per-request spread up to 2.03x. A margin under the model's own variance
# truncates answers it has already been shown to produce.
# HONEST CLAIM, by this comment's own evidence: 2.0 is BELOW the 2.03x worst observed
# spread, and `history_max_entries: 200` with `tighten_quantile: 0.95` leaves ~10 of 200
# retained answers above the percentile by construction. So the multiplier REDUCES
# truncation and, with the sticky floor, RECOVERS from it — it does not stop it. The first
# occurrence in a bucket can still be cut, on a billed 200. Raising 2.0 would need a
# measured truncation rate on a real workload, or a per-bucket max/p99 estimate; neither
# exists yet, so the number is not raised on a guess.
_DEFAULT_TIGHTEN_MULTIPLIER = 2.0
_DEFAULT_HISTORY_MIN_ENTRIES = 5
_DEFAULT_HISTORY_MAX_ENTRIES = 200
# A cap below this is not evidence, it is a degenerate sample — G11 declines to cap rather
# than clamping UP to it. The old `max(64, ...)` clamp made 64 the APPLIED value whenever
# the estimate collapsed (three readiness probes were served capped at 64/74 on 2026-09-07),
# which is the floor deciding the answer length instead of the evidence.
# TRADE-OFF, stated rather than hidden: 64 is inherited from that old clamp, not derived
# from a measurement, and it is used here with the OPPOSITE meaning. A genuinely short
# workload — a classifier whose p95 is 20, so 20 × 2.0 = 40 — is therefore never capped,
# and that is the feature's best case being refused. Direction on the published number:
# DOWN (less capping, so less output-cost reduction). Kept because the alternative is
# indistinguishable from a degenerate sample, and under charter 2.1 quality wins: not
# capping costs money, capping wrongly costs the customer their answer.
_MIN_SANE_CAP = 64
# `_get_sticky_floor` returns this when the floor could NOT be read, which is not the same
# as "no floor stored". See its docstring: the two must not collapse into None.
_FLOOR_UNREADABLE = object()
# Longest caller-supplied discriminator kept verbatim in a Redis key. Beyond it the value is
# hashed, as `g05_cache._system_scope_tag` does, so a caller cannot choose our key length.
_MAX_BUCKET_ID_CHARS = 48
# `workflow_id`/`template_id` both absent → one catch-all bucket per tenant holding every
# workload's answer sizes. A long-form request then inherits a cap learned from short-form
# ones. G11 will not cap from it; see `_history_key`.
_CATCH_ALL_BUCKET = "default"


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


def _is_streaming(ctx: RequestContext) -> bool:
    """True when the caller asked for a streamed response."""
    value = ctx.params.get("stream")
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _history_key(ctx: RequestContext) -> Optional[str]:
    """Redis key for this request's completion-size evidence, or None when the request
    carries no workload discriminator at all.

    Returning None for the catch-all is the fix for the 2026-09-08 truncation defect. Both
    ids default to "default", and neither is set by ordinary traffic, by any readiness probe
    or by any pitch dataset — so every workload a tenant runs pooled into ONE bucket and a
    long-form request was capped from short-form answers. Live evidence: within a single
    readiness sweep that one bucket produced caps of 64 and 367, and 6 of 27 probes came back
    `finish_reason=length` on billed 200s. A cap must be learned from answers to work of the
    same shape, so with no way to tell the shapes apart G11 declines to cap.
    """
    workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id") or _CATCH_ALL_BUCKET
    template_id = ctx.params.get("template_id") or ctx.params.get("x_template_id") or _CATCH_ALL_BUCKET
    if workflow_id == _CATCH_ALL_BUCKET and template_id == _CATCH_ALL_BUCKET:
        return None
    wf = _bucket_tag(workflow_id)
    tpl = _bucket_tag(template_id)
    return f"{getattr(ctx, 'redis_prefix', '')}tok_opt:max_tokens_history:{wf}:{tpl}"


def _bucket_tag(value: Any) -> str:
    """Key-safe form of a caller-supplied bucket id.

    These values come straight off the wire, and until 2026-09-09 they were interpolated
    into a Redis key verbatim — no length bound, no charset limit. A caller could choose
    both the length and the shape of a key on shared Memorystore. Long or odd values are
    hashed to a 16-char digest, exactly as `g05_cache` hashes its system-prompt scope tag.
    Short, plainly-safe ids stay legible so an operator can read a key in `redis-cli`.
    (Cardinality is a separate problem — a caller stamping a unique id per request still
    makes a bucket per request. That is why recording is gated on the auto-tighten switch;
    see `process_response`.)
    """
    text = str(value)
    if len(text) <= _MAX_BUCKET_ID_CHARS and all(
        c.isalnum() or c in "-_." for c in text
    ):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _floor_key(history_key: str) -> str:
    """Sticky-floor key for a history bucket — same tenant prefix and discriminator."""
    return history_key.replace(
        "tok_opt:max_tokens_history:", "tok_opt:max_tokens_floor:", 1
    )


async def _get_sticky_floor(redis, key: str):
    """Lowest cap this bucket is still allowed to apply.

    Returns an int (a floor is stored), None (no floor stored) or `_FLOOR_UNREADABLE`
    (the read itself failed). The caller MUST keep those three apart.

    Raised by `_raise_sticky_floor` whenever a G11-set cap truncated an answer. It exists
    because the percentile alone OSCILLATES: the escalated `truncated` entry is one sample
    among many and ages out, so the estimate falls back to where it was and cuts the same
    request again (observed on DS1: ds1-08 cut at 232, raised to 256, back to 224, cut
    again). A floor is monotone within its TTL, so the loop converges instead.

    Until 2026-09-09 this swallowed every exception and returned None, and the caller then
    applied the percentile cap anyway. Its docstring claimed an absent floor "only means a
    less-informed cap, never a wrong one". That was FALSE and in the one direction that
    hurts: the floor exists precisely because that percentile cap has already been proven
    to truncate THIS bucket, so a transient GET failure — or an eviction under maxmemory —
    reinstated the cap that cut the last answer. A read error is now distinguishable and
    the caller declines to cap on it: fail CLOSED, the same asymmetry as
    `g32_tool_eligibility.authorize_dispatch`, and safe for the same reason — declining
    leaves the request uncapped, which is the ordinary shipped path, not an outage.
    """
    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("G11 could not read the max_tokens floor for %s: %s", key, exc)
        return _FLOOR_UNREADABLE
    try:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        value = int(raw)
        return value if value > 0 else None
    except Exception as exc:
        # A stored value we cannot parse is not a read failure — there is no floor to
        # honour, and the bucket's next truncation rewrites it. Safe to treat as absent.
        logger.warning("G11 ignored an unparseable max_tokens floor at %s: %s", key, exc)
        return None


# Compare-and-set for the floor. Chosen over `ZADD key GT CH` on a single-member sorted
# set for one concrete reason: the floor key already exists as a STRING in every deployment
# that has ever truncated, and ZADD against a string key raises WRONGTYPE forever after —
# the switch would need a migration and would silently disable the floor until the TTL ran
# out. EVAL keeps the type, is one round trip, and is atomic on the server, which is what
# the invariant needs. Non-atomic read-modify-write was the defect: two workers read 100,
# one raised to 464, the other to 150, last write won, and the floor was LOWERED against a
# docstring asserting it never is. Reachable on `max_instances > 1` and on multiple uvicorn
# workers (the #39/#40 precedents).
_FLOOR_CAS_LUA = """
local cur = redis.call('GET', KEYS[1])
local want = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
if cur then
  local have = tonumber(cur)
  if have and have >= want then
    redis.call('EXPIRE', KEYS[1], ttl)
    return have
  end
end
redis.call('SET', KEYS[1], want, 'EX', ttl)
return want
"""


async def _raise_sticky_floor(redis, key: str, value: int, ttl_seconds: int) -> None:
    """Raise the bucket's floor to `value`, never lower it — ATOMICALLY.

    Refreshes the TTL so a bucket that keeps truncating keeps its floor; a bucket that
    stops simply lets it expire. The monotonicity is enforced server-side by
    `_FLOOR_CAS_LUA`; the read-modify-write fallback below is only for a Redis that
    refuses EVAL, and it logs that the invariant is best-effort there.
    """
    value = int(value)
    try:
        await redis.eval(_FLOOR_CAS_LUA, 1, key, value, int(ttl_seconds))
        return
    except Exception as exc:
        logger.warning(
            "G11 could not raise the max_tokens floor atomically (%s); falling back to a "
            "read-modify-write, which is NOT safe against a concurrent raise", exc,
        )
    try:
        current = await _get_sticky_floor(redis, key)
        if isinstance(current, int) and current >= value:
            await redis.expire(key, ttl_seconds)
            return
        if current is _FLOOR_UNREADABLE:
            # Cannot prove we are not lowering it. Raising blindly could LOWER a higher
            # floor; leaving it alone only costs one slower convergence.
            return
        await redis.set(key, value, ex=ttl_seconds)
    except Exception as exc:
        logger.warning("G11 failed to raise the max_tokens floor: %s", exc)


async def _get_historical_p95(
    redis, key: str, quantile: float = 0.95,
    min_entries: int = _DEFAULT_HISTORY_MIN_ENTRIES,
    truncation_backoff: float = _DEFAULT_TRUNCATION_BACKOFF,
) -> Optional[int]:
    """p95 of OBSERVED completion sizes from the Redis ZSET history.

    The evidence is completion_tokens, never the caps that were applied — a p95
    over past caps only echoes past caps back. Entries marked `truncated` (an
    answer cut off by a G11-set cap) count as `completion × truncation_backoff`:
    a truncation only proves the answer wanted MORE than the cap, so it must push
    the estimate up, never anchor it down.

    Reads the WHOLE retained history. Until 2026-09-08 it read `0, min_entries*2-1`
    — the 10 most recent entries — which is not a percentile: over 10 samples
    `int((10-1)*0.95)` is index 8, the second-largest, and any long answer aged out
    after ten requests. On DS1 the true pooled p95 was 257 while that window
    produced caps of 224-260, and it is what discarded the truncation escalation
    before it could take effect. The retained set is bounded at write time
    (`history_max_entries`), so the read is bounded too.
    """
    try:
        entries = await redis.zrevrange(key, 0, -1, withscores=False)
        if not entries or len(entries) < min_entries:
            return None
        completion_values: List[int] = []
        for raw in entries:
            try:
                data = json.loads(raw)
                ct = data.get("completion_tokens")
                if not isinstance(ct, int) or ct <= 0:
                    continue
                if data.get("truncated"):
                    completion_values.append(int(ct * max(1.0, truncation_backoff)))
                else:
                    completion_values.append(ct)
            except Exception:
                continue
        if len(completion_values) < min_entries:
            return None
        sorted_vals = sorted(completion_values)
        idx = int((len(sorted_vals) - 1) * quantile)
        return sorted_vals[idx]
    except Exception as exc:
        logger.warning("G11 failed to compute historical p95: %s", exc)
        return None


async def _record_max_tokens_pair(
    redis, key: str, max_tokens: int, completion_tokens: int, ttl_seconds: int,
    truncated: bool = False, max_entries: int = _DEFAULT_HISTORY_MAX_ENTRIES,
) -> None:
    """Record a (max_tokens, completion_tokens) pair to Redis ZSET with TTL.

    Trims to the newest `max_entries` by rank so the whole-set read in
    `_get_historical_p95` stays bounded — the bound lives here, at write time, rather
    than in the read, so the percentile is taken over a real population instead of a
    ten-entry sliding window.
    """
    try:
        pair: Dict[str, Any] = {"max_tokens": max_tokens, "completion_tokens": completion_tokens}
        if truncated:
            pair["truncated"] = True
        member = json.dumps(pair)
        score = time.time()
        await redis.zadd(key, {member: score})
        await redis.expire(key, ttl_seconds)
        if max_entries and max_entries > 0:
            await redis.zremrangebyrank(key, 0, -(int(max_entries) + 1))
    except Exception as exc:
        logger.warning("G11 failed to record max_tokens history: %s", exc)



# ─── Verbosity steering (T20) ────────────────────────────────────────────────
# Steers the model toward terser answers via a system-prompt suffix — the biggest
# uncovered savings axis (the 54.1% headline is input-only; output tokens cost far
# more per token). Ships bundled preset rulesets (lite/full/ultra) selectable by
# `level`, adapted from caveman-shrink's SKILL.md ruleset (github.com/JuliusBrussee/
# caveman, MIT — attribution in docs/oss-licenses.md), with SAFETY carve-outs so
# security warnings and destructive-action confirmations stay in normal prose.
# Default off → byte-identical when disabled.

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
    1. verbosity_cfg["per_tenant_suffix"][tenant_id]  — static per-tenant override
    2. verbosity_cfg["default_suffix"] (non-empty)    — explicit global override
    3. _VERBOSITY_PRESETS[verbosity_cfg["level"]]     — bundled lite/full/ultra preset

    (A fourth, higher-priority rung used to probe an optional third-party package for a
    trained per-tenant verbosity model. The package WAS installed, so the import succeeded —
    it simply never defined the attribute, and `getattr(pkg, "verbosity_model", None)`
    returned None on every call, falling straight through to rung 1. It was removed with the
    dependency on 2026-09-07. Behaviour is unchanged.)
    """
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
    verbosity steering is off / unconfigured / this request is in the A3 holdout
    cohort. G05 folds this into its key so a terse-mode answer is never served to
    a request configured for verbose output (or vice-versa). "" keeps cache keys
    byte-identical to the pre-feature default.

    G05 (cache) runs before G11 (output-format) in the pipeline, so this must
    independently re-derive whether THIS request would land in the A3 holdout
    cohort — ``_assign_cohort`` is a pure function of stable request/tenant state,
    so it returns the same verdict here as it will later inside G11.process_request.
    Skipping this check would let a holdout (unshaped) request and a treatment
    (suffix-shaped) request collide on the same cache key despite receiving
    different prompts — corrupting both the cache and the A3 measurement itself.
    """
    try:
        cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G11_output", {})
        vcfg = cfg.get("verbosity_steering", {})
        if not cfg.get("enabled", False) or not vcfg.get("enabled", False):
            return ""
        holdout_cfg = cfg.get("output_holdout", {})
        if holdout_cfg.get("enabled", False) and _assign_cohort(ctx, holdout_cfg) == "holdout":
            return ""  # this request won't get the suffix — tag it like steering is off
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
    # The pitch-test-plan harness scopes workflow_id per ablation arm+process as
    # "<id>::<arm>::<token>" (run_roi_pitch._scope_workflow_id) so G17 budgets don't
    # bleed across arms. That scoping would fragment THIS cohort — the same workflow
    # could land in 'holdout' in one arm and 'treatment' in another, corrupting the A3
    # treatment-vs-holdout comparison. Key on the ORIGINAL id by stripping the harness
    # suffix. Production workflow_ids never contain "::", so this is a no-op for real
    # traffic (backlog #10).
    if isinstance(stable, str) and "::" in stable:
        stable = stable.split("::", 1)[0]
    from middleware.g06_routing import stable_bucket
    bucket = stable_bucket(stable, _HOLDOUT_BUCKETS)
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
                # A streamed response never reaches `process_response` (main.py routes it
                # to the stream finaliser instead), so a cap applied here can never be
                # observed: no `finish_reason=length` is recorded, no history entry is
                # written and the sticky floor — the entire convergence mechanism of this
                # feature — never fires. Non-streaming answers keep feeding the same
                # bucket's p95, so the stream would be cut on EVERY request, permanently,
                # with nothing able to correct it. A cap we cannot observe is a cap we
                # cannot correct, so we do not apply one. (Better follow-up: a targeted
                # G11 recording call from the stream finaliser, which already reassembles
                # the content and reads usage off the final chunk — same one-call shape as
                # the G32 hoist. That belongs in main.py, which this change does not touch.)
                if auto_tighten and _is_streaming(ctx):
                    auto_tighten = False
                    logger.debug(
                        "[%s] G11: auto-tighten declined — streamed responses skip the "
                        "response pipeline, so a cap could never be recovered from",
                        ctx.request_id,
                    )
                historical_p95 = None
                sticky_floor = None
                floor_unreadable = False
                key = _history_key(ctx)   # None → catch-all bucket, see _history_key
                if auto_tighten and key is not None:
                    try:
                        redis = _get_redis()
                        tighten_q = cfg.get("tighten_quantile", 0.95)
                        backoff = cfg.get("truncation_backoff_multiplier", _DEFAULT_TRUNCATION_BACKOFF)
                        min_entries = cfg.get("history_min_entries", _DEFAULT_HISTORY_MIN_ENTRIES)
                        historical_p95 = await _get_historical_p95(
                            redis, key, quantile=tighten_q, min_entries=min_entries,
                            truncation_backoff=backoff,
                        )
                        sticky_floor = await _get_sticky_floor(redis, _floor_key(key))
                        if sticky_floor is _FLOOR_UNREADABLE:
                            floor_unreadable = True
                            sticky_floor = None
                    except Exception as exc:
                        logger.warning("G11 auto_tighten lookup failed: %s", exc)
                        floor_unreadable = True
                elif auto_tighten:
                    logger.debug(
                        "[%s] G11: auto-tighten declined — request carries no workflow_id/"
                        "template_id, so its only evidence would be other workloads' answers",
                        ctx.request_id,
                    )

                model_limit = _get_model_max_tokens(ctx.params.get("model"), cfg)
                if floor_unreadable:
                    # We have a percentile but cannot see whether this bucket has already
                    # been cut at it. Fail CLOSED: no cap.
                    historical_p95 = None
                    logger.warning(
                        "[%s] G11: max_tokens left unset — the sticky floor for this "
                        "bucket could not be read, and the percentile alone has already "
                        "been proven to truncate it", ctx.request_id,
                    )
                if historical_p95:
                    tighten_mult = cfg.get("tighten_multiplier", _DEFAULT_TIGHTEN_MULTIPLIER)
                    cap = int(historical_p95 * tighten_mult)
                    # A truncation this bucket already suffered is harder evidence than any
                    # percentile over it: the floor may raise the cap, never lower it.
                    if sticky_floor:
                        cap = max(cap, sticky_floor)
                    cap = min(cap, model_limit)
                    if cap < _MIN_SANE_CAP:
                        # Declining beats clamping up to the floor: a cap that only the floor
                        # justifies is the floor deciding the answer length, not the evidence.
                        logger.debug(
                            "[%s] G11: max_tokens left unset (estimate %d below the %d-token "
                            "sanity floor — degenerate evidence, not a short workload)",
                            ctx.request_id, cap, _MIN_SANE_CAP,
                        )
                    else:
                        ctx.params["max_tokens"] = cap
                        ctx.params["_g11_max_tokens_set"] = True
                        floor_note = f", floor={sticky_floor}" if sticky_floor else ""
                        notes.append(
                            f"max_tokens tightened to {cap} "
                            f"(completion p95×{tighten_mult}, cap={model_limit}{floor_note})"
                        )
                        changed = True
                else:
                    # No completion-size evidence for this workload. Output length is
                    # not derivable from input length (a one-line question can need a
                    # proof-length answer), and a guessed cap truncates exactly those
                    # answers — the truncated completions would then re-teach the low
                    # cap. No evidence → no cap, unless the operator configured an
                    # explicit static fallback.
                    fallback_cap = cfg.get("fallback_max_tokens")
                    if isinstance(fallback_cap, int) and not isinstance(fallback_cap, bool) \
                            and fallback_cap > 0:
                        ctx.params["max_tokens"] = min(fallback_cap, model_limit)
                        ctx.params["_g11_max_tokens_set"] = True
                        # Marked so `process_response` can WARN by name when THIS cap cut
                        # an answer. The fallback is the one cap with no escalation path:
                        # it is static, it applies to catch-all traffic too, and a
                        # truncation it causes raises no floor. Deliberately NOT gated on
                        # `max_tokens_auto_tighten` — it is an operator's explicit spend
                        # ceiling, and making a hard ceiling require the learning switch
                        # would force the riskier feature on to get the simpler one. It is
                        # instead made OBSERVABLE, and the portal no longer offers a value
                        # in the degenerate range.
                        ctx.params["_g11_fallback_cap"] = True
                        notes.append(
                            f"max_tokens set to {ctx.params['max_tokens']} "
                            f"(configured fallback_max_tokens, cap={model_limit})"
                        )
                        changed = True
                    else:
                        logger.debug(
                            "[%s] G11: max_tokens left unset (no completion-size history)",
                            ctx.request_id,
                        )

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
        #    or an explicit per-tenant/default suffix).
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
        Record completed answers' (max_tokens, completion_tokens) pairs to Redis
        ZSET for future tightening; G11-capped truncations enter with a `truncated`
        marker so they escalate the estimate rather than anchor it.
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

        max_tokens = ctx.params.get("max_tokens")
        choices = response.get("choices") or []
        finish_reason = (choices[0] or {}).get("finish_reason") if choices else None
        truncated_by_us = (
            finish_reason == "length"
            and bool(max_tokens)
            and bool(ctx.params.get("_g11_max_tokens_set"))
        )

        # ── An answer WE cut must never enter the response cache ──────────────
        # Runs before every enable flag below, because the fallback cap can truncate with
        # the feedback loop off. G05 refuses only EMPTY answers, and its key carries
        # neither `max_tokens` nor the workflow bucket — so a 300-token cut answer stored
        # under prompt P is served, for the L2 TTL (24h default), to a later request with
        # the same prompt and NO workflow_id, which by design is never capped. That
        # request's user sees a mid-sentence answer the provider was never asked for, and
        # G11 cannot correct it: the cache short-circuit returns without running the
        # response pipeline, so no truncation is recorded and no floor is raised. The
        # capped and never-capped populations share one cache namespace; this is the
        # divergence the bucket fix created, and `verbosity_cache_tag` in g05_cache.py
        # exists for exactly this class of bug. Ordering is sound: pipeline.py runs this
        # stage before `G05-store-response`, which honours `ctx.no_cache`. Precedent: G32.
        if truncated_by_us:
            ctx.no_cache = True
            if ctx.params.get("_g11_fallback_cap"):
                # The one cap with no escalation path: static, applied to catch-all
                # traffic too, and its truncations raise no floor — so the same request is
                # cut identically forever. Nothing else would ever say so.
                logger.warning(
                    "[%s] G11 fallback_max_tokens=%s cut this answer off mid-stream, and "
                    "a fallback cap never learns: every matching request will be cut the "
                    "same way until the value is raised",
                    ctx.request_id, max_tokens,
                )

        if not cfg.get("max_tokens_feedback_loop", False):
            return ctx, response

        if max_tokens and completion_tokens:
            utilization = completion_tokens / max_tokens
            logger.debug(
                "[%s] G11 max_tokens feedback: %d/%d used (%.1f%% utilization)",
                ctx.request_id,
                completion_tokens,
                max_tokens,
                utilization * 100,
            )
            # Retain ephemeral feedback for backward compatibility
            ctx.params.setdefault("_token_opt_feedback", {})["max_tokens_utilization"] = utilization

        # Evidence recording: only COMPLETED answers teach future caps. An answer
        # cut off by a G11-set cap is recorded with the `truncated` marker (read
        # back as completion × truncation_backoff_multiplier) so the estimate
        # climbs out of a bad cap instead of re-learning it. Anything else that
        # is not `stop` (caller-capped truncation, tool_calls, content_filter,
        # missing finish_reason) is no evidence at all.
        # Gated on the AUTO-TIGHTEN switch, not on `max_tokens_feedback_loop`. With the
        # loop off — the shipped default — nothing ever reads this history, and the ids
        # are caller-supplied: a client stamping a unique workflow id per request (a
        # request id, a session id) would make one ZSET + EXPIRE + ZREMRANGEBYRANK per
        # request on shared Memorystore, each held for 7 days, none of them read. That is
        # pure cost to the customer for no benefit. Pre-warming was considered and
        # rejected: with the switch off there is nothing to warm FOR, and when an operator
        # does opt in, an empty bucket makes G11 decline to cap until 5 completed answers
        # exist — declining is the quality-safe cold start, not a penalty. Charter 2.1.
        if completion_tokens and cfg.get("max_tokens_auto_tighten", False):
            key = _history_key(ctx)   # None → catch-all; nothing reads it, so nothing writes it
            if key is not None and (finish_reason == "stop" or truncated_by_us):
                try:
                    redis = _get_redis()
                    ttl_days = cfg.get("max_tokens_history_ttl_days", 7)
                    ttl_seconds = ttl_days * 86400
                    await _record_max_tokens_pair(
                        redis, key, int(max_tokens or 0), completion_tokens,
                        ttl_seconds, truncated=truncated_by_us,
                        max_entries=cfg.get("history_max_entries", _DEFAULT_HISTORY_MAX_ENTRIES),
                    )
                    if truncated_by_us:
                        # We cut this answer. Raise the bucket's floor so no later percentile
                        # can put the cap back where it was — the escalated history entry
                        # alone is one sample among many and ages out (DS1: the same request
                        # was cut, escalated, and cut again three requests later).
                        backoff = cfg.get(
                            "truncation_backoff_multiplier", _DEFAULT_TRUNCATION_BACKOFF
                        )
                        raised = int(int(max_tokens) * max(1.0, backoff))
                        await _raise_sticky_floor(
                            redis, _floor_key(key), raised, ttl_seconds,
                        )
                        logger.info(
                            "[%s] G11 cut an answer at max_tokens=%s — raising the cap floor "
                            "for %s to %d", ctx.request_id, max_tokens, key, raised,
                        )
                except Exception as exc:
                    logger.warning("[%s] G11 failed to record max_tokens pair: %s", ctx.request_id, exc)

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
