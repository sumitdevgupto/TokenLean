"""
G26 · Budget-Aware Context Management (BACM-style)
Stage: Into the LLM (history compaction)
Saving: 20–60% input tokens on long multi-turn conversations

Technique:
  Treat the model's context window as a budget that is *assessed before* the call
  is made. When the assembled prompt (messages + tool definitions) crosses
  ``compact_at_pct`` of the USABLE window — the context window minus the output
  reservation — compact the older span of the conversation back down toward
  ``target_pct`` using the cheapest ladder rung that gets there:

    1. prune     — drop byte-exact duplicate turns, truncate stale tool results
    2. compress  — deterministic prose compression (shared prose_compress engine)
    3. summarize — one cheap-model summary replacing the whole old span (cached)
    4. drop      — opt-in, default OFF: drop oldest turns as a hard-fit guarantee

Each rung is independently toggleable. The most recent ``keep_recent_turns``
turns and every ``system`` message are never touched, and every cut is snapped
to a tool-safe boundary (``history_utils.safe_window_split``) so a
``tool_call_id`` is never orphaned from its declaring assistant turn.

Reference: G26 in token_optimization_playbook_v7.md
"""
import copy
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing
from middleware.history_utils import safe_window_split, summarise_turns
from middleware.prose_compress import compress_text
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G26"

_DEFAULT_CONTEXT_WINDOW = 128000
_DEFAULT_RESERVE_OUTPUT_TOKENS = 1024
_SUMMARY_MARKER = "[Conversation summary — earlier turns]"
# Repeats shorter than this are real conversation ("yes", "ok", "continue"), not boilerplate.
_DEFAULT_PRUNE_MIN_CHARS = 200
# A "turn" is a user+assistant exchange, matching G10's `window * 2` convention.
_MESSAGES_PER_TURN = 2
_SUMMARY_UNAVAILABLE = "[summary unavailable]"

# In-process fallback for the summary cache when Redis is unavailable. Bounded —
# an unbounded module-level dict in a long-lived proxy process is a slow leak.
_LOCAL_SUMMARY_CACHE: Dict[str, Tuple[float, str]] = {}
_LOCAL_SUMMARY_CACHE_MAX = 512

# Config-warnings are emitted once per (tenant, kind) so a misconfigured tenant
# does not flood the log on every request.
_WARNED: set = set()


# ─── Config helpers ───────────────────────────────────────────────────────────

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _resolve_g26_cfg(ctx: RequestContext) -> Dict[str, Any]:
    """G26 config with the per-tenant override deep-merged in (tenant wins).

    Lets a tenant tune ``compact_at_pct`` / rung toggles under
    ``tenants.<id>.groups.G26_context_budget`` without re-declaring the block.
    Mirrors G28's ``_resolve_g28_cfg``.
    """
    base = ctx.config.get("groups", {}).get("G26_context_budget", {})
    tenant_cfg = (
        ctx.config.get("tenants", {})
        .get(getattr(ctx, "tenant_id", "default"), {})
        .get("groups", {})
        .get("G26_context_budget", {})
    )
    if not tenant_cfg:
        return base
    return _deep_merge(base, tenant_cfg)


def _get_model_context_window(model: Optional[str], cfg: Dict[str, Any]) -> int:
    """Total context window for ``model`` from config: exact key → longest
    matching prefix → ``default_context_window``. Mirrors G11's
    ``_get_model_max_tokens`` lookup so operators configure both the same way.
    """
    default = int(cfg.get("default_context_window", _DEFAULT_CONTEXT_WINDOW) or _DEFAULT_CONTEXT_WINDOW)
    if not model:
        return default
    windows = cfg.get("model_context_window", {}) or {}
    if model in windows:
        return int(windows[model])
    # Longest prefix wins so "claude-3-5-sonnet" beats a bare "claude-" entry.
    best: Optional[str] = None
    for prefix in windows:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    if best is not None:
        return int(windows[best])
    return default


def _usable_window(ctx: RequestContext, cfg: Dict[str, Any]) -> int:
    """Window actually available to the PROMPT.

    Providers reject a call when ``prompt + max_tokens > context_window``, so the
    honest budget subtracts the output reservation — the caller's own
    ``max_tokens`` when set, else the configured ``reserve_output_tokens``.
    """
    total = _get_model_context_window(getattr(ctx, "routed_model", None) or ctx.model, cfg)
    return max(0, total - _output_reservation(ctx, cfg))


def _output_reservation(ctx: RequestContext, cfg: Dict[str, Any]) -> int:
    """Tokens to hold back for the answer.

    The caller's own ``max_tokens`` wins when set. When it is NOT set, the configured
    ``reserve_output_tokens`` is only a floor: G11 runs later (Stage 4) and will inject a
    ``max_tokens`` of its own, and reserving less than G11 is about to request re-creates
    the exact ``prompt + max_tokens > context_window`` rejection G26 exists to prevent. So
    when G11 is enabled we reserve whatever it will ask for, taking the larger of the two —
    over-reserving only means compacting slightly earlier, which is the safe direction.
    """
    caller = ctx.params.get("max_tokens") if isinstance(ctx.params, dict) else None
    if isinstance(caller, int) and caller > 0:
        return caller

    reserve = int(cfg.get("reserve_output_tokens", _DEFAULT_RESERVE_OUTPUT_TOKENS) or 0)
    try:
        g11 = (ctx.config.get("groups", {}) or {}).get("G11_output", {}) or {}
        if g11.get("enabled", False):
            from middleware.g11_output_format import _get_model_max_tokens
            g11_reserve = int(_get_model_max_tokens(
                getattr(ctx, "routed_model", None) or ctx.model, g11))
            reserve = max(reserve, g11_reserve)
    except Exception as exc:  # never let the lookup break budgeting
        logger.debug("G26: could not resolve G11's output reservation: %s", exc)
    return max(0, reserve)


def _resolve_thresholds(ctx: RequestContext, cfg: Dict[str, Any]) -> Tuple[float, float]:
    """(compact_at_pct, target_pct), clamped so target is always below trigger.

    A tenant that sets ``target_pct >= compact_at_pct`` would otherwise compact on
    every single request without ever satisfying its own goal; clamp instead of
    failing so the request still succeeds, and warn once.
    """
    compact_at = float(cfg.get("compact_at_pct", 85) or 85)
    target = float(cfg.get("target_pct", 60) or 60)
    compact_at = min(99.0, max(10.0, compact_at))
    if target >= compact_at:
        clamped = max(1.0, compact_at - 10.0)
        key = (getattr(ctx, "tenant_id", "default"), "target_pct")
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning(
                "G26: target_pct (%s) >= compact_at_pct (%s) for tenant %s — clamping target to %s",
                target, compact_at, key[0], clamped,
            )
        target = clamped
    return compact_at, max(1.0, target)


# ─── Summary cache ────────────────────────────────────────────────────────────

def _span_fingerprint(span: List[Dict[str, Any]]) -> str:
    """Stable fingerprint of a conversation span (canonical JSON → sha256)."""
    try:
        canonical = json.dumps(span, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canonical = repr(span)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:40]


def _summary_cache_key(ctx: RequestContext, summary_model: str, fingerprint: str) -> str:
    """Tenant-scoped cache key — never a bare key (multi-tenant isolation)."""
    return f"{getattr(ctx, 'redis_prefix', '')}g26:summary:{summary_model}:{fingerprint}"


def _local_cache_get(key: str) -> Optional[str]:
    entry = _LOCAL_SUMMARY_CACHE.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if expires_at <= time.time():
        _LOCAL_SUMMARY_CACHE.pop(key, None)
        return None
    return value


def _local_cache_put(key: str, value: str, ttl: int) -> None:
    now = time.time()
    # Purge expired entries first, then evict the soonest-to-expire if still full.
    for k in [k for k, (exp, _) in _LOCAL_SUMMARY_CACHE.items() if exp <= now]:
        _LOCAL_SUMMARY_CACHE.pop(k, None)
    while len(_LOCAL_SUMMARY_CACHE) >= _LOCAL_SUMMARY_CACHE_MAX:
        oldest = min(_LOCAL_SUMMARY_CACHE, key=lambda k: _LOCAL_SUMMARY_CACHE[k][0])
        _LOCAL_SUMMARY_CACHE.pop(oldest, None)
    _LOCAL_SUMMARY_CACHE[key] = (now + max(1, ttl), value)


async def _cached_summary_get(key: str) -> Optional[str]:
    try:
        from cache.redis_pool import get_redis
        raw = await get_redis().get(key)
        if raw:
            return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as exc:
        logger.debug("G26 summary cache read fell back to local store: %s", exc)
        return _local_cache_get(key)
    return _local_cache_get(key)


async def _summary_for_span(
    ctx: RequestContext, cfg: Dict[str, Any], span: List[Dict[str, Any]],
    summary_model: str, ttl: int,
) -> Tuple[str, bool]:
    """Summary for ``span``, reusing the newest cached summary of a PREFIX of it.

    Keying only on the whole span makes the cache useless in the one situation it exists
    for: a live conversation. The span grows by an exchange every turn, so the exact key is
    always new and every over-budget request pays a fresh synchronous summariser call.

    Instead, look back a bounded number of messages for a cached summary of an earlier
    prefix. On a hit, only the messages added since then are summarised — with the previous
    summary supplied as leading context, so nothing from the earlier part is lost. Returns
    ``(summary, cache_hit)``, where ``cache_hit`` means no summariser call was needed at all.
    """
    def _key(n: int) -> str:
        return _summary_cache_key(ctx, summary_model, _span_fingerprint(span[:n]))

    exact = await _cached_summary_get(_key(len(span)))
    if exact:
        return exact, True

    base_summary, base_n = None, 0
    lookback = max(0, int(cfg.get("summary_prefix_lookback", 12) or 0))
    for n in range(len(span) - 1, max(0, len(span) - lookback) - 1, -1):
        if n <= 0:
            break
        found = await _cached_summary_get(_key(n))
        if found:
            base_summary, base_n = found, n
            break

    max_turns = int(cfg.get("summary_max_turns", 80) or 80)
    max_tokens = int(cfg.get("summary_max_tokens", 400) or 400)
    max_input = int(cfg.get("summary_max_input_tokens", 24000) or 0)

    if base_summary:
        to_summarise = ([{"role": "system", "content": f"{_SUMMARY_MARKER}\n{base_summary}"}]
                        + span[base_n:])
        logger.debug("[%s] G26 reusing cached summary of the first %d/%d span messages",
                     ctx.request_id, base_n, len(span))
    else:
        to_summarise = span

    summary = await summarise_turns(
        to_summarise, summary_model, ctx,
        max_turns=max_turns, max_tokens=max_tokens, max_input_tokens=max_input,
    )
    if summary and summary != _SUMMARY_UNAVAILABLE:
        await _cached_summary_put(_key(len(span)), summary, ttl)
    return summary, False


async def _cached_summary_put(key: str, value: str, ttl: int) -> None:
    try:
        from cache.redis_pool import get_redis
        await get_redis().set(key, value, ex=max(1, ttl))
        return
    except Exception as exc:
        logger.debug("G26 summary cache write fell back to local store: %s", exc)
    _local_cache_put(key, value, ttl)


# ─── Token accounting ─────────────────────────────────────────────────────────

def _msg_tokens(msg: Dict[str, Any], model: str) -> int:
    """Token cost of one message. ``count_messages_tokens`` is additive per
    message, so per-message counts sum exactly to the whole-list count — which is
    what lets the ladder track its progress incrementally instead of recounting
    the entire prompt after every rung.
    """
    return count_messages_tokens([msg], model)


def _span_tokens(span: List[Dict[str, Any]], model: str) -> int:
    return count_messages_tokens(span, model) if span else 0


# ─── Ladder rungs ─────────────────────────────────────────────────────────────

def _is_dedupable(msg: Dict[str, Any], min_chars: int) -> bool:
    """Whether a repeated message is safe to drop.

    Short conversational turns — "yes", "ok", "continue" — repeat legitimately all through a
    long thread and each one has its own distinct reply after it. Collapsing them is not
    deduplication, it is deleting real conversation. Only repeats long enough to be genuine
    boilerplate (a re-pasted document, a re-sent instruction block) are worth removing, and
    they are where the tokens actually are.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return len(content) >= min_chars
    if isinstance(content, list):  # multimodal — size it by its text parts
        return sum(len(str(p.get("text", "")))
                   for p in content if isinstance(p, dict) and p.get("type") == "text") >= min_chars
    return False


def _rung_prune(span: List[Dict[str, Any]], max_tool_chars: int,
                min_chars: int = _DEFAULT_PRUNE_MIN_CHARS) -> Tuple[List[Dict[str, Any]], bool]:
    """Rung 1 — byte-exact duplicate removal + stale tool-result truncation.

    Deliberately byte-exact (not semantic like G22): this is the free rung, and a
    message carrying ``tool_calls``/``tool_call_id`` is never dropped even when
    duplicated, because removing one half of a tool pair 400s the provider.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    changed = False
    for i, msg in enumerate(span):
        pairs_tool = bool(msg.get("tool_calls")) or bool(msg.get("tool_call_id"))
        if not pairs_tool and _is_dedupable(msg, min_chars):
            try:
                sig = json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str)
            except Exception:
                sig = repr(msg)
            if sig in seen:
                # Dropping this message must not weld its neighbours together into two
                # consecutive same-role messages — that rewrites the shape of the
                # conversation (and strands the reply that answered THIS message).
                nxt = span[i + 1] if i + 1 < len(span) else None
                welds = bool(out and nxt and out[-1].get("role") == nxt.get("role"))
                if not welds:
                    changed = True
                    continue
            seen.add(sig)
        if (
            msg.get("role") == "tool"
            and isinstance(msg.get("content"), str)
            and max_tool_chars > 0
            and len(msg["content"]) > max_tool_chars
        ):
            msg = dict(msg)
            msg["content"] = msg["content"][:max_tool_chars] + "\n…[truncated]"
            changed = True
        out.append(msg)
    return out, changed


def _compress_content(content: Any) -> Tuple[Any, bool]:
    """Deterministic prose compression of a message's content.

    Only ``{"type": "text"}` parts of multimodal content are touched — image /
    audio parts pass through byte-identical. A compressed result is kept only when
    it is genuinely shorter, so the rung can never inflate.
    """
    if isinstance(content, str):
        out = compress_text(content)
        if isinstance(out, str) and len(out) < len(content):
            return out, True
        return content, False
    if isinstance(content, list):
        parts: List[Any] = []
        changed = False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                out = compress_text(part["text"])
                if isinstance(out, str) and len(out) < len(part["text"]):
                    new_part = dict(part)
                    new_part["text"] = out
                    parts.append(new_part)
                    changed = True
                    continue
            parts.append(part)
        return (parts, True) if changed else (content, False)
    return content, False


def _looks_structured(text: str) -> bool:
    """Whether a string is machine-readable data rather than prose."""
    s = text.lstrip()
    if not s or s[0] not in "{[":
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _is_compressible(msg: Dict[str, Any]) -> bool:
    """Whether a message's content is prose the compressor may safely rewrite.

    Tool results are DATA, not prose. The compressor strips articles and filler, so a
    payload like ``{"region": "the north"}`` comes back as ``{"region": "north"}`` — a
    silently different value, in the one part of the conversation the model is meant to
    treat as ground truth. Tool results and any JSON-shaped content are left byte-exact.
    """
    if msg.get("role") == "tool" or msg.get("tool_call_id"):
        return False
    content = msg.get("content")
    if isinstance(content, str) and _looks_structured(content):
        return False
    return True


def _rung_compress(span: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Rung 2 — deterministic, idempotent, code/URL-safe prose compression."""
    out: List[Dict[str, Any]] = []
    changed = False
    for msg in span:
        if not _is_compressible(msg):
            out.append(msg)
            continue
        new_content, did = _compress_content(msg.get("content"))
        if did:
            msg = dict(msg)
            msg["content"] = new_content
            changed = True
        out.append(msg)
    return out, changed


def _rung_drop(
    span: List[Dict[str, Any]], model: str, current: int, target: int
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Rung 4 (opt-in) — drop oldest span messages until under target.

    Lossy hard-fit guarantee of last resort, only reached when rungs 1–3 are
    disabled or insufficient. After each drop, any newly-leading ``role:"tool"``
    messages are dropped too: their declaring assistant turn just left, so keeping
    them would orphan a ``tool_call_id``.
    """
    dropped = 0
    while span and current > target:
        current -= _msg_tokens(span.pop(0), model)
        dropped += 1
        while span and span[0].get("role") == "tool":
            current -= _msg_tokens(span.pop(0), model)
            dropped += 1
    return span, current, dropped


# ─── G26 Middleware ───────────────────────────────────────────────────────────

class G26ContextBudget:
    """
    Budget-aware context management — compact history before the window overflows.
    Reference: G26 in token_optimization_playbook_v7.md
    """

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = _resolve_g26_cfg(ctx)
        if not cfg.get("enabled", False):
            return ctx
        if ctx.bypassed or ctx.cache_hit:
            return ctx
        try:
            return await self._compact(ctx, cfg)
        except Exception as exc:
            # Never block a request over a compaction failure — the original
            # messages are untouched because every rung stages onto copies and
            # only the commit step assigns ctx.messages.
            logger.warning("[%s] G26 compaction failed, passing through: %s", ctx.request_id, exc)
            return ctx

    async def _compact(self, ctx: RequestContext, cfg: Dict[str, Any]) -> RequestContext:
        usable = _usable_window(ctx, cfg)
        if usable <= 0:
            return ctx

        compact_at_pct, target_pct = _resolve_thresholds(ctx, cfg)
        trigger_tokens = usable * compact_at_pct / 100.0
        target_tokens = usable * target_pct / 100.0

        used = ctx.current_request_token_count  # full count #1 (tools-inclusive)
        if used <= trigger_tokens:
            # Under budget — the common case. No mutation, no savings step
            # (fire-only recording, as G22/G28 do).
            return ctx

        model = ctx.model
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        turns = [m for m in ctx.messages if m.get("role") != "system"]

        # A "turn" is an exchange (user + assistant), not a single message — the same
        # meaning G10 gives it when it passes `window * 2` to the same helper. Passing the
        # raw number would silently hand operators half the recent context they configured.
        keep = max(1, int(cfg.get("keep_recent_turns", 6) or 6)) * _MESSAGES_PER_TURN
        cut = safe_window_split(turns, keep)
        if cut == 0:
            # No clean boundary (or everything is already inside the protected
            # tail) — trimming here would orphan a tool pair.
            return ctx

        original_span = turns[:cut]
        tail = turns[cut:]
        span: List[Dict[str, Any]] = copy.deepcopy(original_span)
        span_tokens = _span_tokens(span, model)
        # Everything G26 will not touch: system messages, the protected tail and
        # the tool definitions. Tracking it lets each rung update the running
        # total incrementally instead of recounting the whole prompt.
        fixed_tokens = used - span_tokens
        current = used

        rungs = cfg.get("rungs", {}) or {}
        applied: List[str] = []
        summary_msg: Optional[Dict[str, Any]] = None
        cache_hit = False

        if rungs.get("prune", True) and current > target_tokens:
            span, changed = _rung_prune(
                span,
                int(cfg.get("tool_result_max_chars", 4000) or 0),
                int(cfg.get("prune_min_chars", _DEFAULT_PRUNE_MIN_CHARS) or 0),
            )
            if changed:
                applied.append("prune")
                current = fixed_tokens + _span_tokens(span, model)

        if rungs.get("compress", True) and current > target_tokens:
            span, changed = _rung_compress(span)
            if changed:
                applied.append("compress")
                current = fixed_tokens + _span_tokens(span, model)

        if rungs.get("summarize", True) and current > target_tokens and span:
            summary_model = str(cfg.get("summary_model") or "gpt-4o-mini")
            ttl = int(cfg.get("summary_ttl_seconds", 3600) or 3600)
            # Fingerprint the ORIGINAL span, not the pruned/compressed one: the
            # same conversation prefix then hits the same cache entry on every
            # later turn regardless of which earlier rungs happened to fire.
            # Fingerprint the ORIGINAL span, not the pruned/compressed one, so the cache
            # key depends only on what the caller actually sent.
            summary, cache_hit = await _summary_for_span(
                ctx, cfg, original_span, summary_model, ttl)
            if summary and summary != _SUMMARY_UNAVAILABLE:
                candidate = {"role": "system", "content": f"{_SUMMARY_MARKER}\n{summary}"}
                # Only swap if the summary is genuinely smaller than what it replaces.
                # When the budget pressure comes from the tools or the protected tail, the
                # span can already be shorter than a summary of it — replacing it would
                # GROW the prompt, and the saving clamps at zero so nothing would show it.
                span_now = _span_tokens(span, model)
                summary_cost = _msg_tokens(candidate, model)
                if summary_cost < span_now:
                    summary_msg = candidate
                    span = []
                    applied.append("summarize")
                    current = fixed_tokens + summary_cost
                else:
                    logger.debug(
                        "[%s] G26 skipping summarize: summary (%d tok) is not smaller than "
                        "the span it would replace (%d tok)",
                        ctx.request_id, summary_cost, span_now)
            # Summariser failure keeps the rung-2 output and falls through.

        if rungs.get("drop", False) and current > target_tokens and span:
            span, current, dropped = _rung_drop(span, model, current, target_tokens)
            if dropped:
                applied.append("drop")

        if not applied:
            return ctx

        ctx.messages = (
            system_msgs
            + ([summary_msg] if summary_msg else [])
            + span
            + tail
        )
        after = ctx.current_request_token_count  # full count #2
        pct_before = (used / usable * 100.0) if usable else 0.0
        pct_after = (after / usable * 100.0) if usable else 0.0

        ctx.savings.add_step(
            GROUP,
            (
                f"Context budget: {pct_before:.0f}% → {pct_after:.0f}% of usable window "
                f"({usable} tok); rungs={'+'.join(applied)}"
            ),
            used,
            after,
        )
        self._emit_metric(ctx, applied, cfg)
        langfuse_tracing.add_span(
            ctx,
            name="G26-context-budget",
            span_input={"tokens_before": used, "pct_of_window_before": round(pct_before, 1)},
            output={"tokens_after": after, "pct_of_window_after": round(pct_after, 1)},
            metadata={
                "usable_window": usable,
                "compact_at_pct": compact_at_pct,
                "target_pct": target_pct,
                "rungs_applied": applied,
                "summary_cache_hit": cache_hit,
                "span_messages": len(original_span),
            },
        )
        logger.debug(
            "[%s] G26 compacted %d → %d tokens (%.0f%% → %.0f%% of %d) via %s",
            ctx.request_id, used, after, pct_before, pct_after, usable, applied,
        )
        return ctx

    @staticmethod
    def _emit_metric(ctx: RequestContext, applied: List[str], cfg: Dict[str, Any]) -> None:
        if not cfg.get("metrics_enabled", True):
            return
        try:
            from middleware.g18_observability import CONTEXT_BUDGET_COMPACTIONS_TOTAL
            for rung in applied:
                CONTEXT_BUDGET_COMPACTIONS_TOTAL.labels(
                    tenant_id=getattr(ctx, "tenant_id", "default"), rung=rung
                ).inc()
        except Exception as exc:  # never let metrics break the request
            logger.debug("[%s] G26 metric emit failed: %s", ctx.request_id, exc)
