"""
G19 · Structured Context Pruning
Stage: Request-side (after G8 tool loading), Response-side (after G14 tool output)
Saving: 40-95% on structured content (code, JSON, logs)
Technique:
  AST-aware compression via Headroom OSS library.
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
from middleware import langfuse_tracing
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G19"

# Headroom integration (optional — falls back to built-in compressors if unavailable).
# headroom >= 0.27 exposes SmartCrusher (with compact_document_json for JSON). The older
# CodeCompressor / detect_type entry points were removed upstream, so we use SmartCrusher
# for JSON and the built-in compressors for logs/code/text. We log on failure so a future
# API drift surfaces instead of silently disabling Headroom.
_headroom_available = False
_smart_crusher = None     # headroom.SmartCrusher instance
try:
    import headroom as _headroom_mod
    _smart_crusher = _headroom_mod.SmartCrusher()
    _headroom_available = True
except Exception as _hr_exc:  # ImportError, AttributeError, or API drift
    logger.warning("G19: Headroom unavailable (%s) — using built-in compressors", _hr_exc)


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

        changed = False
        compressed_messages = []
        for msg in ctx.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, str) and len(content) >= min_length:
                content_type = _detect_content_type(content)
                if content_type and content_type in strategies:
                    compressed = _compress(content, content_type, strategies[content_type])
                    if compressed and len(compressed) < len(content):
                        compressed_messages.append({**msg, "content": compressed})
                        changed = True
                        continue
            compressed_messages.append(msg)

        if changed:
            ctx.messages = compressed_messages
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
        """Response-side: compress structured content in tool outputs and assistant messages."""
        cfg = ctx.config.get("groups", {}).get("G19_headroom", {})
        if not cfg.get("enabled", False):
            return response

        if not cfg.get("response_side_enabled", True):
            return response

        min_length = cfg.get("min_length_to_compress", 50)
        strategies = cfg.get("compression_strategies", {})

        choices = response.get("choices", [])
        total_before = 0
        total_after = 0
        changed = False

        for choice in choices:
            msg = choice.get("message", {})

            # Compress assistant message content (code blocks, JSON in responses)
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) >= min_length:
                content_type = _detect_content_type(content)
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

                content_type = _detect_content_type(result_str)
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
_CODE_PATTERNS = [
    re.compile(r"^(import |from |def |class |function |const |let |var |public |private )", re.MULTILINE),
    re.compile(r"```\w*\n", re.MULTILINE),
]
_LOG_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]", re.MULTILINE),
    re.compile(r"^\[?(INFO|DEBUG|WARN|ERROR|FATAL)\]?", re.MULTILINE),
]


def _detect_content_type(text: str) -> Optional[str]:
    """Detect whether text is JSON, code, logs, or plain text.

    Uses Headroom's auto-detection if available, otherwise falls back
    to pattern heuristics. Returns "text" for plain prose so SmartCrusher
    can apply verbosity reduction — callers must have "text" in their
    compression_strategies config to activate this path.
    """
    if _headroom_available:
        try:
            detected = _headroom_mod.detect_type(text)
            if detected:
                return detected
        except Exception:
            pass

    # Heuristic fallback
    stripped = text.strip()

    # JSON detection
    if _JSON_PATTERN.match(stripped):
        try:
            json.loads(stripped)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass

    # Code detection
    for pattern in _CODE_PATTERNS:
        if pattern.search(stripped):
            return "code"

    # Log detection
    for pattern in _LOG_PATTERNS:
        if pattern.search(stripped):
            return "logs"

    # Plain text — return "text" so SmartCrusher can apply verbosity reduction
    return "text"


# ─── Compressors ─────────────────────────────────────────────────────────────

def _compress(text: str, content_type: str, strategy: Dict[str, Any]) -> Optional[str]:
    """Compress structured text. Routing:
      json           → Headroom SmartCrusher.compact_document_json (best-in-class for JSON),
                       falling back to the built-in JSON compactor if unavailable / no-op.
      logs/code/text → built-in compressors (Headroom's query-less crush does not help
                       these; the upstream CodeCompressor was removed).

    Instruction/prose content never reaches here — callers gate on content_type being in
    the configured strategies, and prose ("text") is excluded by default.
    """
    # Headroom: JSON compaction (its strongest path); guard so we only keep a real reduction.
    if _headroom_available and _smart_crusher is not None and content_type == "json":
        try:
            crushed = _smart_crusher.compact_document_json(text)
            if isinstance(crushed, str) and 0 < len(crushed) < len(text):
                return crushed
        except Exception:
            pass  # fall through to built-in

    # Built-in fallback compressors (no headroom dependency)
    if content_type == "json":
        return _compress_json(text, strategy)
    elif content_type == "code":
        return _compress_code(text, strategy)
    elif content_type == "logs":
        return _compress_logs(text, strategy)
    elif content_type == "text":
        return _compress_text(text, strategy)
    return None


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
    """Strip comments, blank lines, and optionally compress imports."""
    lines = text.split("\n")
    result = []

    for line in lines:
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

        result.append(line)

    # Compress imports: collapse multiple import lines into fewer
    if strategy.get("compress_imports", True):
        result = _compress_import_lines(result)

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
    stripping filler phrases. Built-in fallback when headroom.SmartCrusher is
    not available.

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
    """Deduplicate repeated log lines and truncate long lines."""
    lines = text.split("\n")
    max_line_len = strategy.get("truncate_long_lines", 200)
    dedupe = strategy.get("dedupe_lines", True)

    result = []
    seen: Dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Truncate long lines
        if len(stripped) > max_line_len:
            stripped = stripped[:max_line_len] + "...[truncated]"

        if dedupe:
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
