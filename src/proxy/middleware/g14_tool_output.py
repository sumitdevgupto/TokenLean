"""
G14 · Tool Call & Output Minimisation
Stage: After the Response
Saving: 30–90% tool token spend
Technique: Project tool results to only the fields the agent uses.
           Strip unused fields, truncate large text fields, compact arrays.
           headroom SmartCrusher (crush / compact_document_json) applied to CSV/JSON-array outputs.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from middleware import RequestContext
from savings.calculator import estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G14"

_MAX_FIELD_TOKENS = 200   # truncate text fields exceeding this
_MAX_RESULT_TOKENS = 500  # truncate entire result if exceeding this

# ─── Optional headroom SmartCrusher (tabular / JSON-array compression) ────────
# NOTE: the older headroom.compress_spreadsheet() expects a *file path*, not an
# in-memory string, so it never worked here. SmartCrusher operates on strings:
#   crush(text) -> CrushResult(.compressed)  ;  compact_document_json(json) -> str
_smart_crusher = None
try:
    import headroom as _headroom_g14  # type: ignore
    _smart_crusher = _headroom_g14.SmartCrusher()
except (ImportError, AttributeError):
    pass

# Detect CSV: first non-blank line contains commas, no JSON brackets
_CSV_PATTERN = re.compile(r"^[^\[\{<\n]*,[^\n]+\n", re.MULTILINE)


class G14ToolOutput:
    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = ctx.config.get("groups", {}).get("G14_tool_output", {})
        if not cfg.get("enabled", False):
            return response

        field_whitelist: Dict[str, List[str]] = cfg.get("field_whitelist", {})
        spreadsheet_enabled: bool = cfg.get("spreadsheet_compression", True)
        choices = response.get("choices", [])
        changed = False

        for choice in choices:
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                continue
            for tc in tool_calls:
                fn = tc.get("function", {})
                raw_result = fn.get("result") or tc.get("result")
                if raw_result is None:
                    continue
                fn_name = fn.get("name", "")
                tokens_before = estimate_tokens(str(raw_result), ctx.routed_model)

                # Step 1: field projection + truncation (existing logic)
                projected = _project(raw_result, field_whitelist.get(fn_name))
                current = _truncate(projected, ctx.routed_model)

                # Step 2: headroom.compress_spreadsheet for CSV / JSON-array outputs
                if spreadsheet_enabled:
                    current = _maybe_compress_spreadsheet(current, ctx.routed_model)

                tokens_after = estimate_tokens(str(current), ctx.routed_model)

                if tokens_after < tokens_before:
                    fn["result"] = current
                    ctx.savings.add_step(
                        GROUP,
                        f"Tool output minimisation: {fn_name} {tokens_before}→{tokens_after}t",
                        tokens_before,
                        tokens_after,
                    )
                    changed = True

        return response


def _project(result: Any, whitelist: Optional[List[str]]) -> Any:
    """Keep only whitelisted fields from a dict result."""
    if not whitelist or not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items() if k in whitelist}


def _truncate(result: Any, model: str) -> Any:
    """Truncate large text fields and oversized results."""
    if isinstance(result, str):
        tokens = estimate_tokens(result, model)
        if tokens > _MAX_RESULT_TOKENS:
            # Truncate to approximately _MAX_RESULT_TOKENS worth of chars
            char_limit = _MAX_RESULT_TOKENS * 4
            return result[:char_limit] + "...[truncated]"
        return result

    if isinstance(result, dict):
        truncated = {}
        for k, v in result.items():
            if isinstance(v, str):
                t = estimate_tokens(v, model)
                if t > _MAX_FIELD_TOKENS:
                    v = v[: _MAX_FIELD_TOKENS * 4] + "...[truncated]"
            truncated[k] = v
        return truncated

    if isinstance(result, list):
        # Compact list: if items are primitive, keep as compact array
        total = estimate_tokens(str(result), model)
        if total > _MAX_RESULT_TOKENS:
            return result[:20]  # keep first 20 items
        return result

    return result


def _maybe_compress_spreadsheet(result: Any, model: str) -> Any:
    """Apply headroom SmartCrusher to CSV strings and JSON arrays.

    CSV strings use ``SmartCrusher.crush(text).compressed``; JSON arrays are
    serialised and passed to ``compact_document_json()`` (returns compacted JSON
    text). Falls back to the built-in compactor when headroom is unavailable or
    the call raises.
    """
    if _smart_crusher is None:
        return _builtin_compress_spreadsheet(result, model)

    try:
        if isinstance(result, str) and _CSV_PATTERN.search(result):
            crushed = _smart_crusher.crush(result)
            compressed = getattr(crushed, "compressed", None)
            return compressed if compressed and len(compressed) < len(result) else result

        if isinstance(result, list) and len(result) >= 2:
            raw = json.dumps(result, separators=(",", ":"))
            compressed = _smart_crusher.compact_document_json(raw)
            if isinstance(compressed, str) and compressed and len(compressed) < len(raw):
                try:
                    return json.loads(compressed)
                except (json.JSONDecodeError, TypeError):
                    return compressed
    except Exception as exc:
        logger.debug("G14 headroom SmartCrusher failed: %s — using built-in", exc)
        return _builtin_compress_spreadsheet(result, model)

    return result


def _builtin_compress_spreadsheet(result: Any, model: str) -> Any:
    """Built-in fallback: dedup-key JSON arrays → schema+rows format (same as G19 _dedupe_repeated_structures)."""
    if isinstance(result, list) and len(result) >= 2 and all(isinstance(r, dict) for r in result):
        from collections import Counter
        key_sets = [frozenset(r.keys()) for r in result]
        most_common = Counter(key_sets).most_common(1)
        if most_common and most_common[0][1] > len(result) * 0.5:
            shared_keys = sorted(most_common[0][0])
            rows = [[r.get(k) for k in shared_keys] for r in result]
            return {"_schema_": shared_keys, "_rows_": rows}
    return result
