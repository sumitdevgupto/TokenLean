"""
G05 · Response & Step Caching
Stage: At the Gate
Saving: 40–70% API calls eliminated
Technique:
  L1 Exact-match: hash(normalised_prompt) → Redis lookup (sub-millisecond)
  L2 Semantic:    embed query → pgvector cosine similarity (threshold from config)
  L3 GPTCache:    OSS semantic caching with similarity threshold
  Temporal:       Activity replay for durable step cache execution
  Auto-TTL:       Dynamic TTL based on hit rate and access patterns
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from middleware import langfuse_tracing

logger = logging.getLogger(__name__)
GROUP = "G05"

# Import CACHE_HITS counter from g18_observability (lazy import to avoid cycles)
def _get_cache_hits_counter():
    from middleware.g18_observability import CACHE_HITS
    return CACHE_HITS

# Configurable embedding model for L2 semantic cache
_DEFAULT_L2_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# L3 semantic cache (Headroom) — DISABLED.
# The real headroom.SemanticCache API differs from what the _l3_* helpers below
# assume: the ctor is SemanticCache(config, embedding_fn) (no scorer=) and the
# methods are get()/put(query, response, messages_hash) — no .search()/ttl=.
# Wiring it needs an embedding_fn + config rewrite and provides no TTL, while
# L1 (hash) + L2 (semantic) already cover caching. Left disabled pending a scoped
# rewrite; the _l3_lookup/_l3_store helpers below no-op while this is None.
_semantic_cache = None

def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


class AutoTTLManager:
    """Dynamic TTL management based on cache hit rates and access patterns.
    
    Implements adaptive TTL adjustment:
    - High hit rate (>80%): increase TTL by 25%
    - Low hit rate (<20%): decrease TTL by 25%
    - Access recency: reset TTL on access to hot items
    """
    
    def __init__(self, redis_client, prefix: str = ""):
        self.redis = redis_client
        # I4: tenant-scope the stat hashes so one tenant's hit-rate never skews
        # another tenant's adaptive TTL. prefix = ctx.redis_prefix ("" = default).
        self._prefix = prefix
        self._hit_key = f"{prefix}tok_opt:cache:stats:hits"
        self._miss_key = f"{prefix}tok_opt:cache:stats:misses"
        self._last_adjustment_key = f"{prefix}tok_opt:cache:stats:last_adjustment"

    async def record_hit(self, cache_level: str) -> None:
        """Record a cache hit for statistics."""
        try:
            await self.redis.hincrby(self._hit_key, cache_level, 1)
            await self.redis.hincrby(self._hit_key, "total", 1)
        except Exception as exc:
            logger.debug("AutoTTL hit recording failed: %s", exc)
    
    async def record_miss(self, cache_level: str) -> None:
        """Record a cache miss for statistics."""
        try:
            await self.redis.hincrby(self._miss_key, cache_level, 1)
            await self.redis.hincrby(self._miss_key, "total", 1)
        except Exception as exc:
            logger.debug("AutoTTL miss recording failed: %s", exc)
    
    async def get_recommended_ttl(self, base_ttl: int, cache_level: str) -> int:
        """Calculate recommended TTL based on hit rate."""
        try:
            hits = int(await self.redis.hget(self._hit_key, cache_level) or 0)
            misses = int(await self.redis.hget(self._miss_key, cache_level) or 0)
            total = hits + misses
            
            if total < 10:  # Not enough data
                return base_ttl
            
            hit_rate = hits / total
            
            # Adjust TTL based on hit rate
            if hit_rate > 0.80:
                # High hit rate - extend TTL
                new_ttl = int(base_ttl * 1.25)
                logger.debug("AutoTTL: high hit rate (%.2f), extending TTL %d → %d", hit_rate, base_ttl, new_ttl)
                return min(new_ttl, base_ttl * 2)  # Cap at 2x base
            elif hit_rate < 0.20:
                # Low hit rate - reduce TTL
                new_ttl = int(base_ttl * 0.75)
                logger.debug("AutoTTL: low hit rate (%.2f), reducing TTL %d → %d", hit_rate, base_ttl, new_ttl)
                return max(new_ttl, base_ttl // 4)  # Floor at 25% base
            
            return base_ttl
        except Exception as exc:
            logger.debug("AutoTTL calculation failed: %s", exc)
            return base_ttl


def _l3_scope_query(query: str, tenant_id: str) -> str:
    """Prefix query with tenant_id so SemanticCache similarity search never
    crosses tenant boundaries. Scoping the query text is the primary isolation
    guard; `_tenant_id` metadata tag in stored payloads is defense-in-depth."""
    return f"tenant:{tenant_id}|{query}"


async def _l3_lookup(query: str, threshold: float, tenant_id: str = "default") -> Tuple[Optional[Dict], float]:
    """Search headroom.SemanticCache with similarity threshold, scoped to tenant.

    Defense-in-depth: after a hit, verify the stored `_tenant_id` tag matches
    the caller's tenant — catches any future bypass where a caller passes an
    unscoped query to a second lookup call site.
    """
    if _semantic_cache is None:
        return None, 0.0

    scoped_query = _l3_scope_query(query, tenant_id)
    try:
        result = await asyncio.to_thread(
            lambda: _semantic_cache.search(scoped_query, threshold)
        )
        if result and len(result) > 0:
            data = result[0]
            response = data.get("response")
            similarity = data.get("similarity", 0.0)
            if isinstance(response, dict):
                stored_tenant = response.get("_tenant_id", "")
                if stored_tenant and stored_tenant != tenant_id:
                    logger.warning(
                        "L3 cross-tenant hit rejected: stored=%s caller=%s",
                        stored_tenant, tenant_id,
                    )
                    return None, 0.0
            return response, similarity
        return None, 0.0
    except Exception as exc:
        logger.debug("L3 SemanticCache lookup failed: %s", exc)
        return None, 0.0


async def _l3_store(query: str, response: Dict, ttl: int, tenant_id: str = "default") -> None:
    """Store response in headroom.SemanticCache, scoped to tenant_id."""
    if _semantic_cache is None:
        return

    scoped_query = _l3_scope_query(query, tenant_id)
    tagged_response = {**response, "_tenant_id": tenant_id}
    try:
        await asyncio.to_thread(
            lambda: _semantic_cache.put(scoped_query, {"response": tagged_response}, ttl=ttl)
        )
        logger.debug("L3 SemanticCache stored response")
    except Exception as exc:
        logger.debug("L3 SemanticCache store failed: %s", exc)


def _normalise(messages: list) -> str:
    """Normalise messages for the L1 exact-match key: strip whitespace, lowercase.

    Used for L1 only — exact matching needs the full request (system + every turn).
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            content = re.sub(r"\s+", " ", content).strip().lower()
        parts.append(f"{role}:{content}")
    return "|".join(parts)


def _semantic_query_text(messages: list) -> str:
    """Text to embed for L2/L3 semantic matching: the user turns only.

    The system prompt is fixed infrastructure (role, policies, formatting). Embedding
    it lets a prompt longer than the embedding window (~512 tokens for bge-small)
    dominate and truncate the vector, collapsing distinct user queries onto the same
    point — which returns a cached answer for a *different* question. Matching on the
    user turns keys the semantic cache on what actually carries the query intent.
    Falls back to the full normalised string when there are no user turns.
    """
    users = [
        re.sub(r"\s+", " ", m.get("content", "")).strip().lower()
        for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    text = "\n".join(u for u in users if u)
    return text or _normalise(messages)


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
    returned), not just the embeddable user text. A *fuzzy* L2/L3 semantic match
    between two different continuations can therefore return a response generated
    for a DIFFERENT state — e.g. one turn's tool plan served for another turn's
    question. (Observed: a follow-up asking for a user profile matched the prior
    "fetch logs" turn at cosine 0.99 because the shared, long first turn
    dominates the embedding.) L1 exact-match is unaffected — only the fuzzy
    semantic layers are skipped.
    """
    msgs = getattr(ctx, "messages", None) or []
    return any(
        isinstance(m, dict) and m.get("role") in ("assistant", "tool")
        for m in msgs
    )


def _semantic_cache_disabled(ctx) -> bool:
    """Whether L2/L3 *semantic* caching must be skipped for this request.

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

def _resolve_cache_scope(ctx) -> str:
    """Return "tenant" (default) or "tenant+model".

    Per-tenant override (``tenants.<id>.groups.G5_cache.cache_scope``) wins over
    the global ``G5_cache.cache_scope``. Any value other than "tenant+model" → "tenant".
    """
    base = ctx.config.get("groups", {}).get("G5_cache", {}).get("cache_scope", "tenant")
    tenant_cfg = (
        ctx.config.get("tenants", {})
        .get(getattr(ctx, "tenant_id", "") or "", {})
        .get("groups", {})
        .get("G5_cache", {})
    )
    scope = str(tenant_cfg.get("cache_scope", base)).lower()
    return "tenant+model" if scope in ("tenant+model", "tenant_model", "model") else "tenant"


def _model_scope_tag(ctx) -> str:
    """Model component of the cache key when scope == "tenant+model", else "".

    Uses the *requested* model (``ctx.model`` / ``params["model"]``): G05 runs
    before G06 routing, and the caller chose this model deliberately, so the
    requested model is the stable, correct key (identical at lookup and store).
    """
    if _resolve_cache_scope(ctx) != "tenant+model":
        return ""
    return getattr(ctx, "model", None) or ctx.params.get("model") or ""


def _apply_model_scope(text: str, ctx) -> str:
    """Fold the model tag into a cache-key source string. No-op in "tenant" scope
    (returns ``text`` unchanged → keys stay byte-identical to pre-feature)."""
    tag = _model_scope_tag(ctx)
    return f"model={tag}\n{text}" if tag else text


def _step_cache_key(step_name: str, inputs_hash: str, template_version: str, prefix: str = "") -> str:
    payload = f"{step_name}|{inputs_hash}|{template_version}"
    return f"{prefix}tok_opt:step:" + hashlib.sha256(payload.encode()).hexdigest()


def _hash_args(args: tuple, kwargs: dict) -> str:
    """Hash function arguments for Temporal replay cache key."""
    payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class G05Cache:
    """G05 cache with L1 (Redis exact), L2 (pgvector semantic), L3 (GPTCache), and auto-TTL."""
    
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

        tokens_before = ctx.current_token_count
        normalised = _normalise(ctx.messages)
        ns = getattr(ctx, "redis_prefix", "")  # tenant namespace prefix
        ttl_manager = self._get_ttl_manager(ns)  # I4: tenant-scoped TTL stats

        # Get auto-TTL adjusted values
        l1_base_ttl = cfg.get("l1_ttl_seconds", 3600)
        l2_base_ttl = cfg.get("l2_ttl_seconds", 86400)
        l1_ttl = await ttl_manager.get_recommended_ttl(l1_base_ttl, "L1") if ttl_manager else l1_base_ttl
        l2_ttl = await ttl_manager.get_recommended_ttl(l2_base_ttl, "L2") if ttl_manager else l2_base_ttl

        # L1 — exact match
        try:
            redis = _get_redis()
            key = _cache_key(_apply_model_scope(normalised, ctx), prefix=ns)
            # Store key on context for reuse in store_response (Phase 2 fix)
            ctx.params["_g05_l1_cache_key"] = key
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

        # L3 — headroom.SemanticCache(scorer="hybrid")
        # Config key: l3_enabled (preferred) or gptcache_enabled (backward compat)
        l3_enabled = cfg.get("l3_enabled", cfg.get("gptcache_enabled", False))
        if l3_enabled and _semantic_cache is not None and not _semantic_cache_disabled(ctx):
            l3_threshold = cfg.get("l3_similarity_threshold", cfg.get("gptcache_similarity_threshold", 0.85))
            try:
                cached_response, score = await _l3_lookup(
                    _semantic_query_text(ctx.messages), l3_threshold,
                    tenant_id=getattr(ctx, "tenant_id", "default"),
                )
                if cached_response:
                    ctx.cache_hit = True
                    ctx.cache_level = "L3"
                    ctx.cache_response = cached_response
                    ctx.savings.cache_hit = True
                    ctx.savings.cache_level = "L3"
                    ctx.savings.final_tokens_sent = 0
                    ctx.savings.proxy_optimised_tokens = 0   # B1: nothing sent to LLM
                    ctx.savings.provider_prompt_tokens = 0
                    ctx.savings.add_step(
                        GROUP,
                        f"L3 SemanticCache hit (score={score:.3f})",
                        tokens_before,
                        0,
                    )
                    langfuse_tracing.add_span(
                        ctx,
                        name="G05-cache",
                        span_input={"tokens_before": tokens_before, "l3_threshold": l3_threshold},
                        output={"cache_level": "L3", "tokens_after": 0},
                        metadata={"cache_hit": True, "level": "L3", "similarity_score": round(score, 3)},
                    )
                    logger.debug("[%s] G05 L3 SemanticCache hit score=%.3f", ctx.request_id, score)
                    if ttl_manager:
                        await ttl_manager.record_hit("L3")
                    return ctx
                else:
                    if ttl_manager:
                        await ttl_manager.record_miss("L3")
            except Exception as exc:
                logger.debug("G05 L3 SemanticCache error: %s", exc)

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
        """Store the LLM response in L1, L2, L3, and step caches after a successful call."""
        if ctx.cache_hit or ctx.bypassed:
            return

        cfg = ctx.config.get("groups", {}).get("G5_cache", {})
        if not cfg.get("enabled", False):
            return

        # Get auto-TTL adjusted values (I4: tenant-scoped TTL stats)
        ttl_manager = self._get_ttl_manager(getattr(ctx, "redis_prefix", ""))
        l1_base_ttl = cfg.get("l1_ttl_seconds", 3600)
        l2_base_ttl = cfg.get("l2_ttl_seconds", 86400)
        l1_ttl = await ttl_manager.get_recommended_ttl(l1_base_ttl, "L1") if ttl_manager else l1_base_ttl
        l2_ttl = await ttl_manager.get_recommended_ttl(l2_base_ttl, "L2") if ttl_manager else l2_base_ttl

        # Use stored key from lookup if available (Phase 2 fix: ensures consistency)
        key = ctx.params.get("_g05_l1_cache_key")
        if not key:
            # Fallback: recompute (should not happen in normal flow after fix).
            # Mirror the lookup key exactly: tenant prefix + model scope.
            normalised = _normalise(ctx.messages)
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

        # L3 SemanticCache store
        l3_enabled = cfg.get("l3_enabled", cfg.get("gptcache_enabled", False))
        if l3_enabled and _semantic_cache is not None and not _semantic_cache_disabled(ctx):
            try:
                await _l3_store(
                    _semantic_query_text(ctx.messages), response, l2_ttl,
                    tenant_id=getattr(ctx, "tenant_id", "default"),
                )
            except Exception as exc:
                logger.debug("G05 L3 SemanticCache store failed: %s", exc)

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


async def _embed(text: str, model_name: str = _DEFAULT_L2_EMBEDDING_MODEL) -> list:
    """Embed text using sentence-transformers (local, no API call).
    
    Model name is config-driven via G5_cache.l2_embedding_model.
    Default: BAAI/bge-small-en-v1.5 (MIT, 384-dim, higher MTEB than all-MiniLM-L6-v2).
    """
    from ml_models import get_sentence_transformer
    model = get_sentence_transformer(model_name)
    return model.encode(text).tolist()


# Guard so the cache_l2 schema self-heal runs at most once per process.
_cache_l2_schema_ready = False
_cache_l2_schema_lock = asyncio.Lock()


async def _ensure_cache_l2_schema(pool) -> None:
    """Self-heal the ``cache_l2`` table so L2 works on both fresh and
    persisted-old-schema databases (idempotent; runs once per process).

    There is no ``CREATE TABLE cache_l2`` anywhere else in the repo — the table
    was created by an old bootstrap and persists in the ``postgres_data`` volume.
    Older copies predate the ``tenant_id`` column that every L2 read/write now
    requires, so each lookup/store threw ``column "tenant_id" does not exist``
    and L2 silently collapsed to L1-only. ``CREATE … IF NOT EXISTS`` covers fresh
    DBs; ``ALTER … ADD COLUMN IF NOT EXISTS`` migrates the persisted table. This
    mirrors the create-on-use pattern in ``g07_pgvector_fallback.py``.
    """
    global _cache_l2_schema_ready
    if _cache_l2_schema_ready:
        return
    async with _cache_l2_schema_lock:
        if _cache_l2_schema_ready:
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
    model_scope = _model_scope_tag(ctx)  # "" in tenant scope; requested model in tenant+model
    query_text = _semantic_query_text(ctx.messages)

    # M2: skip the semantic layer for over-window queries (truncation → collisions).
    cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G5_cache", {})
    if _embed_input_truncates(query_text, cfg):
        logger.debug(
            "[%s] G05 L2 lookup skipped: query %d chars exceeds embed window",
            getattr(ctx, "request_id", "?"), len(query_text),
        )
        return None, 0.0

    embedding = await _embed(query_text, embedding_model)

    # asyncpg has no built-in pgvector codec — pass embedding as a string
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    pool = await get_pg_pool(db_url)
    await _ensure_cache_l2_schema(pool)
    # I2: set app.tenant_id so RLS scopes this SELECT even if the WHERE were dropped.
    async with tenant_conn(pool, tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT response_json, 1 - (embedding <=> $1::vector) AS similarity
            FROM cache_l2
            WHERE 1 - (embedding <=> $1::vector) >= $2
              AND tenant_id = $3
              AND model_scope = $4
            ORDER BY similarity DESC
            LIMIT 1
            """,
            embedding_str, threshold, tenant_id, model_scope,
        )
        if row:
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
    query_text = _semantic_query_text(ctx.messages)

    # M2: don't store an over-window query — its truncated embedding would later
    # false-match a different long query. (L1 exact-match still caches it.)
    cfg = (getattr(ctx, "config", {}) or {}).get("groups", {}).get("G5_cache", {})
    if _embed_input_truncates(query_text, cfg):
        logger.debug(
            "[%s] G05 L2 store skipped: query %d chars exceeds embed window",
            getattr(ctx, "request_id", "?"), len(query_text),
        )
        return

    embedding = await _embed(query_text, embedding_model)

    # asyncpg has no built-in pgvector codec — pass embedding as a string
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    model_scope = _model_scope_tag(ctx)  # "" in tenant scope; requested model in tenant+model
    # query_hash includes tenant_id (and model_scope when scoped) so different
    # tenants — and, in tenant+model scope, different requested models — never
    # collide on the same row (the unique constraint is on query_hash alone).
    # model_scope="" keeps the hash byte-identical to the pre-feature behaviour.
    _hash_prefix = f"{tenant_id}:{model_scope}" if model_scope else tenant_id
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
            model_scope,
        )
