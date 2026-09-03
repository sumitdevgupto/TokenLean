"""
G28 · Contextual Content Reuse (CCR) — headroom.ccr
Stage: Into the LLM (request-side), tool result retrieval (response-side)
Saving: context tokens, but ONLY net-positive when a stateful agent client reuses
        the reference across turns. Off by default — see the pass-through caveat.

Technique (two-part):
  Request-side  — replaces any string message whose content is at or above
                  ``min_tokens`` with a compact ``[CCR:sha256_prefix]`` marker and
                  stores the full text in Redis (in-process fallback) with a TTL.
                  The SHA only dedupes STORAGE — replacement happens on first sight,
                  not just on repeats. The system role is preserved verbatim unless
                  ``compress_system_prompt`` is set (default off).

  Response-side / MCP tool registration — exposes three MCP tools via G15:
    headroom_compress(text)  → stores text, returns reference token
    headroom_retrieve(ref)   → returns stored text for a reference token
    headroom_stats()         → returns cache size and hit/miss counts

  PASS-THROUGH CAVEAT: a ``[CCR:ref]`` is only resolvable by a client that runs the
  retrieve loop (the model calls ``headroom_retrieve`` and the client re-sends the
  result). A plain chat completion never resolves it, so the model would answer from
  a gutted context — which is why ``enabled`` defaults to false and the system role
  is never replaced. Building a server-side resolve loop is rejected on purpose: it
  would re-inject the retrieved text into a second LLM call (net-negative tokens).

  When headroom is not installed, the module is a transparent no-op on both
  paths. When Redis is unavailable, the request-side path uses an in-process store.

Config key: G28_ccr
"""
import hashlib
import time
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext, resolve_group_config
from middleware import langfuse_tracing
from savings.calculator import estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G28"

# ─── Headroom CCR integration: DISABLED ───────────────────────────────────────
# The real headroom.ccr module is a tool-injection / MCP architecture
# (CCRToolInjector, ContextTracker, …) with NO module-level compress()/retrieve(),
# so this import always failed and G28 has always used the built-in
# [CCR:sha256] + Redis store below. Wiring Headroom's CCR is a dedicated MCP task;
# the built-in implements the same concept. (_ccr_available stays False.)
_ccr_available = False
_ccr_compress_fn = None
_ccr_retrieve_fn = None

# ─── Content store ────────────────────────────────────────────────────────────
#
# Redis-backed and content-addressed, with a bounded in-process read-through cache in
# front (same shape as g26_context_budget's summary cache). Two properties make this safe
# where the previous in-process dict was not:
#
#   * DURABLE — a [CCR:ref] resolves after a restart, an idle scale-to-zero, and from any
#     instance. The old store was a module-level dict, so a reference died with the process
#     that made it and a cross-turn retrieve failed silently on a billed 200.
#   * CONTENT-ADDRESSED — the key IS sha256(value), so concurrent writers of identical
#     content write identical bytes to the same key. Writes are idempotent by construction,
#     which is what removes the concurrency problem a shared cross-artefact cache would
#     otherwise have, and it means a second artefact sending the same document reuses the
#     first one's block instead of storing its own copy.
#
# If the durable store is unreachable we REFUSE TO SUBSTITUTE (see `_store`) rather than
# falling back to process memory: a reference nothing can resolve is worse than sending the
# content, because the request still bills as a 200 while the answer quietly degrades.
_STORE_IS_DURABLE = True


_local_store: Dict[str, Tuple[float, str]] = {}
# hits/misses per tenant prefix. A single global pair was readable by any tenant via
# headroom_stats (see that branch below).
_stats: Dict[str, Dict[str, int]] = {}


# Tenants observed actually resolving a CCR reference. A client only earns reference
# substitution by demonstrating it can resolve one — see the handshake note in
# process_request. Process-local and intentionally so: it is a conservative gate, and the
# worst case of "forgot after a restart" is one honest full-content turn, whereas the worst
# case of wrongly assuming capability is a silently degraded answer.
_resolvers_proven: Dict[str, float] = {}
_RESOLVER_PROOF_TTL = 3600.0


def _resolver_proven(prefix: str) -> bool:
    seen = _resolvers_proven.get(prefix)
    return seen is not None and (time.time() - seen) < _RESOLVER_PROOF_TTL


def _mark_resolver_proven(prefix: str) -> None:
    """Called when a client successfully resolves a reference — it has an agent loop."""
    _resolvers_proven[prefix] = time.time()


def _revoke_resolver_proof(prefix: str) -> None:
    """Called when a client answered a turn WITHOUT resolving a reference we substituted.

    The proof used to be write-once: resolve successfully a single time and the tenant was
    trusted until the TTL expired, no matter what happened afterwards. So a client that
    stopped resolving — a different model, a changed agent loop, or simply a turn where the
    model could not be bothered — kept receiving references it never read, and answered from
    the one-line summary on a billed 200. Proven live on 2026-09-03 (DS22): with the CCR
    tools advertised, gpt-4o-mini returned `tool_calls: []` / `finish_reason: stop` and
    invented the facts that lived in the parked document.

    Revoking costs at most one honest full-content turn; NOT revoking costs a wrong answer
    that reports as a saving. The trade is deliberately asymmetric, in the same direction as
    every other guard in this module.
    """
    _resolvers_proven.pop(prefix, None)


def _record_ignored_reference(prefix: str) -> None:
    """Count a substituted reference the model never resolved. Without this the failure is
    invisible: the request succeeds, the savings step is recorded, and only the answer is
    wrong."""
    try:
        from middleware.g18_observability import CCR_IGNORED_REFS

        CCR_IGNORED_REFS.labels(tenant_id=prefix or "default").inc()
    except Exception:  # noqa: BLE001 — metrics must never break a request
        logger.debug("G28: could not record ignored-reference metric", exc_info=True)


def _record_miss(prefix: str) -> None:
    """Count an unresolvable reference. Silent misses are why this failure used to look
    like a model-quality problem instead of a storage problem."""
    try:
        from middleware.g18_observability import CCR_MISSES
        CCR_MISSES.labels(tenant_id=(prefix or "default").strip(":") or "default").inc()
    except Exception:
        pass


def _stats_for(prefix: str = "") -> Dict[str, int]:
    return _stats.setdefault(prefix, {"hits": 0, "misses": 0})


_UNAVAILABLE_LOGGED = False


def ccr_available(cfg: Dict[str, Any], ctx) -> bool:
    """Public alias — is CCR both configured on AND safe to run for this request?

    G15 auto-executes CCR tools and must ask the same question G28 asks, rather than
    inferring it from a flag G28 happens to set.
    """
    return _ccr_enabled(cfg, ctx)


def _ccr_enabled(cfg: Dict[str, Any], ctx) -> bool:
    """True only if G28 is both configured on AND safe to run.

    The config toggle alone is not enough. CCR replaces content the model needs with a
    reference it can only resolve from a store that does not survive an instance
    recycle — so honouring `enabled: true` today means silently degrading answers on a
    billed 200, which is worse than the feature being unavailable. Refusing is loud;
    the failure it prevents is not.
    """
    if not cfg.get("enabled", False):
        return False
    if _STORE_IS_DURABLE:
        return True
    global _UNAVAILABLE_LOGGED
    if not _UNAVAILABLE_LOGGED:
        _UNAVAILABLE_LOGGED = True
        logger.error(
            "G28 CCR is enabled in config but REFUSED: its content store is in-process "
            "only, so a [CCR:ref] does not survive a scale-out, a restart, or an idle "
            "scale-to-zero. Enabling it would silently degrade answers on billed 200s. "
            "Treating G28 as disabled. See demand-driven-features.md #28."
        )
    return False


def authorize_dispatch(ctx, tool_name: str):
    """See middleware.g32_tool_eligibility.authorize_dispatch. Lazy import: G32 owns
    tool authorization, and a module-level import here would be circular."""
    from middleware.g32_tool_eligibility import authorize_dispatch as _authz
    return _authz(ctx, tool_name)


def record_dispatch_block(ctx, tool_name: str, reason: str) -> None:
    from middleware.g32_tool_eligibility import record_dispatch_block as _rec
    _rec(ctx, tool_name, reason)

# The three CCR tools the proxy hosts server-side. ONE definition: G15 imports this
# rather than keeping its own copy, and G28's response loop uses it instead of the
# inline string tuple it used to re-list — two independent lists of the same names is
# exactly the drift that produced the missing-tenant-prefix bug (public 2768392).
_CCR_MCP_TOOLS = frozenset({"headroom_compress", "headroom_retrieve", "headroom_stats"})

# Reference token format: [CCR:hex8]
_REF_PREFIX = "[CCR:"
_REF_SUFFIX = "]"


def _sha_from_ref(ref: str) -> Optional[str]:
    """Extract the sha from a reference the MODEL typed back, tolerating its formatting.

    The security property is the 64-hex exact-key lookup, not the decorative brackets. A model
    copying `[CCR:<sha>]` out of a prompt reasonably often re-emits it as `CCR:<sha>` or as the
    bare hash - it reads the delimiters as markup. Rejecting those cost a real answer: on
    2026-09-03 DS22 thread 02 called headroom_retrieve twice with `"CCR:01bbfa2c..."`, was
    refused both times for the missing brackets alone, and the model then improvised - on a
    billed 200, with a 64-hex sha that was perfectly correct.

    This does NOT loosen #29: that was 8-char prefixes resolved by SCANNING, where a collision
    returned a different document. Exactly 64 hex characters and an exact keyed GET are still
    required; only the wrapper is optional.
    """
    if not isinstance(ref, str):
        return None
    candidate = ref.strip()
    if candidate.startswith(_REF_PREFIX) and candidate.endswith(_REF_SUFFIX):
        candidate = candidate[len(_REF_PREFIX):-len(_REF_SUFFIX)]
    elif candidate.upper().startswith("CCR:"):
        candidate = candidate[4:]
    candidate = candidate.strip().strip("[]").strip()
    if len(candidate) != 64 or not all(c in "0123456789abcdef" for c in candidate.lower()):
        return None
    return candidate.lower()


def _make_ref(sha: str) -> str:
    """Reference token carrying the FULL sha256.

    It used to carry only ``sha[:8]`` — 32 bits — and retrieval scanned for the first
    insertion-order key with that prefix. Two blocks colliding on 8 hex chars (~1.2% at 10k
    stored blocks) meant the model was handed a DIFFERENT document with a hit counted and
    no error anywhere. The full sha makes retrieval an exact keyed GET: nothing to collide
    with, nothing to scan, and no need to verify after the fact (backlog #29).
    """
    return f"{_REF_PREFIX}{sha}{_REF_SUFFIX}"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _store_key(prefix: str, sha: str) -> str:
    """Content-addressed, tenant-scoped key.

    The key IS the hash of the value, so concurrent writers of identical content write
    identical bytes to the same key — writes are idempotent by construction and there is no
    lost-update to coordinate. That is what makes a shared cross-artefact store tractable
    here rather than a distributed-systems project.

    The tenant prefix is not optional: without it an 8-char reference used to resolve to
    another tenant's block, and the default tenant's prefix is the EMPTY string, so a
    ``startswith(prefix)`` scan matched every tenant's keys. Exact keys end that whole class.
    """
    return f"{prefix}ccr:{sha}"


# ─── Storage helpers ─────────────────────────────────────────────────────────
#
# Redis-first with a BOUNDED in-process read-through cache, mirroring the proven shape in
# g26_context_budget (_cached_summary_get/_local_cache_get). The local dict is a cache and
# never the system of record: a reference that only exists in one process's memory cannot
# be resolved by the next request, which is the failure that kept CCR switched off.

_LOCAL_STORE_MAX = 512


def _local_get(key: str) -> Optional[str]:
    entry = _local_store.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if expires_at <= time.time():
        _local_store.pop(key, None)
        return None
    return value


def _local_put(key: str, value: str, ttl: int) -> None:
    """Expiry-aware put with purge-then-evict. The old dict honoured neither the ttl arg
    nor any size cap, so a long-lived proxy accumulated model-supplied text forever."""
    now = time.time()
    for k in [k for k, (exp, _) in _local_store.items() if exp <= now]:
        _local_store.pop(k, None)
    while len(_local_store) >= _LOCAL_STORE_MAX:
        oldest = min(_local_store, key=lambda k: _local_store[k][0])
        _local_store.pop(oldest, None)
    _local_store[key] = (now + max(1, ttl), value)


async def _store(key: str, text: str, ttl: int, prefix: str = "") -> bool:
    """Persist one content block. Returns True only if it reached the DURABLE store.

    The caller must not substitute a reference when this returns False: a reference the
    model cannot resolve later is worse than sending the content, because the request still
    bills as a 200 while the answer quietly degrades.
    """
    full_key = _store_key(prefix, key)
    try:
        from cache.redis_pool import get_redis
        await get_redis().set(full_key, text, ex=max(1, ttl))
        _local_put(full_key, text, ttl)
        return True
    except Exception as exc:
        logger.warning("G28 CCR durable store unavailable, refusing to substitute: %s", exc)
        return False


async def _retrieve_stored(key: str, prefix: str = "") -> Optional[str]:
    full_key = _store_key(prefix, key)
    cached = _local_get(full_key)
    if cached is not None:
        return cached
    try:
        from cache.redis_pool import get_redis
        raw = await get_redis().get(full_key)
    except Exception as exc:
        logger.debug("G28 CCR retrieve failed: %s", exc)
        return None
    if raw is None:
        return None
    val = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    _local_put(full_key, val, 300)
    return val


# ─── MCP tool definitions ─────────────────────────────────────────────────────

def _build_mcp_tools() -> List[Dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for the three CCR MCP tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "headroom_compress",
                "description": (
                    "Store a verbatim text block and return a compact reference token. "
                    "Use this before passing large repeated content to the model."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The text to compress."},
                        "ttl": {"type": "integer", "description": "TTL in seconds (default: 86400)."},
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "headroom_retrieve",
                "description": "Retrieve the full text stored for a CCR reference token.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "The CCR reference token (e.g. [CCR:abcd1234])."},
                    },
                    "required": ["ref"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "headroom_stats",
                "description": "Return CCR cache statistics (size, hit count, miss count).",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
    ]


# ─── Request-side content replacement ────────────────────────────────────────

async def _replace_content(
    content: str,
    min_tokens: int,
    model: str,
    ttl: int,
    prefix: str = "",
    may_substitute: bool = False,
) -> Tuple[str, bool]:
    """Return (possibly_replaced_content, was_replaced).

    Storing and substituting are deliberately separate decisions:

    * we ALWAYS store (cheap, content-addressed, deduplicates across artefacts — a second
      app sending the same document reuses the first one's block rather than writing its
      own copy);
    * we substitute a reference ONLY when ``may_substitute`` says the caller has proven it
      can resolve one, and only when the block actually reached the durable store.

    That separation is the fix for the 2026-06-30 regression, where a reference replaced a
    676-token system prompt in a pass-through completion that had no agent loop to call
    ``headroom_retrieve`` — the model answered from generic knowledge on a billed 200.
    """
    if estimate_tokens(content, model) < min_tokens:
        return content, False

    sha = _sha256_hex(content)

    # Already stored? Content-addressed, so a hit means an identical block exists — no
    # second write, and the miss counter is NOT bumped: a first-sight store is not a
    # failed retrieval, and conflating them made headroom_stats unreadable.
    existing = await _retrieve_stored(sha, prefix=prefix)
    stored = True if existing is not None else await _store(sha, content, ttl, prefix=prefix)

    if not (may_substitute and stored):
        return content, False
    return _make_ref(sha), True


async def _process_messages(
    messages: List[Dict],
    min_tokens: int,
    model: str,
    ttl: int,
    compress_system: bool = False,
    prefix: str = "",
    may_substitute: bool = False,
) -> Tuple[List[Dict], int, int]:
    """Walk messages and replace large text blocks with CCR references.

    The system role is preserved verbatim unless ``compress_system`` is True. In a
    pass-through chat completion there is no agent loop to call ``headroom_retrieve``,
    so replacing the system instruction with a reference the model can't resolve
    silently strips the policy/facts the answer depends on. Default off mirrors
    G01's ``compress_system_prompt`` guard.

    Returns: (new_messages, tokens_before, tokens_after)
    """
    new_messages = []
    tokens_before = 0
    tokens_after = 0

    compressible_roles = (
        ("user", "assistant", "system") if compress_system else ("user", "assistant")
    )

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, str) and role in compressible_roles:
            t_before = estimate_tokens(content, model)
            new_content, replaced = await _replace_content(
                content, min_tokens, model, ttl, prefix=prefix,
                may_substitute=may_substitute)
            t_after = estimate_tokens(new_content, model)
            tokens_before += t_before
            tokens_after += t_after
            if replaced:
                new_messages.append({**msg, "content": new_content})
                continue

        new_messages.append(msg)
        t = estimate_tokens(str(content), model)
        tokens_before += t
        tokens_after += t

    return new_messages, tokens_before, tokens_after


# ─── MCP tool dispatch ────────────────────────────────────────────────────────

async def dispatch_mcp_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    ttl: int = 86400,
    prefix: str = "",
    max_store_chars: int = 200_000,
) -> Any:
    """Dispatch a CCR MCP tool call; return the tool result as a JSON-serialisable value."""
    if tool_name == "headroom_compress":
        text = arguments.get("text", "")
        call_ttl = arguments.get("ttl", ttl)
        if not text:
            return {"error": "text is required"}
        # Cap model-supplied input. This writes arbitrary-length text the model chose into
        # shared storage; unbounded, one caller can fill the store for everyone.
        if len(text) > max_store_chars:
            return {"error": f"text exceeds {max_store_chars} characters"}
        sha = _sha256_hex(text)
        if not await _store(sha, text, call_ttl, prefix=prefix):
            return {"error": "CCR store unavailable"}
        return {"ref": _make_ref(sha), "sha256": sha, "original_len": len(text)}

    if tool_name == "headroom_retrieve":
        ref = arguments.get("ref", "")
        sha = _sha_from_ref(ref)
        if sha is None:
            return {"error": f"Invalid CCR reference: {ref!r}"}
        # Exact keyed GET on the full sha — no prefix scan. The old scan returned the first
        # insertion-order match on 8 hex chars, so a collision silently handed the model a
        # DIFFERENT document; and because the default tenant's prefix is the empty string,
        # `startswith(prefix)` matched every tenant's keys. _sha_from_ref enforces the
        # 64-hex requirement, so anything reaching here is exact-lookup-safe.
        val = await _retrieve_stored(sha, prefix=prefix)
        if val is None:
            _stats_for(prefix)["misses"] += 1
            # A miss used to be a debug log and nothing else, on a billed 200 — so the model
            # answered from its own earlier paraphrase and the failure presented as a
            # model-quality problem. Make it loud and countable.
            _record_miss(prefix)
            logger.warning("G28 CCR reference not found (prefix=%s): %s", prefix, ref)
            return {"error": "Reference not found"}
        _stats_for(prefix)["hits"] += 1
        # Proof of capability: this caller runs a retrieve loop, so later turns may receive
        # references instead of full content.
        _mark_resolver_proven(prefix)
        return {"text": val}

    if tool_name == "headroom_stats":
        # Tenant-scoped: a single process-global counter leaked co-tenants' request volume,
        # block-size distribution and activity timing to anyone polling this tool.
        tenant_stats = _stats_for(prefix)
        return {
            "local_store_size": sum(1 for k in _local_store if k.startswith(prefix)),
            "hits": tenant_stats["hits"],
            "misses": tenant_stats["misses"],
        }

    return {"error": f"Unknown CCR tool: {tool_name}"}


# ─── Per-tenant config resolution ─────────────────────────────────────────────



def _resolve_g28_cfg(ctx: RequestContext) -> Dict[str, Any]:
    """G28 config with the per-tenant override deep-merged in (tenant wins).

    Delegates to the shared ``middleware.resolve_group_config`` so every group
    resolves the tenant overlay identically AND inherits its type guards:
    ``config.yaml`` is operator-edited, and an unguarded ``.get()`` chain over a
    mis-indented ``tenants:`` block raised ``AttributeError`` here — a 500 on every
    request for that tenant.
    """
    return resolve_group_config(ctx, "G28_ccr")



# ─── G28 Middleware ───────────────────────────────────────────────────────────

class G28CCR:
    """
    Contextual Content Reuse — replace repeated verbatim blocks with compact refs.
    Reference: G28 in token_optimization_playbook_v7.md
    """

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = _resolve_g28_cfg(ctx)
        if not _ccr_enabled(cfg, ctx):
            return ctx

        min_tokens: int = cfg.get("min_tokens", 300)
        ttl: int = cfg.get("ttl_seconds", 86400)
        # Off by default: never replace the system instruction in pass-through, where
        # the model can't resolve a CCR reference (no agent loop). Opt in only for
        # clients that run the retrieve loop.
        compress_system: bool = cfg.get("compress_system_prompt", False)
        prefix = getattr(ctx, "redis_prefix", "")

        # ── Resolve-capability handshake ────────────────────────────────────────
        # NEVER substitute a reference the caller cannot resolve. A [CCR:ref] is only
        # resolvable by a client that runs an agent loop and calls headroom_retrieve; in a
        # pass-through completion nothing does, so substitution silently strips the very
        # content the answer depends on and the model improvises — on a billed 200.
        #
        # A caller earns substitution by demonstrating it: the first turn stores the block
        # and sends the content IN FULL, and only once we have actually observed that
        # tenant resolving a reference do later turns get the savings. Storing regardless
        # is what makes that first turn cheap for everyone afterwards, including a second
        # artefact sending the same document.
        # HARD precondition, deliberately NOT overridable: never substitute a reference into
        # a request that carries no means of resolving it. The CCR tools are advertised only
        # to callers that already send tools (see the injection block below), so a caller
        # sending none is handed a [CCR:ref] and no headroom_retrieve to call — the reference
        # is unresolvable by construction, not by policy.
        #
        # DS22's first two live runs (2026-09-03) were exactly this: the dataset sent no
        # tools, so nothing was advertised, `require_proven_resolver: false` waved the soft
        # guard through, and the model answered from the summary with every planted fact
        # gone while the run recorded 44.99% "savings". The soft handshake is a trust
        # decision an operator may reasonably override; this one is a physical impossibility,
        # so it is not offered as a knob.
        will_advertise_tools = bool(cfg.get("expose_mcp_tools", True)) and bool(ctx.params.get("tools"))
        may_substitute = (
            will_advertise_tools
            and (_resolver_proven(prefix) if cfg.get("require_proven_resolver", True) else True)
        )
        if not will_advertise_tools:
            logger.debug(
                "[%s] G28 CCR: caller sent no tools, so no reference can be resolved — "
                "storing only, sending full content", ctx.request_id,
            )

        new_messages, tokens_before, tokens_after = await _process_messages(
            ctx.messages, min_tokens, ctx.routed_model, ttl, compress_system,
            prefix=prefix, may_substitute=may_substitute,
        )

        if tokens_after < tokens_before:
            ctx.messages = new_messages
            # The response side needs to know a reference actually went out this turn, so it
            # can tell "the model answered without resolving" from "there was nothing to
            # resolve". Only substitution counts — storing alone changes nothing the model sees.
            ctx.ccr_refs_substituted = True
            ctx.savings.add_step(
                GROUP,
                f"G28 CCR: {tokens_before}t → {tokens_after}t (refs substituted)",
                tokens_before,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G28-ccr",
                span_input={"tokens_before": tokens_before},
                output={"tokens_after": tokens_after},
                metadata={"pct_saved": round((1 - tokens_after / tokens_before) * 100, 1)},
            )
            logger.debug(
                "[%s] G28 CCR: %dt → %dt",
                ctx.request_id, tokens_before, tokens_after,
            )

        # Inject MCP tools when enabled so the LLM can call retrieve/compress.
        #
        # Only for callers that ALREADY send tools. Injecting three tool definitions into
        # every request would (a) change the tool block, which is part of the cached prefix,
        # churning the very prefix G21 stabilises; (b) add tokens to requests that can never
        # use them; and (c) hand tools to a pass-through caller that sent none, so the model
        # may emit tool_calls it never asked for. "Already sends tools" is the same
        # is-this-a-stateful-agent test the resolve handshake uses.
        existing_tools = ctx.params.get("tools") or []
        if cfg.get("expose_mcp_tools", True) and existing_tools:
            ccr_tools = _build_mcp_tools()
            ccr_names = {t["function"]["name"] for t in ccr_tools}
            merged = [t for t in existing_tools if t.get("function", {}).get("name") not in ccr_names]
            merged.extend(ccr_tools)
            ctx.params["tools"] = merged
            # The dispatch sites refuse to auto-execute a CCR tool unless this is
            # set. It is the difference between "the model called a tool we offered"
            # and "the model named one we never mentioned" — only the first is a
            # legitimate reason for the proxy to act.
            ctx.ccr_tools_injected = True

        return ctx

    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Dispatch any CCR tool calls the model made during this turn."""
        cfg = _resolve_g28_cfg(ctx)
        if not _ccr_enabled(cfg, ctx):
            return response

        ttl: int = cfg.get("ttl_seconds", 86400)

        choices = response.get("choices", [])
        answered_without_tools = bool(choices)
        resolved_this_turn = False
        for choice in choices:
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                # Still mid-loop: the model may retrieve on a later turn, so do not judge it.
                answered_without_tools = False
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                if tool_name not in _CCR_MCP_TOOLS:
                    continue
                # Same authorization as G15's dispatch site. Both loops execute the
                # same sink, so both must gate identically or the weaker one becomes
                # the way in.
                reason = authorize_dispatch(ctx, tool_name)
                if reason:
                    record_dispatch_block(ctx, tool_name, reason)
                    continue
                try:
                    arguments = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                result = await dispatch_mcp_tool(
                    tool_name, arguments, ttl,
                    prefix=getattr(ctx, "redis_prefix", ""),
                    max_store_chars=int(cfg.get("max_store_chars", 200_000)),
                )
                tc["function"]["result"] = result
                if tool_name == "headroom_retrieve" and "text" in (result or {}):
                    resolved_this_turn = True
                logger.debug("[%s] G28 MCP tool %s → %r", ctx.request_id, tool_name, result)

        # Self-healing handshake. A reference went out and the model produced a FINAL answer
        # without ever reading it — so that answer came from the summary, not the document,
        # and the savings we just recorded bought a worse answer. Stop trusting this client:
        # the next turn sends full content again, and it can re-earn substitution by actually
        # resolving. Never inferred from a mid-loop turn (tool_calls present) — only from a
        # completed answer.
        if getattr(ctx, "ccr_refs_substituted", False) and answered_without_tools and not resolved_this_turn:
            prefix = getattr(ctx, "redis_prefix", "")
            _revoke_resolver_proof(prefix)
            _record_ignored_reference(prefix)
            logger.warning(
                "[%s] G28 CCR: reference substituted but the model answered without resolving it "
                "— reverting this tenant to full content until it resolves one again",
                ctx.request_id,
            )

        return response
