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
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
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

# ─── In-process fallback store (used when Redis unavailable) ─────────────────

_local_store: Dict[str, str] = {}
_stats = {"hits": 0, "misses": 0}

# Reference token format: [CCR:hex8]
_REF_PREFIX = "[CCR:"
_REF_SUFFIX = "]"


def _make_ref(sha: str) -> str:
    return f"{_REF_PREFIX}{sha[:8]}{_REF_SUFFIX}"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ─── Storage helpers ─────────────────────────────────────────────────────────

def _store(redis_client: Optional[Any], key: str, text: str, ttl: int) -> None:
    if redis_client is not None:
        try:
            redis_client.setex(f"ccr:{key}", ttl, text)
            return
        except Exception as exc:
            logger.debug("G28 Redis store failed: %s — using local fallback", exc)
    _local_store[key] = text


def _retrieve_stored(redis_client: Optional[Any], key: str) -> Optional[str]:
    if redis_client is not None:
        try:
            val = redis_client.get(f"ccr:{key}")
            if val is not None:
                _stats["hits"] += 1
                return val.decode() if isinstance(val, bytes) else val
        except Exception as exc:
            logger.debug("G28 Redis retrieve failed: %s — falling back to local", exc)
    val = _local_store.get(key)
    if val is not None:
        _stats["hits"] += 1
    else:
        _stats["misses"] += 1
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

def _replace_content(
    content: str,
    redis_client: Optional[Any],
    min_tokens: int,
    model: str,
    ttl: int,
) -> Tuple[str, bool]:
    """Return (possibly_replaced_content, was_replaced)."""
    if estimate_tokens(content, model) < min_tokens:
        return content, False

    sha = _sha256_hex(content)
    ref = _make_ref(sha)

    # Check if it's already stored (repeat content → only one storage round-trip)
    existing = _retrieve_stored(redis_client, sha)
    if existing is None:
        _store(redis_client, sha, content, ttl)

    return ref, True


def _process_messages(
    messages: List[Dict],
    redis_client: Optional[Any],
    min_tokens: int,
    model: str,
    ttl: int,
    compress_system: bool = False,
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
            new_content, replaced = _replace_content(content, redis_client, min_tokens, model, ttl)
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

def dispatch_mcp_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    redis_client: Optional[Any] = None,
    ttl: int = 86400,
) -> Any:
    """Dispatch a CCR MCP tool call; return the tool result as a JSON-serialisable value."""
    if tool_name == "headroom_compress":
        text = arguments.get("text", "")
        call_ttl = arguments.get("ttl", ttl)
        if not text:
            return {"error": "text is required"}
        sha = _sha256_hex(text)
        ref = _make_ref(sha)
        _store(redis_client, sha, text, call_ttl)
        return {"ref": ref, "sha256": sha, "original_len": len(text)}

    if tool_name == "headroom_retrieve":
        ref = arguments.get("ref", "")
        if not ref.startswith(_REF_PREFIX):
            return {"error": f"Invalid CCR reference: {ref!r}"}
        sha_prefix = ref[len(_REF_PREFIX):-len(_REF_SUFFIX)]
        # Find matching key (local store linear search; Redis uses prefix scan)
        if redis_client is not None:
            try:
                val = redis_client.get(f"ccr:{sha_prefix}")
                if val is None:
                    # Try full SHA scan — sha_prefix is only 8 chars
                    for key in _local_store:
                        if key.startswith(sha_prefix):
                            return {"text": _local_store[key]}
                    return {"error": "Reference not found"}
                _stats["hits"] += 1
                return {"text": val.decode() if isinstance(val, bytes) else val}
            except Exception:
                pass
        for key in _local_store:
            if key.startswith(sha_prefix):
                _stats["hits"] += 1
                return {"text": _local_store[key]}
        _stats["misses"] += 1
        return {"error": "Reference not found"}

    if tool_name == "headroom_stats":
        return {
            "local_store_size": len(_local_store),
            "hits": _stats["hits"],
            "misses": _stats["misses"],
        }

    return {"error": f"Unknown CCR tool: {tool_name}"}


# ─── Per-tenant config resolution ─────────────────────────────────────────────

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _resolve_g28_cfg(ctx: RequestContext) -> Dict[str, Any]:
    """G28 config with the per-tenant override deep-merged in (tenant wins).

    Lets a tenant flip a single key (e.g. ``enabled`` for a cooperating agent client,
    or ``compress_system_prompt``) under ``tenants.<id>.groups.G28_ccr`` without
    re-declaring the whole block. Mirrors G21's ``_resolve_g21_cfg``.
    """
    base = ctx.config.get("groups", {}).get("G28_ccr", {})
    tenant_cfg = (
        ctx.config.get("tenants", {})
        .get(ctx.tenant_id, {})
        .get("groups", {})
        .get("G28_ccr", {})
    )
    if not tenant_cfg:
        return base
    return _deep_merge(base, tenant_cfg)


# ─── G28 Middleware ───────────────────────────────────────────────────────────

class G28CCR:
    """
    Contextual Content Reuse — replace repeated verbatim blocks with compact refs.
    Reference: G28 in token_optimization_playbook_v7.md
    """

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = _resolve_g28_cfg(ctx)
        if not cfg.get("enabled", False):
            return ctx

        min_tokens: int = cfg.get("min_tokens", 300)
        ttl: int = cfg.get("ttl_seconds", 86400)
        # Off by default: never replace the system instruction in pass-through, where
        # the model can't resolve a CCR reference (no agent loop). Opt in only for
        # clients that run the retrieve loop.
        compress_system: bool = cfg.get("compress_system_prompt", False)
        redis_client = getattr(ctx, "redis_client", None)

        new_messages, tokens_before, tokens_after = _process_messages(
            ctx.messages, redis_client, min_tokens, ctx.routed_model, ttl, compress_system
        )

        if tokens_after < tokens_before:
            ctx.messages = new_messages
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

        # Inject MCP tools when enabled so the LLM can call retrieve/compress
        if cfg.get("expose_mcp_tools", True):
            existing_tools = ctx.params.get("tools") or []
            ccr_tools = _build_mcp_tools()
            ccr_names = {t["function"]["name"] for t in ccr_tools}
            merged = [t for t in existing_tools if t.get("function", {}).get("name") not in ccr_names]
            merged.extend(ccr_tools)
            ctx.params["tools"] = merged

        return ctx

    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Dispatch any CCR tool calls the model made during this turn."""
        cfg = _resolve_g28_cfg(ctx)
        if not cfg.get("enabled", False):
            return response

        redis_client = getattr(ctx, "redis_client", None)
        ttl: int = cfg.get("ttl_seconds", 86400)

        choices = response.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                if tool_name not in ("headroom_compress", "headroom_retrieve", "headroom_stats"):
                    continue
                try:
                    arguments = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                result = dispatch_mcp_tool(tool_name, arguments, redis_client, ttl)
                tc["function"]["result"] = result
                logger.debug("[%s] G28 MCP tool %s → %r", ctx.request_id, tool_name, result)

        return response
