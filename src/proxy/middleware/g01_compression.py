"""
G01 · Prompt & System Prompt Design
Stage: Before the Request
Saving: 15–40% input tokens
Technique: 
  1. Layered composition: base → role → task → dynamic layers
  2. Build-time compression of static system prompts
  3. LLMLingua-2 runtime compression for large messages
  4. Selective Context integration for relevance-based pruning
"""
import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from middleware import RequestContext
from middleware import cache_floor
from middleware import langfuse_tracing
from middleware.prose_compress import compress_text as _prose_compress_text
from middleware.prose_compress import protected_segments as _protected_segments
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G01"

# Selective Context configuration (optional - falls back gracefully)
_selective_context_available = False
try:
    from selective_context import SelectiveContext
    _selective_context_available = True
except ImportError:
    pass

# Kompress-v2-base fallback for log/error content (optional — no sidecar needed)
# Loaded lazily on first use; None when transformers is not installed.
_kompress_pipe: Optional[Any] = None
_kompress_pipe_model: Optional[str] = None  # model name of the loaded pipeline
_kompress_loaded: bool = False               # True once load was attempted (even if failed)

import re as _re


class LayeredPromptComposer:
    """Compose system prompts from layered components (base → role → task → dynamic).

    The base + role layers are static across requests for a given
    role/persona, so they are deduplicated and cached at first use
    (build-time compression); task + dynamic layers are request-specific
    and always recomposed fresh.
    """

    def __init__(self, config: Dict[str, Any]):
        self.layers = config.get("layers", {})
        self.build_time_compression = config.get("build_time_compression", True)
        self._compressed_cache: Dict[str, str] = {}

    def get_layer(self, layer_name: str, **variables) -> str:
        """Get a prompt layer with variable substitution."""
        template = self.layers.get(layer_name, "")
        if not template:
            return ""

        # Simple variable substitution
        result = template
        for key, value in variables.items():
            placeholder = f"{{{key}}}"
            result = result.replace(placeholder, str(value))

        return result

    def compose(self, context: Dict[str, Any]) -> str:
        """Compose full prompt from all layers in order."""
        parts = []

        base_content = self.get_layer("base", **context)
        role_content = self.get_layer("role", **context)
        if self.build_time_compression and (base_content or role_content):
            static_key = self._static_key(base_content, role_content)
            static_combined = self._compressed_cache.get(static_key)
            if static_combined is None:
                static_combined = self._compress_static_layers(base_content, role_content)
                self._compressed_cache[static_key] = static_combined
            if static_combined:
                parts.append(static_combined)
        else:
            if base_content:
                parts.append(base_content)
            if role_content:
                parts.append(role_content)

        for layer_name in ("task", "dynamic"):
            layer_content = self.get_layer(layer_name, **context)
            if layer_content:
                parts.append(layer_content)

        return "\n\n".join(parts)

    @staticmethod
    def _static_key(base: str, role: str) -> str:
        """Cache key for a static base+role combination."""
        combined = f"{base}:{role}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @staticmethod
    def _compress_static_layers(base: str, role: str) -> str:
        """Dedupe role-layer lines that already appear in the base layer."""
        base_stripped = base.strip()
        role_stripped = role.strip()

        base_lines = {line.strip().lower() for line in base_stripped.split("\n") if line.strip()}
        unique_role_lines = [
            line for line in role_stripped.split("\n")
            if line.strip() and line.strip().lower() not in base_lines
        ]
        compressed_role = "\n".join(unique_role_lines)

        return f"{base_stripped}\n\n{compressed_role}".strip()

    def get_build_time_compressed(self, layer_name: str) -> Optional[str]:
        """Get pre-compressed layer content (computed at build/deploy time)."""
        return self._compressed_cache.get(layer_name)

    def set_build_time_compressed(self, layer_name: str, content: str) -> None:
        """Store build-time compressed content."""
        self._compressed_cache[layer_name] = content


class SelectiveContextPruner:
    """Selective Context integration for relevance-based context pruning."""
    
    def __init__(self, max_tokens: int = 4000):
        self.max_tokens = max_tokens
        self._sc = None
        
        if _selective_context_available:
            try:
                self._sc = SelectiveContext()
            except Exception as exc:
                logger.debug("SelectiveContext init failed: %s", exc)
    
    def prune_context(self, text: str, query: Optional[str] = None) -> Tuple[str, float]:
        """Prune context to most relevant sentences.
        
        Returns: (pruned_text, reduction_ratio)
        """
        if not self._sc or not text:
            return text, 1.0
        
        try:
            # Use Selective Context to prune irrelevant content
            result = self._sc(text, self.max_tokens)
            pruned = result if isinstance(result, str) else result[0]
            
            original_tokens = len(text.split())  # Rough estimate
            pruned_tokens = len(pruned.split())
            reduction = pruned_tokens / original_tokens if original_tokens > 0 else 1.0
            
            return pruned, reduction
        except Exception as exc:
            logger.debug("SelectiveContext pruning failed: %s", exc)
            return text, 1.0


# ─── Kompress-v2-base fallback ────────────────────────────────────────────────

# ── Faithfulness boundary for every compressed message (E26, 2026-09-06) ──────
# DS9 shipped a wrong answer to a customer through this module. The assistant message
# said:
#
#     "employees may carry over up to 5 unused PTO days into the following calendar
#      year. Any PTO EXCEEDING THIS LIMIT is forfeited on January 1st."
#
# LLMLingua-2 at ratio 0.5 returned:
#
#     "Section 4. 2 HR Policy Manual : 5 PTO days. PTO FORFEITED January 1st."
#
# Deleting "exceeding this limit" does not lose a detail — it INVERTS the rule, turning a
# carry-over allowance into a blanket forfeiture. The model then reported that inversion
# faithfully: "unused PTO days are forfeited... you are not permitted to carry over any."
#
# What makes this worth a permanent guard rather than a tuning change: G01's output was
# BYTE-IDENTICAL in the run where DS9 passed (2026-08-07) and the one where it failed
# (2026-09-06) — 207t → 181t, saving 26, both times. The compressor did not get worse. The
# model simply stopped reconstructing the fact we had destroyed. So eleven consecutive
# PASSes never showed the compression was safe; they showed the model was repairing it, and
# that repair is not something we can keep buying. `force_reserve_digit` already protects
# the NUMBER ("5"); nothing protected the words that bound it.
#
# Deliberately a property of the OUTPUT, not a blocklist against one library — the same
# reasoning as `_is_faithful_compaction` in g19_headroom.py, and for the same reason: the
# last time a compressor's behaviour was argued from "it does not do that", the conclusion
# was wrong.
_NEGATIONS = frozenset({
    "no", "not", "never", "none", "cannot", "cant", "dont", "doesnt", "didnt", "wont",
    "shouldnt", "wouldnt", "couldnt", "isnt", "arent", "wasnt", "werent", "without",
    "nor", "neither", "nothing", "nobody", "unable", "excluded", "prohibited",
})
# Words that BOUND a claim. Delete one and a limited rule becomes an unlimited one.
_SCOPE_LIMITERS = frozenset({
    "except", "unless", "exceeding", "exceeds", "exceed", "excess", "only", "limit",
    "limited", "maximum", "minimum", "max", "min", "cap", "capped", "subject",
    "provided", "eligible", "ineligible", "required", "optional", "up", "least", "most",
    "before", "after", "unused", "remaining",
})
# NOT included, deliberately: "per". It reads as a scope word but in practice appears as a
# CITATION ("Per Section 4.2 of the HR Policy Manual"), so including it refused compressions
# that had preserved every actual bound — a false positive found by this guard's own tests.
_MEANING_CRITICAL = _NEGATIONS | _SCOPE_LIMITERS
_WORD_RE = _re.compile(r"[a-z0-9']+")


def _is_faithful_compression(before: str, after: str) -> bool:
    """Refuse a compression that drops a negation or a scope-limiting qualifier, or that
    changes code, a URL, a path, an identifier or a version number.

    The first refusal was learned from one defect (E26): if a meaning-critical word appears
    in the source and not in the output, the output may assert something the source did not.
    The second: LLMLingua-2 drops word tokens and Kompress rewrites text, and neither can
    tell `fetch_user` or `/v2/users/` from filler, so the model would answer about, or edit,
    code it never wrote. We cannot tell "harmlessly terse" from "inverted" without reading
    it, so the compression is declined and the ORIGINAL is sent — the same fail-safe
    direction as every other guard in this codebase: a request that costs more is
    recoverable, a wrong answer is not.

    Deterministic, provider-agnostic and linear in the message length, so it runs on every
    compressed message.
    """
    return _unfaithful_reason(before, after) is None


def _unfaithful_reason(before: str, after: str) -> Optional[str]:
    """Why compressing ``before`` into ``after`` must be refused, or None."""
    if not before or not after:
        return None if (after or not before) else "the whole message was dropped"
    src_words = set(_WORD_RE.findall(before.lower()))
    out_words = set(_WORD_RE.findall(after.lower()))
    if (src_words & _MEANING_CRITICAL) - out_words:
        return "a negation or scope qualifier was dropped"
    # Every protected segment must survive unchanged and in its original order (the
    # compressors only delete, so a moved segment was rewritten). One forward pass.
    pos = 0
    for segment in _protected_segments(before):
        found = after.find(segment, pos)
        if found < 0:
            return "code, a URL, a path or an identifier was changed"
        pos = found + len(segment)
    return None


# A fenced code block: ``` or ~~~ opening a line (up to any indentation).
_CODE_FENCE_RE = _re.compile(r"^[ \t]*(?:```|~~~)", _re.MULTILINE)


# These run on client-supplied history inside the event loop, so each must stay linear. The
# stack-frame indentation is spaces and tabs only: `^\s+` also matched newlines, so on a run
# of blank lines every line start rescanned the rest of the message (quadratic).
_LOG_ERROR_PATTERNS = [
    _re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", _re.MULTILINE),   # timestamps
    _re.compile(r"^\[?(INFO|DEBUG|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\]?", _re.MULTILINE),
    _re.compile(r"Traceback \(most recent call last\)", _re.MULTILINE),
    _re.compile(r"^[ \t]+at \w[\w.]+\([\w.]+:\d+\)", _re.MULTILINE),           # Java stack frames
    _re.compile(r'^\d{2}:\d{2}:\d{2}\.\d+ \[', _re.MULTILINE),                # structured logs
]


def _is_log_error_content(text: str) -> bool:
    """Return True if text looks like log output or an error/stack trace."""
    return any(p.search(text) for p in _LOG_ERROR_PATTERNS)


def _get_kompress_pipe(model: str) -> Optional[Any]:
    """Lazy-load Kompress-v2-base pipeline; cache across requests (singleton)."""
    global _kompress_pipe, _kompress_pipe_model, _kompress_loaded
    if _kompress_loaded and _kompress_pipe_model == model:
        return _kompress_pipe
    _kompress_loaded = True
    _kompress_pipe_model = model
    try:
        from transformers import pipeline  # type: ignore
        _kompress_pipe = pipeline("text2text-generation", model=model)
        logger.info("G01 Kompress pipeline loaded: %s", model)
    except Exception as exc:
        _kompress_pipe = None
        logger.debug("G01 Kompress pipeline unavailable (%s): %s", model, exc)
    return _kompress_pipe


def _kompress_compress(text: str, model: str, max_new_tokens: int = 256) -> Optional[str]:
    """Compress log/error text via Kompress-v2-base; returns None on failure."""
    pipe = _get_kompress_pipe(model)
    if pipe is None:
        return None
    try:
        result = pipe(text, max_new_tokens=max_new_tokens, truncation=True)
        if result and isinstance(result, list):
            compressed = result[0].get("generated_text", "")
            return compressed if compressed and len(compressed) < len(text) else None
    except Exception as exc:
        logger.debug("G01 Kompress compression failed: %s", exc)
    return None


class G01Compression:
    """G01 with layered composition, build-time compression, and Selective Context."""
    
    def __init__(self):
        self._composers: Dict[str, LayeredPromptComposer] = {}
        self._selective_pruner: Optional[SelectiveContextPruner] = None

    def _get_composer(self, cfg: Dict) -> LayeredPromptComposer:
        # One composer per layer set, so the layers follow the current config (a reload,
        # a per-tenant setting) rather than the first request's for the process lifetime.
        key = json.dumps([cfg.get("layers", {}), cfg.get("build_time_compression", True)],
                         sort_keys=True, default=str)
        composer = self._composers.get(key)
        if composer is None:
            composer = self._composers[key] = LayeredPromptComposer(cfg)
        return composer

    def _compose_layers(self, ctx: RequestContext, cfg: Dict) -> None:
        """Layered composition, for the system messages that ask for it.

        A system message opts in by carrying a ``layer_context`` object; its content is then
        replaced by the configured layers, filled from that object. Nothing else triggers
        it: it used to fire on any system prompt containing ``{{`` and ``}}`` (template
        syntax a prompt may simply mention) and replaced the developer's instructions with
        the generic layers. ``layer_context`` is TokenLean's field, not the provider's, so it
        is removed from every message here, composed or not."""
        if not any(isinstance(m, dict) and "layer_context" in m for m in ctx.messages):
            return
        enabled = cfg.get("layered_composition_enabled", False)
        tokens_before = count_messages_tokens(ctx.messages, ctx.model)
        composed_count = 0
        for i, msg in enumerate(ctx.messages):
            if not isinstance(msg, dict) or "layer_context" not in msg:
                continue
            layer_context = msg["layer_context"]
            msg = {k: v for k, v in msg.items() if k != "layer_context"}
            if enabled and msg.get("role") == "system" and isinstance(layer_context, dict):
                composed = self._get_composer(cfg).compose(layer_context)
                if composed:
                    msg["content"] = composed
                    composed_count += 1
            ctx.messages[i] = msg
        if composed_count:
            tokens_after = count_messages_tokens(ctx.messages, ctx.model)
            ctx.savings.add_step(
                GROUP, f"Layered system prompt composed ({composed_count} message(s))",
                tokens_before, tokens_after)
            logger.info("[%s] G01 composed %d layered system prompt(s): %d -> %d tokens",
                        ctx.request_id, composed_count, tokens_before, tokens_after)
    
    def _get_selective_pruner(self, cfg: Dict) -> Optional[SelectiveContextPruner]:
        if not cfg.get("selective_context_enabled", True):
            return None
        
        if self._selective_pruner is None:
            max_tokens = cfg.get("selective_context_max_tokens", 4000)
            self._selective_pruner = SelectiveContextPruner(max_tokens)
        return self._selective_pruner
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G1_compression", {})
        if not cfg.get("enabled", False):
            return ctx

        min_tokens: int = cfg.get("min_tokens_to_compress", 200)
        sidecar_url: str = cfg.get("sidecar_url", ctx.config.get("services", {}).get("llmlingua_url", "http://llmlingua-svc:8080/compress"))
        ratio: float = cfg.get("compression_ratio_target", 0.5)
        # Preserve digit tokens (dates/IDs/amounts) so compression can't silently
        # corrupt a tool argument (e.g. an incident date in a log-query window).
        force_reserve_digit: bool = cfg.get("force_reserve_digit", True)
        kompress_enabled: bool = cfg.get("kompress_enabled", True)
        kompress_model: str = cfg.get("kompress_model", "microsoft/Kompress-v2-base")
        kompress_max_new_tokens: int = cfg.get("kompress_max_new_tokens", 256)
        # Deterministic regex prose fallback (zero-LLM, zero-latency) — engages only
        # when neither LLMLingua nor Kompress reduced anything (e.g. sidecar down), so
        # a compression outage degrades to *some* savings instead of pass-through.
        deterministic_fallback: bool = cfg.get("deterministic_fallback", False)
        
        # Layered composition: opt-in per system message (layer_context), default off.
        use_layers = cfg.get("layered_composition_enabled", False)
        self._compose_layers(ctx, cfg)

        tokens_before = ctx.current_token_count
        if tokens_before < min_tokens:
            return ctx

        compressed_messages = []
        changed = False
        selective_pruner = self._get_selective_pruner(cfg)
        
        # Quality guard: by default DO NOT compress the system instruction — it is
        # the developer's contract and aggressive compression silently degrades
        # instruction-following. Compress only safe context (assistant history, and
        # user-pasted bulk if explicitly enabled). Opt in via compress_system_prompt.
        # Per-request opt-in (x_compress_user) lets a caller that knows its user content
        # is safe-to-compress bulk prose (a pasted transcript, a verbose write-up) enable
        # user-message compression without flipping the global default. The system prompt
        # stays protected unless compress_system_prompt is explicitly set in config.
        _x_compress_user = str(ctx.params.get("x_compress_user", "")).lower() in ("true", "1", "yes")
        compress_user_messages = cfg.get("compress_user_messages", False) or _x_compress_user
        compress_system_prompt = cfg.get("compress_system_prompt", False)
        roles = ["assistant"]
        if compress_system_prompt:
            roles.append("system")
        if compress_user_messages:
            roles.append("user")
        compressible_roles = tuple(roles)

        # Index of every message this pass actually rewrote, so the cacheable-prefix guard
        # below can accept or reject candidates INDIVIDUALLY instead of all-or-nothing.
        changed_indices: List[int] = []
        for _i, msg in enumerate(ctx.messages):
            role = msg.get("role", "")
            if role in compressible_roles:
                content = msg.get("content", "")
                min_chars = cfg.get("min_chars_to_compress", 100)
                reduction_threshold = cfg.get("reduction_threshold", 0.95)
                if isinstance(content, str) and len(content) > min_chars:
                    compressed = content
                    reduction_info = []
                    # The model-based steps drop or rewrite tokens with no notion of code, so
                    # a message holding a fenced code block skips them (no sidecar call). The
                    # deterministic fallback keeps code byte-for-byte and may still run.
                    model_steps = not _CODE_FENCE_RE.search(content)

                    # Step 1: Selective Context pruning (if enabled)
                    if selective_pruner and model_steps:
                        pruned, reduction = selective_pruner.prune_context(compressed)
                        if reduction < reduction_threshold:
                            compressed = pruned
                            reduction_info.append(f"SC:{reduction:.2f}")

                    # Step 2: LLMLingua-2 compression
                    llm_compressed = (
                        await _call_llmlingua(sidecar_url, compressed, ratio, force_reserve_digit)
                        if model_steps else None)
                    if llm_compressed and len(llm_compressed) < len(compressed):
                        compressed = llm_compressed
                        reduction_info.append("LLML2")
                    elif model_steps and kompress_enabled and _is_log_error_content(compressed):
                        # Step 2b: Kompress-v2-base fallback for log/error content when
                        # LLMLingua sidecar is unavailable or produced no reduction
                        k_compressed = _kompress_compress(compressed, kompress_model, kompress_max_new_tokens)
                        if k_compressed and len(k_compressed) < len(compressed):
                            compressed = k_compressed
                            reduction_info.append("KMP")

                    # Step 2c: deterministic prose fallback (regex, zero-LLM). Only when
                    # the model-based paths reduced nothing — protects code/paths/idents
                    # byte-for-byte via prose_compress; never touches an already-reduced msg.
                    if deterministic_fallback and not any(
                        t in reduction_info for t in ("LLML2", "KMP")
                    ):
                        det = _prose_compress_text(compressed)
                        if det and len(det) < len(compressed):
                            compressed = det
                            reduction_info.append("DET")

                    if compressed != content and not _is_faithful_compression(content, compressed):
                        # Guarded here rather than per-compressor so LLMLingua, Kompress and
                        # the deterministic fallback are all held to the same boundary, and
                        # so any future compressor is too (E26).
                        logger.warning(
                            "[%s] G01 refused an unfaithful compression of a %s message "
                            "(%d->%d chars): %s",
                            ctx.request_id, role, len(content), len(compressed),
                            _unfaithful_reason(content, compressed),
                        )
                        compressed = content

                    if compressed != content:
                        compressed_messages.append({**msg, "content": compressed})
                        changed_indices.append(_i)
                        changed = True
                        continue
            compressed_messages.append(msg)

        # ── Cacheable-prefix guard (backlog #41) — DEFAULT OFF ────────────────────
        # Compressing a prompt below the provider's minimum cacheable size destroys
        # provider prefix caching outright: the provider stops caching and says nothing
        # (no read, no write, no error), while G21 keeps injecting markers that can never
        # pay out. Measured on DS8 2026-09-04: the all-on stack sent 63% FEWER tokens and
        # paid 2.5x MORE for input than G21 alone, because a ~90% discount on ~51.7k
        # tokens was forfeited.
        #
        # Three things this must get right, each of which the first cut got wrong:
        #   * it is the provider's CACHEABLE SPAN that has a minimum, not the prompt —
        #     on a marker-based provider that is tools + system, and the rest of the
        #     prompt is billed the same either way;
        #   * the suffix is never cached, so its compression is free money and must be
        #     kept even when the span is held back;
        #   * whether holding tokens back is cheaper is ARITHMETIC over the provider's own
        #     cache rates and the observed reuse, not a threshold. On DS8 the right answer
        #     is opposite between providers, so a bare floor test costs one of them money.
        # All of that lives in `cache_floor`; G01's job is to offer it the cheapest
        # candidate it can produce. `ratio` is a keep-fraction, so aiming the span just
        # above the floor is one more sidecar call, not a search.
        floor_state = cache_floor.get(ctx)
        if changed and floor_state.active:
            span_before = cache_floor.span_tokens(ctx, ctx.messages)
            span_after = cache_floor.span_tokens_paired(
                ctx, ctx.messages, compressed_messages)
            if not cache_floor.allows_shrink(ctx, span_before, span_after, "G01",
                                             can_floor=True):
                in_span = [i for i in changed_indices if floor_state.covers(ctx.messages[i])]
                # Arm C — re-compress the in-span messages at a milder, floor-targeted
                # rate. Anything outside the span keeps its full compression regardless.
                # The rate is derived from the SHRINKABLE subset only: the rest of the
                # span (untouched messages, tool definitions) is fixed, so aiming the
                # whole-span ratio at the subset lands it well above the target and gives
                # back tokens for nothing.
                shrinkable = count_messages_tokens(
                    [ctx.messages[i] for i in in_span], ctx.model)
                rate = cache_floor.target_rate(
                    ctx, shrinkable, fixed_tokens=span_before - shrinkable)
                floored = list(compressed_messages)
                any_floored = False
                if rate is not None:
                    for i in in_span:
                        original = ctx.messages[i].get("content", "")
                        if not isinstance(original, str):
                            continue
                        milder = await _call_llmlingua(
                            sidecar_url, original, rate, force_reserve_digit)
                        if (milder and len(milder) < len(original)
                                and _is_faithful_compression(original, milder)):
                            floored[i] = {**ctx.messages[i], "content": milder}
                            any_floored = True
                        else:
                            floored[i] = ctx.messages[i]
                else:
                    for i in in_span:
                        floored[i] = ctx.messages[i]
                # `any_floored` matters for honesty, not for correctness: when the milder
                # pass produced nothing (no sidecar, or a refused compression) the result
                # IS arm B, and reporting it as "floored" would claim a mechanism fired
                # that did not. The observable exists so a measurement can assert the
                # mechanism rather than infer it (Gate 8.6) — mislabelling here would
                # defeat exactly that.
                floored_span = cache_floor.span_tokens_paired(ctx, ctx.messages, floored)
                if any_floored and floored_span >= floor_state.floor:
                    compressed_messages = floored
                    cache_floor.record_action(ctx, cache_floor.ACTION_FLOORED)
                    logger.info(
                        "[%s] G01: compressed the cacheable span down to the provider's "
                        "%d-token minimum instead of below it (span %d→%dt, reuse=%d)",
                        ctx.request_id, floor_state.floor, span_before,
                        floored_span, floor_state.reuse,
                    )
                elif cache_floor.allows_shrink(ctx, span_before, span_after, "G01"):
                    # Arm C was out of reach, so the choice is A or B — and with a small
                    # cache discount, keeping the span whole costs more than compressing it.
                    logger.info(
                        "[%s] G01: could not land the cacheable span on the provider's "
                        "%d-token minimum, and keeping it whole (%dt) would cost more than "
                        "compressing it to %dt at reuse=%d — compressing",
                        ctx.request_id, floor_state.floor, span_before, span_after,
                        floor_state.reuse,
                    )
                else:
                    # Arm B — the milder rate still undershot. Keep the span whole; the
                    # suffix stays compressed, which is the half that was never at stake.
                    preserved = list(compressed_messages)
                    for i in in_span:
                        preserved[i] = ctx.messages[i]
                    compressed_messages = preserved
                    ctx.g01_cache_floor_skips = getattr(ctx, "g01_cache_floor_skips", 0) + 1
                    cache_floor.record_action(ctx, cache_floor.ACTION_PRESERVED)
                    logger.info(
                        "[%s] G01: holding the cacheable span at %d tokens — compressing it "
                        "to %d would fall under this provider's %d-token minimum and "
                        "forfeit the prefix-cache discount (preserve_cacheable_prefix)",
                        ctx.request_id, span_before, span_after, floor_state.floor,
                    )
                changed = any(a != b for a, b in zip(ctx.messages, compressed_messages, strict=True))

        if changed:
            original_messages = ctx.messages
            compressed_count = sum(1 for a, b in zip(original_messages, compressed_messages, strict=True) if a != b)
            ctx.messages = compressed_messages
            # The floor reservation identifies its span by content, so the assignment
            # above has just invalidated it — and an invalid snapshot reads as an EMPTY
            # span, which the arithmetic treats as "nothing to protect" and lets G08/G19
            # shrink freely. That would undo an arm-C landing on the very next stage.
            # Covers both paths: arm C and plain compression share this assignment.
            cache_floor.resnapshot(ctx, original_messages, ctx.messages)
            tokens_after = count_messages_tokens(ctx.messages, ctx.model)
            ctx.savings.add_step(
                GROUP,
                "G01 prompt compression (layered + selective + LLMLingua-2 + Kompress fallback)",
                tokens_before,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G01-compression",
                span_input={"tokens_before": tokens_before},
                output={"tokens_after": tokens_after, "compressed_count": compressed_count},
                metadata={
                    "compression_ratio": round(tokens_after / tokens_before, 2) if tokens_before > 0 else 0.0,
                    "sidecar_url": sidecar_url,
                    "layered_composition": use_layers,
                    "selective_context": selective_pruner is not None,
                },
            )
            logger.debug(
                "[%s] G01 compressed %d → %d tokens",
                ctx.request_id,
                tokens_before,
                tokens_after,
            )
        return ctx


async def _call_llmlingua(url: str, text: str, ratio: float, force_reserve_digit: bool = True) -> str:
    if not url:
        return text  # an empty sidecar_url turns LLMLingua off
    try:
        # On GCP the sidecar requires IAM: send the proxy's identity token (none locally).
        from ml_models import cloud_run_auth_headers
        headers = await asyncio.to_thread(cloud_run_auth_headers, url)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"text": text, "ratio": ratio, "force_reserve_digit": force_reserve_digit},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json().get("compressed", text)
    except Exception as exc:
        logger.warning("G01 LLMLingua sidecar unavailable: %s — skipping compression", exc)
        return text


def generate_build_time_compressed(layers_config: Dict[str, str]) -> Dict[str, str]:
    """Generate compressed versions of static layers at build/deploy time.
    
    This should be called during CI/CD pipeline to pre-compute compressed
    versions of base/role layers that don't change per-request.
    
    Returns: {layer_name: compressed_content}
    """
    compressed = {}
    for layer_name, content in layers_config.items():
        if content:
            # Mark as pre-compressed (actual compression happens at runtime via sidecar)
            compressed[layer_name] = f"<!-- build-compressed -->{content}"
    return compressed
