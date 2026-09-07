"""
G19 · Structured Context Pruning
Stage: Request-side (after G8 tool loading), Response-side (after G14 tool output)
Saving: 40-95% on structured content (code, JSON, logs)
Technique:
  Structural compression via this module's own compressors. NOT AST-based: nothing here
  parses a syntax tree — the compressors work line-wise, sentence-wise and over decoded
  JSON structure. ("AST-aware" was claimed here until 2026-09-07; `ast` is not imported.)
  Auto-detects content type and applies optimal compressor:
    - Code:    strips imports, comments, whitespace; preserves logic
    - JSON:    removes empty fields, deduplicates repeated structures
    - Logs:    groups, truncates, deduplicates
    - Config:  replaces with concise summaries

  G1 handles natural-language compression (LLMLingua); G19 handles structured
  content. Content-type detection prevents overlap.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import cache_floor
from middleware import langfuse_tracing
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G19"

# G19 compresses with its OWN compressors and calls no third-party compactor.
#
# It used to route JSON through `headroom.SmartCrusher.compact_document_json`. That was
# REMOVED 2026-09-06 (backlog #48) because the library replaces any JSON string leaf of
# roughly 300 characters or more with an unresolvable `<<ccr:HASH,string,NB>>` retrieval
# marker: the bytes are handed to an in-process Rust store that this repo exposes no route
# to, so the model receives a pointer to nothing on a billed 200. Confirmed on 2026-09-06
# by running this middleware inside the deployed container against the shipped config —
# a runbook tool result came back as `"summary":"<<ccr:884b9b5deca1,string,423B>>"`.
#
# The output is valid JSON, so the parseability guard added for backlog #45 did not catch
# it; only a check on the CONTENT does, which is why `_is_faithful_compaction` exists below.
#
# Measured before removing it, in the container, on 70 real tool payloads harvested from
# the datasets: SmartCrusher 55.3% of tokens removed vs the built-in JSON compactor 54.5%.
# Eight tenths of a point is not worth a transform that silently deletes the answer, and
# the library's largest single "win" in that sample WAS one of the deletions.


class G19Headroom:
    """Structured context pruning for code, JSON, logs, and config content."""

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        """Request-side: compress structured content in messages (tool defs, code blocks)."""
        cfg = ctx.config.get("groups", {}).get("G19_headroom", {})
        if not cfg.get("enabled", False):
            return ctx

        if not cfg.get("request_side_enabled", True):
            return ctx

        tokens_before = ctx.current_token_count
        min_length = cfg.get("min_length_to_compress", 50)
        strategies = cfg.get("compression_strategies", {})
        ratio = _dominance_ratio(cfg)

        changed = False
        compressed_messages = []
        for msg in ctx.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, str) and len(content) >= min_length:
                content_type = _detect_content_type(content, ratio)
                if content_type and content_type in strategies:
                    compressed = _compress(content, content_type, strategies[content_type])
                    if compressed and len(compressed) < len(content):
                        compressed_messages.append({**msg, "content": compressed})
                        changed = True
                        continue
            compressed_messages.append(msg)

        # Prefix-cache floor (backlog #41). G19 rewrites EVERY role including `system`,
        # so it can push a provider's cacheable span under the minimum size just as
        # compression can — and it runs after G01, so a guard that lived only there could
        # be undone here. It has no compression-rate dial, so the choice is binary: take
        # the shrink, or keep the in-span messages whole. Everything outside the span is
        # compressed either way; it is never cached, so its saving is never at stake.
        if changed:
            floor_state = cache_floor.get(ctx)
            if floor_state.active and not cache_floor.allows_shrink(
                ctx,
                cache_floor.span_tokens(ctx, ctx.messages),
                cache_floor.span_tokens_paired(ctx, ctx.messages, compressed_messages),
                "G19",
            ):
                # Membership is decided on the ORIGINAL at each index, never on the
                # rewrite: a compressed message no longer matches the reservation's
                # snapshot, so testing it would restore nothing. And when the snapshot is
                # STALE (some earlier group rewrote the messages without re-taking it)
                # `covers` answers False to everything — so refusing the shrink and then
                # restoring by `covers` would restore nothing and take the shrink anyway.
                # Stale means "we cannot tell what is in the span": restore all of it.
                _stale = cache_floor.snapshot_is_stale(ctx)
                compressed_messages = [
                    (ctx.messages[i] if (_stale or floor_state.covers(ctx.messages[i])) else m)
                    for i, m in enumerate(compressed_messages)
                ]
                changed = any(a != b for a, b in zip(ctx.messages, compressed_messages))
                cache_floor.record_action(ctx, cache_floor.ACTION_PRESERVED)
                logger.info(
                    "[%s] G19: holding the cacheable span whole — pruning it would fall "
                    "under this provider's %d-token minimum cacheable size",
                    ctx.request_id, floor_state.floor,
                )

        if changed:
            _pre_g19_messages = ctx.messages
            ctx.messages = compressed_messages
            # Keep the floor reservation describing the messages that now exist — a
            # content-keyed snapshot that no longer matches reads as an EMPTY span, which
            # the arithmetic treats as "nothing to protect". G19 is the last consumer
            # today, so this is the invariant rather than a live fix; leaving it stale
            # would make the next group added after G19 fail open silently.
            cache_floor.resnapshot(ctx, _pre_g19_messages, ctx.messages)
            tokens_after = ctx.current_token_count
            ctx.savings.add_step(
                GROUP,
                f"G19 structured pruning (request-side) {tokens_before}→{tokens_after}t",
                tokens_before,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G19-headroom-request",
                span_input={"tokens_before": tokens_before},
                output={"tokens_after": tokens_after},
                metadata={"side": "request"},
            )
            logger.debug(
                "[%s] G19 request-side: %d → %d tokens",
                ctx.request_id, tokens_before, tokens_after,
            )
        return ctx

    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Response-side: compress tool RESULTS (always) + assistant answer content (opt-in).

        Answer content is only touched when `response_side_compress_answers: true` —
        by default the user-visible answer is returned exactly as the model wrote it.
        """
        cfg = ctx.config.get("groups", {}).get("G19_headroom", {})
        if not cfg.get("enabled", False):
            return response

        if not cfg.get("response_side_enabled", True):
            return response

        min_length = cfg.get("min_length_to_compress", 50)
        strategies = cfg.get("compression_strategies", {})
        ratio = _dominance_ratio(cfg)
        # The ANSWER (assistant message content) is user-visible, whatever its shape —
        # prose, code (comments/docstrings are part of the deliverable), quoted logs,
        # or a JSON the model produced for the caller. Rewriting it changes what the
        # user reads while saving nothing on THIS call: the provider has already
        # generated and billed those output tokens (the only benefit is deferred, if
        # the answer is replayed as later-turn history). Off by default; opt in to
        # restore the old behaviour. Tool RESULTS are still compressed either way —
        # they are data that gets replayed into later turns, not the answer itself.
        # Request-side compression is unaffected (there it genuinely shrinks the send).
        compress_answers = cfg.get("response_side_compress_answers", False)

        choices = response.get("choices", [])
        total_before = 0
        total_after = 0
        changed = False

        for choice in choices:
            msg = choice.get("message", {})

            # Compress assistant message content — opt-in only (see compress_answers)
            content = msg.get("content", "")
            if compress_answers and isinstance(content, str) and len(content) >= min_length:
                content_type = _detect_content_type(content, ratio)
                if content_type and content_type in strategies:
                    before_tokens = estimate_tokens(content, ctx.routed_model)
                    compressed = _compress(content, content_type, strategies[content_type])
                    if compressed and len(compressed) < len(content):
                        after_tokens = estimate_tokens(compressed, ctx.routed_model)
                        # Mutates in-place on the response choices list (request-side rebuilds instead)
                        msg["content"] = compressed
                        total_before += before_tokens
                        total_after += after_tokens
                        changed = True

            # Compress tool call results
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {})
                raw_result = fn.get("result") or tc.get("result")
                if raw_result is None:
                    continue

                result_str = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
                if len(result_str) < min_length:
                    continue

                content_type = _detect_content_type(result_str, ratio)
                if content_type and content_type in strategies:
                    before_tokens = estimate_tokens(result_str, ctx.routed_model)
                    compressed = _compress(result_str, content_type, strategies[content_type])
                    if compressed and len(compressed) < len(result_str):
                        after_tokens = estimate_tokens(compressed, ctx.routed_model)
                        # Store back as same type
                        if isinstance(raw_result, str):
                            fn["result"] = compressed
                        else:
                            try:
                                fn["result"] = json.loads(compressed)
                            except (json.JSONDecodeError, TypeError):
                                fn["result"] = compressed
                        total_before += before_tokens
                        total_after += after_tokens
                        changed = True

        if changed:
            ctx.savings.add_step(
                GROUP,
                f"G19 structured pruning (response-side) {total_before}→{total_after}t",
                total_before,
                total_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G19-headroom-response",
                span_input={"tokens_before": total_before},
                output={"tokens_after": total_after},
                metadata={"side": "response"},
            )
            logger.debug(
                "[%s] G19 response-side: %d → %d tokens",
                ctx.request_id, total_before, total_after,
            )

        return response


# ─── Content type detection ──────────────────────────────────────────────────

# Patterns for content type heuristics
_JSON_PATTERN = re.compile(r"^\s*[\[{]", re.DOTALL)
_FENCE_PATTERN = re.compile(r"^\s*```")
_CODE_LINE_PATTERN = re.compile(
    r"^\s*(import |from |def |class |function |const |let |var |public |private )")
_LOG_LINE_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]"),
    re.compile(r"^\[?(INFO|DEBUG|WARN|ERROR|FATAL)\]?"),
]

# A payload must be MOSTLY code (or mostly logs) before we treat it as one.
#
# The previous detector used `.search()` over the whole message, so a single ```
# fence — or one line starting "from " — reclassified an entire prose answer as
# code. _compress_code then deleted every '#'-leading line, i.e. the answer's
# Markdown headings, silently rewriting user-visible text. Requiring dominance
# keeps genuine code/log payloads (where such lines are the overwhelming majority)
# while leaving prose-with-an-example alone. Default only — tunable per tenant via
# `G19_headroom.detect_dominance_ratio` (resolved by `_dominance_ratio(cfg)`).
_DOMINANCE_RATIO = 0.5


def _dominance_ratio(cfg: Dict[str, Any]) -> float:
    """Resolve the detection dominance threshold from config, defaulting safely.

    Invalid values (non-numeric, <=0, >1) fall back to the default rather than
    raising or producing an always-/never-matching detector.
    """
    try:
        ratio = float(cfg.get("detect_dominance_ratio", _DOMINANCE_RATIO))
    except (TypeError, ValueError):
        return _DOMINANCE_RATIO
    return ratio if 0.0 < ratio <= 1.0 else _DOMINANCE_RATIO


def _iter_fenced(lines):
    """Walk ``` fences once: yields (line, is_fence_marker, in_fence).

    Single source of truth for fence semantics — _line_stats (detection) and
    _compress_code (compression) must always agree on what is inside a fence,
    otherwise a payload can be classified by one boundary and compressed by
    another. `in_fence` is the state AFTER processing the marker (True on the
    opening marker, False on the closing one).
    """
    in_fence = False
    for line in lines:
        if _FENCE_PATTERN.match(line):
            in_fence = not in_fence
            yield line, True, in_fence
        else:
            yield line, False, in_fence


def _line_stats(text: str) -> tuple:
    """(non_blank_lines, code_lines, log_lines). Lines inside a ``` fence count as code."""
    total = code = logs = 0
    for line, is_marker, in_fence in _iter_fenced(text.split("\n")):
        if is_marker:
            continue  # the fence marker itself is neither code nor prose
        if not line.strip():
            continue
        total += 1
        if in_fence or _CODE_LINE_PATTERN.match(line):
            code += 1
        stripped = line.lstrip()
        if any(p.match(stripped) for p in _LOG_LINE_PATTERNS):
            logs += 1
    return total, code, logs


def _detect_content_type(text: str, dominance_ratio: float = None) -> Optional[str]:
    """Detect whether text is JSON, code, logs, or plain text.

    Pattern heuristics only. Returns "text" for plain prose so the text compressor can
    apply verbosity reduction — callers must have "text" in their compression_strategies
    config to activate this path.

    `dominance_ratio` is the fraction of non-blank lines that must be code-shaped
    (or log-shaped) before the payload counts as code/logs; None uses the module
    default. The middleware resolves it from `G19_headroom.detect_dominance_ratio`
    via `_dominance_ratio(cfg)` — keep the parameter threaded so the threshold
    stays config-driven, never hardcoded at a call site.
    """
    if dominance_ratio is None:
        dominance_ratio = _DOMINANCE_RATIO
    # A `headroom.detect_type(text)` call used to sit here, wrapped in a bare
    # `except Exception: pass`. The function does not exist in headroom 0.34.0 (verified
    # against the installed package, 2026-09-05), so it raised AttributeError on every
    # single call and fell straight through to the heuristic below — which has therefore
    # always been the only classifier. Removed rather than left as an import-shaped
    # comment: a silent always-failing call is worse than no call, because it reads like
    # a working integration to anyone tracing this path.
    stripped = text.strip()

    # JSON detection
    if _JSON_PATTERN.match(stripped):
        try:
            json.loads(stripped)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass

    # Code / log detection — the signal must DOMINATE the payload, not merely appear
    # somewhere in it (see _DOMINANCE_RATIO / detect_dominance_ratio).
    total, code_lines, log_lines = _line_stats(stripped)
    if total:
        if code_lines / total >= dominance_ratio:
            return "code"
        if log_lines / total >= dominance_ratio:
            return "logs"

    # Plain text — return "text" so the text compressor can apply verbosity reduction
    return "text"


# ─── Compressors ─────────────────────────────────────────────────────────────

# A compaction that shortens a payload by DELETING it is not a compaction. Anything a
# compactor emits that matches this is a reference into a store the caller cannot read.
_UNRESOLVABLE_REFERENCE = re.compile(r"<<\s*ccr\s*:", re.IGNORECASE)


def _preserves_json_parseability(before: str, after: str) -> bool:
    """True unless a compaction turned parseable JSON into something that is not.

    Deliberately one-directional: if the INPUT was not valid JSON we have no contract to
    uphold and return True. We only refuse the case that actually breaks a caller —
    parseable in, unparseable out.
    """
    try:
        json.loads(before)
    except Exception:
        return True          # nothing was promised about this payload
    try:
        json.loads(after)
        return True
    except Exception:
        return False


def _is_faithful_compaction(before: str, after: str) -> bool:
    """The acceptance boundary for every value G19 substitutes into a message.

    Two refusals, each one learned from a defect that shipped:

      1. **Parseable in, unparseable out** (backlog #45). A tool result's consumer does
         `json.loads(result)`, so shorter-but-broken is a silent failure on a billed 200.
      2. **Content replaced by an unresolvable reference** (backlog #48). Valid JSON, so
         (1) passes it, and shorter, so a length check passes it — and the answer is gone.

    Length was the only test until 2026-09-05; (1) was added then and (2) on 2026-09-06.
    Both are currently INERT against the compressors this module ships, which are
    structural and cannot produce either failure. That is deliberate and worth the two
    `json.loads`: the guard is here so that re-introducing ANY third-party compactor
    cannot repeat #48 in silence. It is stated as a property of the output, not as a
    blocklist against one library's sentinel, because the last time this was reasoned
    about as "that library does not do that", the probe used the wrong input shape and
    the conclusion was wrong for eight days.
    """
    if not _preserves_json_parseability(before, after):
        return False
    return not _UNRESOLVABLE_REFERENCE.search(after)


def _compress(text: str, content_type: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Compress structured text with this module's own compressors — json / code / logs /
    text — every one of which must clear `_is_faithful_compaction` before it is returned.
    No third-party compactor is called from here; see the note at the top of the file for
    why the headroom JSON path was removed (backlog #48).

    Prose ("text") DOES reach here on the request side — the shipped config enables the
    `text` strategy so repeated boilerplate in a pasted payload is deduped. ANSWER content
    (assistant messages, ALL types) is blocked on the RESPONSE side by
    `response_side_compress_answers` (default false), because there the content is the
    user-visible answer; response-side tool RESULTS still flow through here. (An earlier
    version of this docstring claimed prose was excluded by default; that was false
    against the shipped template, and the test fixture omitted `text` so nothing caught
    it.)
    """
    # A JSON payload dominated by ONE long string leaf is prose in an envelope, not
    # structured data — the exact shape of a tool result carrying a document
    # ({"text": "<11k-char runbook>"}). Record-array compaction mangles that shape: the
    # since-removed SmartCrusher path reduced one such payload 11,492 → 45 chars in the
    # live container (2026-09-03, DS22 all-on turn 2), the model then fabricated the facts
    # it could no longer read, and the only-G28 arm answered correctly. This branch stays
    # even though that compactor is gone (#48), because it is what makes the leaf take the
    # SAME text strategy the identical content gets when it arrives unwrapped.
    if content_type == "json":
        enveloped = _compress_prose_envelope(text, strategy)
        if enveloped is not None:
            return enveloped

    if content_type == "json":
        compacted = _compress_json(text, strategy)
    elif content_type == "code":
        compacted = _compress_code(text, strategy)
    elif content_type == "logs":
        compacted = _compress_logs(text, strategy)
    elif content_type == "text":
        compacted = _compress_text(text, strategy)
    else:
        return None

    # Nothing leaves here without passing the fidelity boundary. Keeping the original is
    # always the safe outcome: the caller treats None as "not compressible" and sends the
    # payload whole, which costs tokens and loses nothing.
    if compacted is not None and not _is_faithful_compaction(text, compacted):
        logger.warning(
            "G19: %s compaction rejected as unfaithful (%d→%d chars) — sending the "
            "payload uncompressed", content_type, len(text), len(compacted),
        )
        return None
    return compacted


_ENVELOPE_DOMINANCE = 0.7  # a single string leaf carrying >70% of the payload IS the payload


def _largest_string_leaf(obj: Any) -> str:
    """The longest string value anywhere in a parsed JSON structure."""
    if isinstance(obj, str):
        return obj
    best = ""
    values = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ()
    for v in values:
        leaf = _largest_string_leaf(v)
        if len(leaf) > len(best):
            best = leaf
    return best


def _compress_prose_envelope(text: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Compress a prose-dominated JSON payload by its leaf, never by its structure.

    Returns the re-serialised payload with the dominant leaf text-compressed, the ORIGINAL
    text when the leaf resists compression (keeping content beats saving tokens), or None
    when the payload is genuinely structured and should take the normal JSON path.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    leaf = _largest_string_leaf(data)
    if not leaf or len(leaf) < _ENVELOPE_DOMINANCE * len(text):
        return None
    compressed_leaf = _compress_text(leaf, strategy)
    if not compressed_leaf or len(compressed_leaf) >= len(leaf):
        return text  # envelope confirmed but nothing safe to remove — keep it whole

    def _swap(obj):
        if isinstance(obj, str):
            return compressed_leaf if obj == leaf else obj
        if isinstance(obj, dict):
            return {k: _swap(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_swap(v) for v in obj]
        return obj

    return json.dumps(_swap(data), separators=(",", ":"))


def _compress_json(text: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Remove empty fields, compact JSON, deduplicate repeated structures."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if strategy.get("remove_empty", True):
        data = _remove_empty_fields(data)

    if strategy.get("dedupe_keys", True):
        data = _dedupe_repeated_structures(data)

    return json.dumps(data, separators=(",", ":"))


def _remove_empty_fields(obj: Any) -> Any:
    """Recursively remove empty/null/empty-string fields."""
    if isinstance(obj, dict):
        return {
            k: _remove_empty_fields(v)
            for k, v in obj.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    elif isinstance(obj, list):
        return [_remove_empty_fields(item) for item in obj if item is not None]
    return obj


def _dedupe_repeated_structures(obj: Any) -> Any:
    """For arrays of dicts with identical keys, convert to schema-referencing format.

    Reduces token count by replacing repeated key names per row with a single
    shared schema array, e.g.:
      [{"a":1,"b":2},{"a":3,"b":4}] ->
      {"_schema_":["a","b"],"_rows_":[[1,2],[3,4]]}
    """
    if isinstance(obj, dict):
        return {k: _dedupe_repeated_structures(v) for k, v in obj.items()}

    if isinstance(obj, list) and len(obj) >= 2:
        if all(isinstance(item, dict) for item in obj):
            key_sets = [frozenset(item.keys()) for item in obj]
            from collections import Counter
            most_common = Counter(key_sets).most_common(1)
            if most_common and most_common[0][1] > len(obj) * 0.5:
                shared_keys = sorted(list(most_common[0][0]))
                rows = []
                for item in obj:
                    rows.append([_dedupe_repeated_structures(item.get(k)) for k in shared_keys])
                return {"_schema_": shared_keys, "_rows_": rows}
        # Generic list: recurse on items
        return [_dedupe_repeated_structures(item) for item in obj]

    return obj


def _compress_code(text: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Strip comments, blank lines, and optionally compress imports.

    When the payload contains ``` fences, ONLY the fenced regions are treated as code —
    everything outside them is prose (Markdown headings, bullets, explanation) and is
    emitted verbatim. Without this, a '# Heading' outside a fence is indistinguishable
    from a Python comment and gets deleted, silently rewriting the answer.
    """
    lines = text.split("\n")
    has_fence = any(_FENCE_PATTERN.match(ln) for ln in lines)

    def _crush(code_lines):
        """Apply the code compressors to a run of lines known to BE code."""
        out = []
        for line in code_lines:
            stripped = line.strip()

            # Strip single-line comments
            if strategy.get("strip_comments", True):
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                # Strip inline comments (simple heuristic — not AST-level)
                for comment_marker in (" #", " //"):
                    idx = line.find(comment_marker)
                    if idx > 0 and not _in_string(line, idx):
                        line = line[:idx].rstrip()

            # Strip blank lines
            if strategy.get("strip_whitespace", True) and stripped == "":
                continue

            out.append(line)
        # Collapse consecutive imports. Safe here because every line in this run is
        # code — applied to a whole fenced document it could not tell a prose line
        # opening "from ..." from a real import statement.
        if strategy.get("compress_imports", True):
            out = _compress_import_lines(out)
        return out

    if not has_fence:
        return "\n".join(_crush(lines))

    # Fenced payload: compress each fenced region, pass everything else through
    # verbatim. A '# Heading' outside a fence is indistinguishable from a Python
    # comment, so deleting it would silently rewrite the surrounding prose.
    result, segment = [], []
    for line, is_marker, in_fence in _iter_fenced(lines):
        if is_marker:
            if not in_fence:          # closing marker — flush the region we just left
                result.extend(_crush(segment))
                segment = []
            result.append(line)
        elif in_fence:
            segment.append(line)
        else:
            result.append(line)       # prose outside a fence — never touched
    if segment:                       # unterminated fence
        result.extend(_crush(segment))

    return "\n".join(result)


def _in_string(line: str, pos: int) -> bool:
    """Rough check if position is inside a string literal."""
    in_single = False
    in_double = False
    for i in range(pos):
        c = line[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
    return in_single or in_double


def _compress_import_lines(lines: List[str]) -> List[str]:
    """Group consecutive import/from lines into fewer lines."""
    result = []
    import_block: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            import_block.append(stripped)
        else:
            if import_block:
                result.extend(import_block)
                import_block = []
            result.append(line)

    if import_block:
        result.extend(import_block)

    return result


def _compress_text(text: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Reduce verbosity of plain prose by deduplicating repeated sentences and
    stripping filler phrases. The only text compressor there is — prose never had a
    headroom path, and JSON's was removed in #48.

    Strategy keys:
      dedupe_sentences (bool, default True)  — collapse exact-duplicate sentences
      max_sentence_len (int, default 0)      — truncate sentences longer than N chars (0=off)
    """
    dedupe = strategy.get("dedupe_sentences", True)
    max_len = strategy.get("max_sentence_len", 0)

    # Split on sentence-ending punctuation followed by whitespace
    sentence_end = re.compile(r"(?<=[.!?])\s+")
    sentences = sentence_end.split(text.strip())

    seen: dict = {}
    result = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if max_len and len(s) > max_len:
            s = s[:max_len] + "…"
        if dedupe:
            if s in seen:
                seen[s] += 1
                continue
            seen[s] = 1
        result.append(s)

    if not result:
        return None

    compressed = " ".join(result)
    return compressed if len(compressed) < len(text) else None


def _compress_logs(text: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Deduplicate repeated log lines and truncate long lines.

    Severity-aware dedup (2026-07-23): lines matching `always_keep_severities`
    (default ERROR/FATAL/CRITICAL/PANIC) are NEVER folded into the dedup count —
    every occurrence survives verbatim with its own timestamp. A recurring error
    is itself diagnostic signal (is this a one-off or is it flapping?), not noise;
    only low-severity boilerplate (INFO/DEBUG/WARN heartbeat lines) is collapsed.
    Previously the dedup stripped timestamps before comparing ANY line, so a real
    second occurrence of an ERROR (identical text, different timestamp) landed in
    the same bucket as repeated INFO noise and silently vanished behind an opaque
    "[N duplicate log patterns suppressed]" footer that named no pattern — proven
    on DS7 ds7-03 (pitch-test-plan quality gate): the optimised answer omitted a
    genuine ERROR recurrence at 10:51:01Z that the unoptimised baseline reported.
    """
    lines = text.split("\n")
    max_line_len = strategy.get("truncate_long_lines", 200)
    dedupe = strategy.get("dedupe_lines", True)
    always_keep = strategy.get("always_keep_severities", ["ERROR", "FATAL", "CRITICAL", "PANIC"])
    always_keep_re = (
        re.compile(r"\b(?:" + "|".join(re.escape(s) for s in always_keep) + r")\b", re.IGNORECASE)
        if always_keep else None
    )

    result = []
    seen: Dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        is_high_severity = bool(always_keep_re and always_keep_re.search(stripped))

        # Truncate long lines (after severity detection, so a marker near the
        # start of a long line is never missed because of the later cut).
        if len(stripped) > max_line_len:
            stripped = stripped[:max_line_len] + "...[truncated]"

        if dedupe and not is_high_severity:
            # Normalise: remove timestamps for dedup comparison
            normalised = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\.\d]*", "<TS>", stripped)
            if normalised in seen:
                seen[normalised] += 1
                continue
            seen[normalised] = 1

        result.append(stripped)

    # Append dedup counts
    if dedupe:
        deduped_count = sum(1 for v in seen.values() if v > 1)
        if deduped_count > 0:
            result.append(f"[{deduped_count} duplicate log patterns suppressed]")

    return "\n".join(result)
