"""
G05 · Response & Step Caching
Stage: At the Gate
Saving: 40–70% API calls eliminated
Technique:
  L1 Exact-match: hash(normalised_prompt) → Redis lookup (sub-millisecond)
  L2 Semantic:    embed query → pgvector cosine similarity (threshold from config)
  Temporal:       Activity replay for durable step cache execution
  Auto-TTL:       TTLs follow the tenant's hit rate over the last one to two hours
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from middleware import langfuse_tracing, resolve_group_config

logger = logging.getLogger(__name__)
GROUP = "G05"

# Import CACHE_HITS counter from g18_observability (lazy import to avoid cycles)
def _get_cache_hits_counter():
    from middleware.g18_observability import CACHE_HITS
    return CACHE_HITS

# Configurable embedding model for L2 semantic cache
_DEFAULT_L2_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# L3 semantic cache — REMOVED 2026-09-07.
# L3 was a third-party semantic cache that never executed: its backing object was
# hard-wired to None because the installed library's API did not match what the
# _l3_* helpers assumed, so every L3 call site short-circuited. The library itself
# has now been dropped from the image, which makes the "pending a scoped rewrite"
# note un-keepable — there is no longer a class to rewrite. The helpers, both call
# sites and the `l3_enabled` / `l3_similarity_threshold` config keys went with it.
# L1 (exact hash) and L2 (semantic) are unchanged and remain the shipped cache.
# Note `_semantic_cache_disabled` below is NOT part of L3: it gates L2 and stays.

def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


_TTL_STATS_WINDOW_S = 3600   # hit-rate stats are counted per window of this many seconds
_clock = time.time           # wall clock: every replica must agree on the window


def _auto_ttl_enabled(cfg: Dict) -> bool:
    return bool(cfg.get("auto_ttl_enabled", True))


class AutoTTLManager:
    """Adaptive TTLs from a tenant's recent cache hit rate.

    Hits and misses are counted per level in hourly windows, and a TTL is chosen from the
    current and previous window together. Above an 80% hit rate the TTL grows by a quarter;
    below 20% it shrinks by a quarter; with fewer than 10 lookups it stays as configured.
    The result is kept within auto_ttl_min_multiplier and auto_ttl_max_multiplier of the
    configured TTL (defaults 0.25 and 2.0), and is never under one second.
    """

    def __init__(self, redis_client, prefix: str = ""):
        self.redis = redis_client
        # I4: tenant-scope the stat hashes so one tenant's hit-rate never skews
        # another tenant's adaptive TTL. prefix = ctx.redis_prefix ("" = default).
        self._prefix = prefix

    def _stats_key(self, window: int) -> str:
        return f"{self._prefix}tok_opt:cache:ttl_stats:{window}"

    async def _count(self, cache_level: str, outcome: str) -> None:
        key = self._stats_key(int(_clock() // _TTL_STATS_WINDOW_S))
        try:
            await self.redis.hincrby(key, f"{cache_level}:{outcome}", 1)
            await self.redis.expire(key, 2 * _TTL_STATS_WINDOW_S)
        except Exception as exc:
            logger.debug("AutoTTL %s recording failed: %s", outcome, exc)

    async def record_hit(self, cache_level: str) -> None:
        await self._count(cache_level, "hit")

    async def record_miss(self, cache_level: str) -> None:
        await self._count(cache_level, "miss")

    async def get_recommended_ttl(self, base_ttl: int, cache_level: str, cfg: Dict) -> int:
        """The TTL for a new entry at ``cache_level``, from the recent hit rate."""
        try:
            hits = misses = 0
            window = int(_clock() // _TTL_STATS_WINDOW_S)
            for counted in (window, window - 1):
                hit, miss = await self.redis.hmget(
                    self._stats_key(counted), [f"{cache_level}:hit", f"{cache_level}:miss"])
                hits += int(hit or 0)
                misses += int(miss or 0)
            total = hits + misses
            if total < 10:  # Not enough data
                return base_ttl
            hit_rate = hits / total
            if hit_rate > 0.80:
                scale = 1.25
            elif hit_rate < 0.20:
                scale = 0.75
            else:
                return base_ttl
            low = float(cfg.get("auto_ttl_min_multiplier", 0.25))
            high = float(cfg.get("auto_ttl_max_multiplier", 2.0))
            new_ttl = max(1, int(base_ttl * min(max(scale, low), high)))
            logger.debug("AutoTTL: %s hit rate %.2f, TTL %d -> %d",
                         cache_level, hit_rate, base_ttl, new_ttl)
            return new_ttl
        except Exception as exc:
            logger.debug("AutoTTL calculation failed: %s", exc)
            return base_ttl


# Message fields the model reads besides role and content. An assistant turn that only
# calls tools has content None, so without its tool_calls two agent transcripts that
# called the same tool with different arguments normalised to the same L1 key.
_TURN_FIELDS = ("tool_calls", "function_call", "tool_call_id", "name")


def _normalise(messages: list) -> str:
    """Normalise messages for the L1 exact-match key: strip whitespace, lowercase.

    Used for L1 only — exact matching needs the full request (system + every turn),
    including each turn's tool calls (arguments verbatim), the call a tool result
    answers, and a message ``name``. A turn without those keeps its old form.
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            content = re.sub(r"\s+", " ", content).strip().lower()
        part = f"{role}:{content}"
        fields = {k: msg[k] for k in _TURN_FIELDS if msg.get(k)}
        if fields:
            part += " " + json.dumps(fields, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str)
        parts.append(part)
    return "|".join(parts)


def _semantic_query_text(messages: list) -> str:
    """Text to embed for L2 semantic matching: the user turns only.

    The system prompt is fixed infrastructure (role, policies, formatting). Embedding
    it lets a prompt longer than the embedding window (~512 tokens for bge-small)
    dominate and truncate the vector, collapsing distinct user queries onto the same
    point — which returns a cached answer for a *different* question. Matching on the
    user turns keys the semantic cache on what actually carries the query intent.

    Returns "" (L2 is skipped; L1 still applies) when a user turn carries anything but
    text, such as an image, audio or a file: the embedding would see only the words,
    so "describe this image" would match across different images. Also "" when there
    is no user text at all. It never falls back to the whole transcript.
    """
    users = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            if not all(isinstance(p, dict) and p.get("type") == "text"
                       and isinstance(p.get("text"), str) for p in content):
                return ""
            content = "\n".join(p["text"] for p in content)
        if not isinstance(content, str):
            return ""
        users.append(re.sub(r"\s+", " ", content).strip().lower())
    return "\n".join(u for u in users if u)


def _request_messages(ctx) -> list:
    """The messages as the caller sent them (``ctx.original_messages``).

    Stages after the lookup rewrite ``ctx.messages``: compression, retrieval, memory,
    image handling, history compaction. The L2 store must decide and embed on the same
    request its lookup saw; otherwise a compacted multi-turn request, or an image a later
    stage replaced, is stored as a single-turn text question."""
    msgs = getattr(ctx, "original_messages", None)
    if isinstance(msgs, list) and msgs:
        return msgs
    return getattr(ctx, "messages", None) or []


# Embedding-window guard (M2). bge-small-en-v1.5 truncates input at ~512 tokens;
# sentence-transformers does this silently, so two distinct long queries that share
# a long prefix embed to (nearly) the same vector and can collide in the L2 semantic
# cache — returning a cached answer for a *different* question. Such over-window
# queries skip the semantic layer entirely (L1 exact-match still applies). ~4 chars
# ≈ 1 token, so the default 2000 chars sits safely under the 512-token window.
_DEFAULT_L2_MAX_EMBED_CHARS = 2000


def _embed_input_truncates(query_text: str, cfg: Dict) -> bool:
    """True when query_text would exceed the embedding window and be silently
    truncated — making its embedding unsafe for similarity matching."""
    max_chars = int(cfg.get("l2_max_embed_chars", _DEFAULT_L2_MAX_EMBED_CHARS))
    return max_chars > 0 and len(query_text) > max_chars


def _is_multiturn_continuation(ctx) -> bool:
    """True when the request continues an existing conversation — its history
    already contains a prior ``assistant`` or ``tool`` turn.

    For such a request the correct response is a function of the accumulated
    conversation state (what was already said, which tools ran and what they
    returned), not just the embeddable user text. A *fuzzy* L2 semantic match
    between two different continuations can therefore return a response generated
    for a DIFFERENT state — e.g. one turn's tool plan served for another turn's
    question. (Observed: a follow-up asking for a user profile matched the prior
    "fetch logs" turn at cosine 0.99 because the shared, long first turn
    dominates the embedding.) L1 exact-match is unaffected — only the fuzzy
    semantic layers are skipped.

    Read from the request as sent (:func:`_request_messages`), so a later stage that
    compacts the history cannot make the store treat the request as single-turn.
    """
    msgs = _request_messages(ctx)
    return any(
        isinstance(m, dict) and m.get("role") in ("assistant", "tool")
        for m in msgs
    )


def _semantic_cache_disabled(ctx) -> bool:
    """Whether L2 *semantic* caching must be skipped for this request.

    L1 exact-match caching is unaffected. Two triggers:
      * **Explicit opt-out** via ``x_cache_semantic=false`` — exact-only caching
        for callers that want it (and how the benchmark keeps per-group savings
        attributable / avoids over-matching when a long, >embedding-window system
        prompt dominates the vector and collapses distinct queries together).
      * **Stateful multi-turn continuations** (see ``_is_multiturn_continuation``)
        — a fuzzy semantic match across continuations can return another turn's
        answer. Gated by ``G5_cache.semantic_skip_multiturn`` (default on).
    """
    val = getattr(ctx, "params", {}).get("x_cache_semantic", True)
    if str(val).lower() in ("false", "0", "no"):
        return True

    cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G5_cache", {})
    if cfg.get("semantic_skip_multiturn", True) and _is_multiturn_continuation(ctx):
        return True
    return False


def _cache_key(normalised: str, prefix: str = "") -> str:
    # prefix = ctx.redis_prefix (e.g. "t:acme:" for tenant "acme", "" for default)
    return f"{prefix}tok_opt:l1:" + hashlib.sha256(normalised.encode()).hexdigest()


# ─── Cache scope (tenant | tenant+model) ──────────────────────────────────────
# Default "tenant": cache is keyed by tenant + request content only, so an answer
# is reusable across providers within a tenant (max savings). Opt-in "tenant+model"
# additionally keys on the *requested* model, so a tenant that deliberately uses
# several providers never gets one model's cached answer served to another.
# Resolved per request; default keeps keys byte-identical to the pre-feature
# behaviour, so enabling/leaving it never invalidates existing caches.

# Exact-match allowlist for cache_scope — spelling → canonical scope. Substring
# matching is deliberately NOT used: a typo must fail closed to "tenant" (with a
# warning), never silently activate a scope component. Legacy spellings kept from
# the original tenant+model rollout.
_CACHE_SCOPE_CANONICAL: Dict[str, str] = {
    "tenant": "tenant",
    "tenant+model": "tenant+model",
    "tenant_model": "tenant+model",          # legacy spelling
    "model": "tenant+model",                 # legacy spelling
    "tenant+system": "tenant+system",
    "tenant_system": "tenant+system",
    "system": "tenant+system",
    "tenant+model+system": "tenant+model+system",
    "tenant+system+model": "tenant+model+system",
    "tenant_model_system": "tenant+model+system",
    "model+system": "tenant+model+system",
    "system+model": "tenant+model+system",
}
_warned_cache_scopes: set = set()


def _resolve_cache_scope(ctx) -> str:
    """Return the normalised cache scope — "tenant" (default), optionally with
    "+model" and/or "+system" components (canonical order, regardless of how the
    config spells them).

    Per-tenant override (``tenants.<id>.groups.G5_cache.cache_scope``) wins over
    the global ``G5_cache.cache_scope``. Recognised components:
      * ``tenant``               — default; tenant-only isolation (byte-identical keys)
      * ``tenant+model``         — also key on the REQUESTED model
      * ``tenant+system``        — also key on a fingerprint of the system prompt
      * ``tenant+model+system``  — both
    Anything unrecognised fails CLOSED to "tenant" (the safe, pre-feature default)
    and logs a one-time warning naming the valid values — see _CACHE_SCOPE_CANONICAL.
    """
    # resolve_group_config type-checks every level of the operator's `tenants:` overlay: a
    # node that is not a mapping (`tenants:` left empty, `acme: off`) used to raise here on
    # every lookup and store, for every tenant.
    raw = str(resolve_group_config(ctx, "G5_cache").get("cache_scope", "tenant")).lower().strip()
    canonical = _CACHE_SCOPE_CANONICAL.get(raw)
    if canonical is None:
        # Fail CLOSED to "tenant" (safe, pre-feature keys) but say so once — a typo
        # like "tenant+sytem" silently losing system isolation, or a stray value
        # accidentally activating a scope, must be visible, not guessed at.
        if raw not in _warned_cache_scopes:
            _warned_cache_scopes.add(raw)
            logger.warning(
                "G05 unrecognised cache_scope %r — using 'tenant'. Valid: %s",
                raw, sorted(set(_CACHE_SCOPE_CANONICAL.values())),
            )
        return "tenant"
    return canonical


def _model_scope_tag(ctx) -> str:
    """Model component of the cache key when the scope includes "+model", else "".

    Uses the *requested* model (``ctx.model`` / ``params["model"]``): G05 runs
    before G06 routing, and the caller chose this model deliberately, so the
    requested model is the stable, correct key (identical at lookup and store).
    """
    if "+model" not in _resolve_cache_scope(ctx):
        return ""
    return getattr(ctx, "model", None) or ctx.params.get("model") or ""


def _system_scope_tag(ctx) -> str:
    """System-prompt fingerprint folded into the cache key when the scope includes
    "+system", else "" (the default → keys stay byte-identical to pre-feature).

    WHY THIS EXISTS: the L2 semantic key embeds **user turns only** (see
    :func:`_semantic_query_text` — the system prompt is deliberately excluded so a
    long prompt can't dominate and truncate the ~512-token embedding window). That
    makes the key blind to the system prompt, so two requests with the SAME user
    question but DIFFERENT system prompts collide — a restrictive prompt ("only
    answer Northwind questions") can be served an answer generated under a laxer
    one, silently bypassing the scope/persona/format constraint it encodes.

    Observed 2026-07-20 (pitch-test-plan DS8): the baseline correctly declined
    off-topic geography questions, while the optimised arm returned cached "Rome."
    / "The capital of Egypt is Cairo." at 0.95-0.96 similarity with
    final_tokens_sent=0. Isolation is per-tenant either way (``tenant_id`` is in the
    L2 WHERE clause), so this is a WITHIN-tenant collision — it bites a tenant
    running several personas/apps/agents on one key.

    Fingerprinting rather than embedding keeps the vector keyed on query intent
    (no truncation regression) while making the KEY system-prompt-aware. Mirrors
    :func:`_verbosity_scope_tag`, which solves the same class of bug for G11 terse
    vs verbose output. Computed once per request and stashed on ``ctx.params`` so
    every path (L1 lookup/store, L2 lookup/store) reads the SAME value — L1 and L2
    can never disagree on scope within one request.
    """
    params = getattr(ctx, "params", None)
    if isinstance(params, dict) and "_g05_system_tag" in params:
        return params["_g05_system_tag"]
    tag = ""
    if "+system" in _resolve_cache_scope(ctx):
        parts: List[str] = []
        for msg in (getattr(ctx, "messages", None) or []):
            if not isinstance(msg, dict) or msg.get("role") != "system":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):      # OpenAI multimodal list-of-parts
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                        parts.append(p["text"])
        # Normalise whitespace so cosmetic reformatting doesn't split the cache.
        joined = re.sub(r"\s+", " ", "\n".join(parts)).strip()
        # Hash even when empty-but-scoped so "no system prompt" is its OWN bucket and
        # can't be served an answer produced under one.
        tag = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
    if isinstance(params, dict):
        params["_g05_system_tag"] = tag
    return tag


def _verbosity_scope_tag(ctx) -> str:
    """G11 verbosity-steering tag folded into the cache key so a terse-mode answer
    is never served to a request configured for verbose output. "" (the default,
    steering off) keeps keys byte-identical to the pre-feature behaviour. Lazy import
    of G11 avoids a module-load import cycle.

    Computed once per request and stashed on ``ctx.params`` — every G05 cache path
    (L1 lookup/store, L2 lookup/store) reads the SAME cached value, so L1 and L2
    can never disagree on scope within one request."""
    params = getattr(ctx, "params", None)
    if isinstance(params, dict) and "_g05_verbosity_tag" in params:
        return params["_g05_verbosity_tag"]
    try:
        from middleware.g11_output_format import verbosity_cache_tag
        tag = verbosity_cache_tag(ctx)
    except Exception:
        tag = ""
    if isinstance(params, dict):
        params["_g05_verbosity_tag"] = tag
    return tag


# ─── Request parameters in the cache key ──────────────────────────────────────
# Parameters that change the answer's content or shape. Two requests that differ in one
# of them must not share a cache entry: an answer made for one response_format, tool
# list, choice count or token budget is the wrong answer for another. The provider names
# are the answer-affecting part of protocols.base.OPENAI_CHAT_PARAMS (the Anthropic and
# Gemini adapters map onto the same names). The TokenLean names change the prompt or the
# output format in a stage that runs after this lookup.
_ANSWER_PARAMS = frozenset({
    "audio", "frequency_penalty", "function_call", "functions", "logit_bias", "logprobs",
    "max_completion_tokens", "max_tokens", "modalities", "n", "parallel_tool_calls",
    "presence_penalty", "reasoning_effort", "response_format", "seed", "stop",
    "temperature", "thinking", "thinking_config", "tool_choice", "tools", "top_k",
    "top_logprobs", "top_p", "verbosity", "web_search_options",
    "json_schema", "x_json_output",                                      # G11 output format
    "rag_query", "x_rag_collection", "x_rag_top_k", "x_jit_retrieval",   # G07 retrieval
    "session_id", "x_session_id",                                        # G10 session context
})
# Admitted request fields that do not change the answer. A unit test fails when a field
# admitted at ingress, or an `x_` field the proxy reads, is in neither set, so a new
# parameter is classified before it can share entries across different answers.
_ANSWER_NEUTRAL_PARAMS = frozenset({
    # Delivery, provider-side storage and caching, abuse-monitoring ids: same answer.
    "stream", "stream_options", "store", "service_tier", "prediction", "prompt_cache_key",
    "prompt_cache_retention", "user", "safety_identifier", "metadata", "batch_topic",
    # Routing hints: the default cache_scope shares answers across models on purpose
    # (cache_scope "tenant+model" keys on the requested model instead).
    "complexity", "complexity_tier", "x_complexity", "x_complexity_tier", "x_route_to",
    # Labels and budget buckets (G11, G17, G18, G24, traces).
    "user_id", "x_user_id", "workflow_id", "x_workflow_id", "template_id", "x_template_id",
    "dataset_id", "x_dataset", "x_team", "x_feature", "rag_collection", "token_opt_state",
    "x_confidence_score",
    # This cache's own switches, and the step cache (keyed separately).
    "x_cache_semantic", "x_no_cache", "x_step_name", "x_step_inputs_hash",
    "x_template_version",
    # Prompt layout and compression, which preserve the answer by design, and the prompt
    # echo (attached after the cache).
    "x_prefix_profile", "x_compress_user", "x_echo_prompt",
})


def _params_scope_tag(ctx) -> str:
    """Fingerprint of the request's :data:`_ANSWER_PARAMS` values; "" when it sets none.

    Folded into the L1 key and the L2 scope. Computed once, at lookup, and memoised on
    ``ctx.params``: later stages rewrite some of these fields (G08 and G16 filter
    ``tools``, G11 sets ``max_tokens``, G25 sets ``reasoning_effort``), and the store must
    file the answer under the request the caller sent, which is what a later identical
    request looks up. A field sent as null counts as absent."""
    params = getattr(ctx, "params", None)
    if not isinstance(params, dict):
        return ""
    if "_g05_params_tag" in params:
        return params["_g05_params_tag"]
    chosen = {k: params[k] for k in _ANSWER_PARAMS if params.get(k) is not None}
    tag = ""
    if chosen:
        blob = json.dumps(chosen, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
        tag = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    params["_g05_params_tag"] = tag
    return tag


def _apply_model_scope(text: str, ctx) -> str:
    """Fold the model, verbosity, system and parameter tags into a cache-key source
    string. No-op when all are empty → keys stay byte-identical to pre-feature. Used by
    BOTH L1 lookup and store so the key matches."""
    tag = _model_scope_tag(ctx)
    if tag:
        text = f"model={tag}\n{text}"
    vtag = _verbosity_scope_tag(ctx)
    if vtag:
        text = f"verbosity={vtag}\n{text}"
    stag = _system_scope_tag(ctx)
    if stag:
        text = f"system={stag}\n{text}"
    ptag = _params_scope_tag(ctx)
    if ptag:
        text = f"params={ptag}\n{text}"
    return text


def _scope_value(ctx) -> str:
    """Combined model+verbosity+system+parameter scope for L2 (colon-joined; "" when
    all are unscoped → byte-identical to pre-feature). Shared by L2's lookup WHERE filter
    AND store INSERT so a row's stored scope always matches what a later lookup searches
    for — this is what makes the isolation actually enforced on L2's READ path
    (unlike folding a tag into query_hash alone, which only dedupes the INSERT and is
    never consulted by SELECT)."""
    ptag = _params_scope_tag(ctx)
    return ":".join(
        p for p in (_model_scope_tag(ctx), _verbosity_scope_tag(ctx), _system_scope_tag(ctx),
                    f"params={ptag}" if ptag else "") if p
    )


def _step_cache_key(step_name: str, inputs_hash: str, template_version: str, prefix: str = "") -> str:
    payload = f"{step_name}|{inputs_hash}|{template_version}"
    return f"{prefix}tok_opt:step:" + hashlib.sha256(payload.encode()).hexdigest()


def _hash_args(args: tuple, kwargs: dict) -> str:
    """Hash function arguments for Temporal replay cache key."""
    payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def is_empty_answer(response: Dict[str, Any]) -> bool:
    """True when the response carries no content and no tool calls.

    Public on purpose (backlog #59): ``main._refuse_empty_cache_hit`` imports this
    function so the cache write-gate and read-gate share one derivation and cannot
    drift apart. A leading underscore signalled "internal" while a call site outside
    this module already depended on it; renaming it here now reads as changing a
    documented API rather than silently breaking an unstated one. The coupling is
    also pinned by a test (``test_is_empty_answer_import_contract`` in
    ``test_empty_completion_guard.py``) so a future rename fails loudly at the
    import, not at the next empty response served from a stale cache entry.

    Deliberately NOT keyed on ``finish_reason``: a truncated-to-nothing answer
    (``length``), a model that returned an empty string, and a malformed choice are
    all equally useless to serve from cache later. A guardrail refusal is excluded --
    it carries its refusal text as content, so it never reaches the empty test.

    Backlog #64, fixed 2026-09-17: a body with NO ``choices`` at all used to return
    False here — "not empty" — which is backwards. Both call sites (this module's own
    ``store_response`` write-gate, and ``main._refuse_empty_cache_hit``'s read-gate,
    imported under an alias) only ever see this function AFTER a non-streaming call
    completed; a streaming response never reaches either path (it returns via
    ``_stream_response`` before the response pipeline runs at all), so the old
    "malformed/streamed shape, not our case" reasoning described a case this function
    is never actually asked about. A response with zero choices reaching here is a
    malformed provider body — exactly the shape the whole guard exists to keep out of
    the cache, and exactly what would otherwise be replayed to every look-alike
    question for a full TTL (L1 1h, L2 24h) with the provider never called again.

    Backlog #79, fixed the same day: every shape below is now type-checked before use.
    The old code indexed ``choices[0]`` and called ``.get()`` on it unconditionally, so
    a ``choices`` value that was present but not a list (or whose first entry was not a
    dict) raised ``AttributeError`` with nothing between this function and its two call
    sites to catch it — turning a cache-safety guard into a 500 on the write path, and
    into a 500 on every future read of an already-cached entry with that shape on the
    read path. Anything this function cannot make sense of is treated as empty: a
    provider body too malformed to parse is too malformed to trust, on either side.
    """
    if not isinstance(response, dict):
        return True
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    item = choices[0]
    message = item.get("message") if isinstance(item, dict) else None
    if not isinstance(message, dict):
        return True
    if message.get("tool_calls") or message.get("function_call"):
        return False
    content = message.get("content")
    if isinstance(content, list):
        return not content
    return content is None or not str(content).strip()


class G05Cache:
    """G05 cache with L1 (Redis exact), L2 (pgvector semantic), and auto-TTL."""
    
    def __init__(self):
        # One AutoTTLManager per tenant prefix (I4) — stats must not be shared
        # across tenants, so cache them keyed by redis_prefix.
        self._ttl_managers: Dict[str, AutoTTLManager] = {}

    def _get_ttl_manager(self, prefix: str = "") -> Optional[AutoTTLManager]:
        """Lazy init a tenant-scoped TTL manager (keyed by redis_prefix)."""
        mgr = self._ttl_managers.get(prefix)
        if mgr is None:
            try:
                redis = _get_redis()
                mgr = AutoTTLManager(redis, prefix=prefix)
                self._ttl_managers[prefix] = mgr
            except Exception:
                return None
        return mgr

    async def process_request(self, ctx: "RequestContext") -> "RequestContext":
        cfg = ctx.config.get("groups", {}).get("G5_cache", {})
        if not cfg.get("enabled", False):
            return ctx
        # G29 sets no_cache when it masks PII: the masked cache key is lossy, so reading
        # (or later writing) the shared cache could serve one caller's PII-derived answer
        # to another's look-alike masked query. Skip the cache entirely for such requests.
        #
        # x_no_cache is the per-request opt-out for an internal/self-call whose own caller
        # already applies its OWN caching layer (e.g. the commercial docs-chat endpoint caches
        # {answer, citations} keyed by docs_version in api/docs_cache.py) — for such a caller,
        # G05's semantic (L2) match on the INTERNAL prompt text is actively wrong: two
        # differently-grounded internal calls (different retrieved chunks, hence different
        # correct answers) can still land within the L2 similarity threshold of each other
        # purely because their SYSTEM instructions are similar, silently serving one grounded
        # answer in place of another regardless of what was actually retrieved this time.
        if str(ctx.params.get("x_no_cache", "")).lower() in ("true", "1", "yes"):
            ctx.no_cache = True
        if getattr(ctx, "no_cache", False):
            return ctx

        tokens_before = ctx.current_token_count
        normalised = _normalise(ctx.messages)
        ns = getattr(ctx, "redis_prefix", "")  # tenant namespace prefix
        # I4: tenant-scoped TTL stats, counted here and read when an answer is stored.
        ttl_manager = self._get_ttl_manager(ns) if _auto_ttl_enabled(cfg) else None

        # L1 — exact match
        try:
            # The key, and the scope tags it memoises for L2, are fixed here, before Redis
            # is touched: later stages rewrite tools, max_tokens and the messages, and the
            # store must file the answer under the request as the caller sent it.
            key = _cache_key(_apply_model_scope(normalised, ctx), prefix=ns)
            # Store key on context for reuse in store_response (Phase 2 fix)
            ctx.params["_g05_l1_cache_key"] = key
            redis = _get_redis()
            cached = await redis.get(key)
            if cached:
                ctx.cache_hit = True
                ctx.cache_level = "L1"
                ctx.cache_response = json.loads(cached)
                ctx.savings.cache_hit = True
                ctx.savings.cache_level = "L1"
                ctx.savings.final_tokens_sent = 0
                ctx.savings.proxy_optimised_tokens = 0   # B1: nothing sent to LLM
                ctx.savings.provider_prompt_tokens = 0
                ctx.savings.add_step(GROUP, "L1 exact-match cache hit", tokens_before, 0)
                langfuse_tracing.add_span(
                    ctx,
                    name="G05-cache",
                    span_input={"cache_key": key, "tokens_before": tokens_before},
                    output={"cache_level": "L1", "tokens_after": 0},
                    metadata={"cache_hit": True, "level": "L1"},
                )
                logger.debug("[%s] G05 L1 cache hit", ctx.request_id)
                if ttl_manager:
                    await ttl_manager.record_hit("L1")
                # Record Prometheus metric with tenant_id (lazy import to avoid cycles)
                tenant_id = getattr(ctx, "tenant_id", "default")
                _get_cache_hits_counter().labels(level="L1", tenant_id=tenant_id).inc()
                return ctx
            else:
                if ttl_manager:
                    await ttl_manager.record_miss("L1")
        except Exception as exc:
            logger.warning("G05 L1 Redis error: %s", exc)

        # L2 — semantic similarity (pgvector)
        l2_threshold = cfg.get("l2_similarity_threshold", 0.90)
        try:
            embedding_model = cfg.get("l2_embedding_model", _DEFAULT_L2_EMBEDDING_MODEL)
            cached_response, score = await _l2_lookup(ctx, l2_threshold, embedding_model)
            if cached_response:
                ctx.cache_hit = True
                ctx.cache_level = "L2"
                ctx.cache_response = cached_response
                ctx.savings.cache_hit = True
                ctx.savings.cache_level = "L2"
                ctx.savings.final_tokens_sent = 0
                ctx.savings.proxy_optimised_tokens = 0   # B1: nothing sent to LLM
                ctx.savings.provider_prompt_tokens = 0
                ctx.savings.add_step(
                    GROUP,
                    f"L2 semantic cache hit (score={score:.3f})",
                    tokens_before,
                    0,
                )
                langfuse_tracing.add_span(
                    ctx,
                    name="G05-cache",
                    span_input={"tokens_before": tokens_before, "l2_threshold": l2_threshold},
                    output={"cache_level": "L2", "tokens_after": 0},
                    metadata={"cache_hit": True, "level": "L2", "similarity_score": round(score, 3)},
                )
                logger.debug("[%s] G05 L2 cache hit score=%.3f", ctx.request_id, score)
                if ttl_manager:
                    await ttl_manager.record_hit("L2")
                # Record Prometheus metric with tenant_id (lazy import)
                tenant_id = getattr(ctx, "tenant_id", "default")
                _get_cache_hits_counter().labels(level="L2", tenant_id=tenant_id).inc()
                return ctx
            else:
                if ttl_manager:
                    await ttl_manager.record_miss("L2")
        except Exception as exc:
            logger.warning("G05 L2 pgvector error: %s", exc)

        # (L3 removed 2026-09-07 — see the note at the top of this module.)

        # Step-level idempotent cache (only when x_step_name is present)
        if cfg.get("step_cache_enabled", True):
            step_hit = await self._check_step_cache(ctx, cfg)
            if step_hit:
                return ctx

        return ctx

    async def _check_step_cache(self, ctx: "RequestContext", cfg: Dict) -> bool:
        step_name = ctx.params.get("x_step_name")
        if not step_name:
            return False
        inputs_hash = ctx.params.get("x_step_inputs_hash", "")
        template_version = ctx.params.get("x_template_version", "")
        key = _step_cache_key(step_name, inputs_hash, template_version, prefix=getattr(ctx, "redis_prefix", ""))
        try:
            redis = _get_redis()
            cached = await redis.get(key)
            if cached:
                ctx.cache_hit = True
                ctx.cache_level = "STEP"
                ctx.cache_response = json.loads(cached)
                ctx.savings.cache_hit = True
                ctx.savings.cache_level = "STEP"
                ctx.savings.final_tokens_sent = 0
                ctx.savings.proxy_optimised_tokens = 0   # B1: nothing sent to LLM
                ctx.savings.provider_prompt_tokens = 0
                ctx.savings.add_step(
                    GROUP,
                    f"Step cache hit: {step_name} (v{template_version})",
                    ctx.current_token_count,
                    0,
                )
                langfuse_tracing.add_span(
                    ctx,
                    name="G05-cache",
                    span_input={"step_name": step_name, "template_version": template_version},
                    output={"cache_level": "STEP", "tokens_after": 0},
                    metadata={"cache_hit": True, "level": "STEP"},
                )
                logger.debug("[%s] G05 step cache hit: %s", ctx.request_id, step_name)
                return True
        except Exception as exc:
            logger.warning("G05 step cache error: %s", exc)
        return False

    async def store_response(
        self, ctx: "RequestContext", response: Dict[str, Any]
    ) -> None:
        """Store the LLM response in L1, L2, and step caches after a successful call."""
        # F2-dispatched answers come from a tenant-registered downstream agent, not the
        # main LLM — caching them would let a later matching prompt be served straight
        # from cache (G05's lookup runs BEFORE F2 in the pipeline), bypassing intent
        # classification entirely and replaying a stale agent answer even after the
        # operator disables orchestration or edits/removes that agent.
        if ctx.cache_hit or ctx.bypassed or getattr(ctx, "no_cache", False) \
                or getattr(ctx, "agent_dispatched", False):
            return
        # An answer that carries NOTHING must never enter the cache. G18 sets
        # ctx.no_cache on detection one stage earlier, so this is belt AND braces:
        # ordering alone was exactly what failed for G32/G15, and the blast radius
        # here is a tenant being served an empty answer for every look-alike question
        # until the L2 TTL expires (default 24h), with the provider never called again
        # and so nothing left to notice it. Independent of any enable flag.
        if is_empty_answer(response):
            logger.warning(
                "[%s] G05 refusing to cache an EMPTY answer (finish_reason=%s) — "
                "caching it would replay the emptiness to every similar question",
                getattr(ctx, "request_id", "?"),
                ((response.get("choices") or [{}])[0] or {}).get("finish_reason") or "(none)",
            )
            return


        cfg = ctx.config.get("groups", {}).get("G5_cache", {})
        if not cfg.get("enabled", False):
            return

        # Get auto-TTL adjusted values (I4: tenant-scoped TTL stats)
        ttl_manager = (self._get_ttl_manager(getattr(ctx, "redis_prefix", ""))
                       if _auto_ttl_enabled(cfg) else None)
        l1_base_ttl = cfg.get("l1_ttl_seconds", 3600)
        l2_base_ttl = cfg.get("l2_ttl_seconds", 86400)
        l1_ttl = await ttl_manager.get_recommended_ttl(l1_base_ttl, "L1", cfg) if ttl_manager else l1_base_ttl
        l2_ttl = await ttl_manager.get_recommended_ttl(l2_base_ttl, "L2", cfg) if ttl_manager else l2_base_ttl

        # Use stored key from lookup if available (Phase 2 fix: ensures consistency)
        key = ctx.params.get("_g05_l1_cache_key")
        if not key:
            # Fallback: recompute (should not happen in normal flow after fix).
            # Mirror the lookup key exactly: tenant prefix + model scope, on the messages
            # as sent (later stages have rewritten ctx.messages by now).
            normalised = _normalise(_request_messages(ctx))
            key = _cache_key(_apply_model_scope(normalised, ctx), prefix=getattr(ctx, "redis_prefix", ""))
            logger.warning("G05 store_response called without prior lookup key; recomputed")

        # L1 store
        try:
            redis = _get_redis()
            await redis.set(key, json.dumps(response), ex=l1_ttl)
        except Exception as exc:
            logger.warning("G05 L1 store failed: %s", exc)

        # L2 store
        try:
            embedding_model = cfg.get("l2_embedding_model", _DEFAULT_L2_EMBEDDING_MODEL)
            await _l2_store(ctx, response, l2_ttl, embedding_model)
        except Exception as exc:
            logger.warning("G05 L2 store failed: %s", exc)

        # (L3 store removed 2026-09-07 — see the note at the top of this module.)

        # Step cache store
        if cfg.get("step_cache_enabled", True):
            await self._store_step_cache(ctx, response, cfg)

    async def temporal_activity_replay(
        self, ctx: "RequestContext", activity_func, *args, **kwargs
    ) -> Any:
        """Execute activity with Temporal-style replay-aware caching.
        
        This enables durable execution patterns where:
        1. First execution: runs activity_func, stores result in step cache
        2. Replay: returns cached result without re-execution
        
        Usage pattern for LangGraph/Temporal workflows:
            result = await cache.temporal_activity_replay(
                ctx, expensive_api_call, arg1, arg2
            )
        """
        cfg = ctx.config.get("groups", {}).get("G5_cache", {})
        if not cfg.get("enabled", False) or not cfg.get("step_cache_enabled", True):
            # No caching - just execute
            return await activity_func(*args, **kwargs)
        
        step_name = ctx.params.get("x_step_name", "temporal_activity")
        inputs_hash = _hash_args(args, kwargs)
        template_version = ctx.params.get("x_template_version", "v1")
        
        # Check cache first (replay path)
        key = _step_cache_key(step_name, inputs_hash, template_version, prefix=getattr(ctx, "redis_prefix", ""))
        try:
            redis = _get_redis()
            cached = await redis.get(key)
            if cached:
                logger.debug("[%s] Temporal replay hit: %s", ctx.request_id, step_name)
                return json.loads(cached)
        except Exception as exc:
            logger.debug("Temporal replay check failed: %s", exc)
        
        # Execute and store (execution path)
        result = await activity_func(*args, **kwargs)
        
        # Store for future replays
        ttl = cfg.get("step_cache_ttl_seconds", 86400)
        try:
            await redis.set(key, json.dumps(result), ex=ttl)
            logger.debug("[%s] Temporal replay stored: %s", ctx.request_id, step_name)
        except Exception as exc:
            logger.warning("Temporal replay store failed: %s", exc)
        
        return result

    async def warm_cache(
        self,
        patterns: List[str],
        redis_client=None,
        prefix: str = "",
        embedding_model: str = _DEFAULT_L2_EMBEDDING_MODEL,
        ttl: int = 86400,
    ) -> int:
        """Pre-compute and store L2 embeddings for known query patterns.

        Each pattern's embedding is stored under:
            ``{prefix}tok_opt:l2:warm:{sha256(pattern)[:16]}``

        Returns the count of patterns successfully warmed.
        """
        count = 0
        redis = redis_client
        if redis is None:
            try:
                redis = _get_redis()
            except Exception:
                logger.warning("G05 warm_cache: cannot connect to Redis — skipping warming")
                return 0

        for pattern in patterns:
            try:
                embedding = await _embed(pattern, embedding_model)
                key_suffix = hashlib.sha256(pattern.encode()).hexdigest()[:16]
                key = f"{prefix}tok_opt:l2:warm:{key_suffix}"
                await redis.set(key, json.dumps(embedding), ex=ttl)
                count += 1
                logger.debug("G05 warm_cache: stored embedding key=%s", key)
            except Exception as exc:
                logger.warning("G05 warm_cache: failed for pattern '%s...': %s", pattern[:30], exc)

        logger.info("G05 warm_cache: warmed %d/%d patterns", count, len(patterns))
        return count

    async def _store_step_cache(
        self, ctx: "RequestContext", response: Dict, cfg: Dict
    ) -> None:
        step_name = ctx.params.get("x_step_name")
        if not step_name:
            return
        inputs_hash = ctx.params.get("x_step_inputs_hash", "")
        template_version = ctx.params.get("x_template_version", "")
        key = _step_cache_key(step_name, inputs_hash, template_version, prefix=getattr(ctx, "redis_prefix", ""))
        ttl = cfg.get("step_cache_ttl_seconds", 86400)
        try:
            redis = _get_redis()
            await redis.set(key, json.dumps(response), ex=ttl)
            logger.debug("[%s] G05 step cache stored: %s", ctx.request_id, step_name)
        except Exception as exc:
            logger.warning("G05 step cache store failed: %s", exc)


async def _embed(text: str, model_name: str = _DEFAULT_L2_EMBEDDING_MODEL,
                 prefix: str = "") -> list:
    """Embed text using sentence-transformers (local, no API call).

    Model name is config-driven via G5_cache.l2_embedding_model.
    Default: BAAI/bge-small-en-v1.5 (MIT, 384-dim, higher MTEB than all-MiniLM-L6-v2).

    ``model.encode`` is a synchronous, CPU-bound call (and a cold load is 1–2s).
    Run it in a worker thread so it does not block the event loop and stall every
    other in-flight request behind this one embedding.
    Vectors are cached per tenant and keyed by content hash (embedding_cache), so the same
    query text asked twice - by the same app or a different one in the tenant - is encoded
    once. Embeddings are deterministic for a given (model, text), so this can only skip
    work, never change a retrieval result.
    """
    from embedding_cache import embed_cached

    return await embed_cached(text, model_name, prefix=prefix)


# Guard so the cache_l2 schema self-heal runs at most once per process.
_cache_l2_schema_ready = False
_cache_l2_schema_lock = asyncio.Lock()

# The HNSW index the lookup's ORDER BY uses (pgvector 0.5 or later). Without it every L2
# lookup scanned all of the tenant's rows on the request path.
_L2_INDEX = "idx_cache_l2_embedding"
_L2_INDEX_VALID_SQL = ("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                       " WHERE c.relname = $1")
_l2_index_tasks: set = set()
# pgvector 0.8 or later can keep scanning the index past rows the lookup's WHERE drops (another
# tenant's, another model scope's, expired ones); without that, a tenant with few rows among
# many can miss its cached answer. Off for the process once the server refuses it.
_iterative_scan = True

# The nearest row by distance - the shape the index serves - and whether it is close
# enough. The threshold is not a WHERE filter: when the nearest row fails it, no other
# row can pass, and the filter would only keep the index scan going.
_L2_LOOKUP_SQL = """
    SELECT response_json, 1 - (embedding <=> $1::vector) AS similarity,
           1 - (embedding <=> $1::vector) >= $2 AS close_enough
    FROM cache_l2
    WHERE tenant_id = $3
      AND model_scope = $4
      AND expires_at > NOW()
    ORDER BY embedding <=> $1::vector
    LIMIT 1
"""


async def _l2_index_valid(pool) -> bool:
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(_L2_INDEX_VALID_SQL, _L2_INDEX) is True
    except Exception as exc:
        logger.debug("G05 cache_l2 vector index check failed: %s", exc)
        return False


async def _build_l2_vector_index(pool) -> bool:
    """Build the HNSW index the lookup orders by, CONCURRENTLY so stores keep writing while it
    builds. A CONCURRENTLY build that died leaves an INVALID index, which IF NOT EXISTS would
    keep (and the planner never use), so it is dropped and built again. True once the index
    is ready; a failure (pgvector before 0.5, no rights on the table) is logged, not raised."""
    try:
        async with pool.acquire() as conn:
            valid = await conn.fetchval(_L2_INDEX_VALID_SQL, _L2_INDEX)
            if valid is True:
                return True
            if valid is False:
                await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_L2_INDEX}")
            await conn.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_L2_INDEX} "
                               "ON cache_l2 USING hnsw (embedding vector_cosine_ops)")
        logger.info("G05 cache_l2 vector index ready")
        return True
    except Exception as exc:
        if await _l2_index_valid(pool):          # another instance built it meanwhile
            return True
        logger.warning("G05 cache_l2 vector index could not be built, so each L2 lookup scans "
                       "the tenant's rows: %s", exc)
        return False


def _spawn_l2_index_build(pool) -> None:
    """Start the index build in the background, held until it finishes: on a large table it
    takes a while, and the request that ran the schema step must not wait for it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_build_l2_vector_index(pool))
    _l2_index_tasks.add(task)
    task.add_done_callback(_l2_index_tasks.discard)


async def finish_l2_index_build(pool) -> bool:
    """Wait for the index builds this process started, then whether the index is ready. For a
    one-shot schema job run as the table's owner: a proxy on a restricted runtime role cannot
    build the index (it does not own cache_l2), and a job that exited first would leave none."""
    if _l2_index_tasks:
        await asyncio.gather(*list(_l2_index_tasks), return_exceptions=True)
    return await _l2_index_valid(pool)


async def _allow_filtered_index_scan(conn) -> None:
    """Let the HNSW scan run past rows the lookup's WHERE drops (pgvector 0.8 or later). Set on
    this pooled connection, where it only ever affects HNSW scans."""
    global _iterative_scan
    if not _iterative_scan:
        return
    try:
        await conn.execute("SET hnsw.iterative_scan = strict_order")
    except Exception as exc:
        _iterative_scan = False
        logger.info("G05: this pgvector has no iterative index scans (%s), so a tenant with few "
                    "cached rows among many can miss its answer; pgvector 0.8 or later "
                    "(ALTER EXTENSION vector UPDATE) adds them", exc)


async def _ensure_cache_l2_schema(pool) -> None:
    """Self-heal the ``cache_l2`` table so L2 works on both fresh and
    persisted-old-schema databases (idempotent; runs once per process).

    There is no ``CREATE TABLE cache_l2`` anywhere else in the repo — the table
    was created by an old bootstrap and persists in the ``postgres_data`` volume.
    Older copies predate the ``tenant_id`` column that every L2 read/write now
    requires, so each lookup/store threw ``column "tenant_id" does not exist``
    and L2 silently collapsed to L1-only. ``CREATE … IF NOT EXISTS`` covers fresh
    DBs; ``ALTER … ADD COLUMN IF NOT EXISTS`` migrates the persisted table. This
    all happens on first use, not at import.
    """
    global _cache_l2_schema_ready
    if _cache_l2_schema_ready:
        return
    async with _cache_l2_schema_lock:
        if _cache_l2_schema_ready:
            return
        from cache.pg_pool import may_run_ddl
        if not await may_run_ddl(pool, "cache_l2"):
            # A restricted runtime role: the schema job, as the owner, keeps it current.
            _cache_l2_schema_ready = True
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_l2 (
                    id SERIAL PRIMARY KEY,
                    query_hash TEXT UNIQUE NOT NULL,
                    embedding vector(384),
                    response_json TEXT,
                    similarity_score double precision,
                    created_at timestamptz DEFAULT now(),
                    expires_at timestamptz,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )
            await conn.execute(
                "ALTER TABLE cache_l2 ADD COLUMN IF NOT EXISTS "
                "tenant_id TEXT NOT NULL DEFAULT 'default'"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_l2_tenant ON cache_l2 (tenant_id)"
            )
            # model_scope: '' in default "tenant" scope (matches all existing rows),
            # the requested model in "tenant+model" scope. Backward-compatible default.
            await conn.execute(
                "ALTER TABLE cache_l2 ADD COLUMN IF NOT EXISTS "
                "model_scope TEXT NOT NULL DEFAULT ''"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_l2_tenant_model "
                "ON cache_l2 (tenant_id, model_scope)"
            )
        _spawn_l2_index_build(pool)
        _cache_l2_schema_ready = True
        logger.info("G05 cache_l2 schema ensured (tenant_id + model_scope columns present)")


async def _l2_lookup(ctx: "RequestContext", threshold: float, embedding_model: str = _DEFAULT_L2_EMBEDDING_MODEL):
    from cache.pg_pool import get_pg_pool, tenant_conn

    if _semantic_cache_disabled(ctx):
        return None, 0.0

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return None, 0.0

    tenant_id = getattr(ctx, "tenant_id", "default")
    # Combined model+verbosity scope — MUST match _l2_store's INSERT value exactly, or
    # a terse-steered answer can be semantically served to a verbose-configured request
    # (and vice versa). Stored in the `model_scope` column (no migration needed — it's
    # a bare TEXT column; the DB column name predates verbosity scoping).
    scope_value = _scope_value(ctx)
    query_text = _semantic_query_text(_request_messages(ctx))
    if not query_text:
        # A user turn carries an image, audio or a file, or there is no user text:
        # nothing the embedding can safely match on. L1 exact-match still applies.
        return None, 0.0

    # M2: skip the semantic layer for over-window queries (truncation → collisions).
    cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G5_cache", {})
    if _embed_input_truncates(query_text, cfg):
        logger.debug(
            "[%s] G05 L2 lookup skipped: query %d chars exceeds embed window",
            getattr(ctx, "request_id", "?"), len(query_text),
        )
        return None, 0.0

    embedding = await _embed(query_text, embedding_model,
                             prefix=getattr(ctx, "redis_prefix", ""))

    # asyncpg has no built-in pgvector codec — pass embedding as a string
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    pool = await get_pg_pool(db_url)
    await _ensure_cache_l2_schema(pool)
    # I2: set app.tenant_id so RLS scopes this SELECT even if the WHERE were dropped.
    # A row past its expires_at (or without one, so of unknown age) is never served:
    # l2_ttl_seconds is enforced here, whether or not anything has deleted the row yet.
    async with tenant_conn(pool, tenant_id) as conn:
        await _allow_filtered_index_scan(conn)
        row = await conn.fetchrow(_L2_LOOKUP_SQL, embedding_str, threshold, tenant_id, scope_value)
        if row and row["close_enough"]:
            return json.loads(row["response_json"]), row["similarity"]
        return None, 0.0


async def _l2_store(ctx: "RequestContext", response: Dict, ttl: int, embedding_model: str = _DEFAULT_L2_EMBEDDING_MODEL) -> None:
    from cache.pg_pool import get_pg_pool, tenant_conn

    if _semantic_cache_disabled(ctx):
        return

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return

    tenant_id = getattr(ctx, "tenant_id", "default")
    # The request as sent, as the lookup embedded it; "" = the lookup skipped L2 too.
    query_text = _semantic_query_text(_request_messages(ctx))
    if not query_text:
        return

    # M2: don't store an over-window query — its truncated embedding would later
    # false-match a different long query. (L1 exact-match still caches it.)
    cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G5_cache", {})
    if _embed_input_truncates(query_text, cfg):
        logger.debug(
            "[%s] G05 L2 store skipped: query %d chars exceeds embed window",
            getattr(ctx, "request_id", "?"), len(query_text),
        )
        return

    embedding = await _embed(query_text, embedding_model,
                             prefix=getattr(ctx, "redis_prefix", ""))

    # asyncpg has no built-in pgvector codec — pass embedding as a string
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    # Combined model+verbosity scope — MUST match _l2_lookup's WHERE filter exactly;
    # this is what actually enforces isolation on the READ path (query_hash below only
    # dedupes the INSERT and is never consulted by SELECT). "" (unscoped) keeps both
    # the stored column value and the hash byte-identical to the pre-feature behaviour.
    scope_value = _scope_value(ctx)
    _hash_prefix = f"{tenant_id}:{scope_value}" if scope_value else tenant_id
    query_hash = hashlib.sha256(f"{_hash_prefix}:{query_text}".encode()).hexdigest()

    pool = await get_pg_pool(db_url)
    await _ensure_cache_l2_schema(pool)
    # I2: set app.tenant_id so RLS's WITH CHECK ties this INSERT to the tenant.
    async with tenant_conn(pool, tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO cache_l2 (query_hash, embedding, response_json, expires_at, tenant_id, model_scope)
            VALUES ($1, $2::vector, $3, NOW() + ($4 * interval '1 second'), $5, $6)
            ON CONFLICT (query_hash) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                response_json = EXCLUDED.response_json,
                expires_at = EXCLUDED.expires_at,
                tenant_id = EXCLUDED.tenant_id,
                model_scope = EXCLUDED.model_scope
            """,
            query_hash,
            embedding_str,
            json.dumps(response),
            ttl,
            tenant_id,
            scope_value,
        )
        try:
            await _purge_expired_l2(conn, tenant_id)
        except Exception as exc:
            logger.warning("G05 L2 expired-row purge failed: %s", exc)


# Expired L2 rows are deleted by the store itself: the retention job that also deletes
# them is off by default, so without this cache_l2 grew forever and every lookup scanned
# the dead rows too. At most one bounded batch per tenant per interval per process, and
# again on the next store while a backlog remains.
_L2_PURGE_INTERVAL_S = 300.0
_L2_PURGE_BATCH = 1000
_l2_next_purge: Dict[str, float] = {}


async def _purge_expired_l2(conn, tenant_id: str) -> None:
    now = time.monotonic()
    if now < _l2_next_purge.get(tenant_id, 0.0):
        return
    _l2_next_purge[tenant_id] = now + _L2_PURGE_INTERVAL_S
    status = await conn.execute(
        """
        DELETE FROM cache_l2 WHERE id IN (
            SELECT id FROM cache_l2
            WHERE tenant_id = $1 AND (expires_at IS NULL OR expires_at <= NOW())
            LIMIT $2)
        """,
        tenant_id, _L2_PURGE_BATCH,
    )
    try:
        deleted = int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        deleted = 0
    if deleted >= _L2_PURGE_BATCH:
        _l2_next_purge[tenant_id] = now   # a backlog remains: purge again on the next store
