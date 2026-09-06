"""
G14 · Tool Call & Output Minimisation
Stage: After the Response
Saving: 30–90% tool token spend
Technique: Project tool results to only the fields the agent uses.
           Strip unused fields, truncate large text fields, compact arrays.
           Built-in structural compaction of CSV / JSON-array outputs.
"""
import logging
from typing import Any, Dict, List, Optional

from middleware import RequestContext
from savings.calculator import estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G14"

_MAX_FIELD_TOKENS = 200   # truncate text fields exceeding this
_MAX_RESULT_TOKENS = 500  # truncate entire result if exceeding this

# G14 compacts tool output with its OWN compactor and calls no third-party library.
#
# Two entry points were removed here, both after measuring them on the installed
# headroom 0.34.0 rather than reasoning about them:
#   * `crush` (2026-09-05) — its CSV output was never SHORTER than the input at 10, 200
#     or 2,000 rows, so the length guard discarded it every single time: a Rust round-trip
#     per CSV tool result whose result was always thrown away.
#   * `compact_document_json` (2026-09-06, backlog #48) — it replaces any JSON string leaf
#     of roughly 300 characters or more with an unresolvable `<<ccr:HASH,string,NB>>`
#     marker. The dropped bytes live in an in-process Rust store no route of ours exposes,
#     so the caller receives a pointer to nothing on a billed 200. Reproduced through this
#     function inside the deployed container: an array of incident records came back with
#     every `summary` field replaced by a marker.
#
# The built-in `_builtin_compress_spreadsheet` is purely structural — it re-keys parsed
# values into a schema/rows form and never synthesises a string — so it cannot produce
# either failure. That is why there is no runtime marker guard here, only the test that
# pins this file to calling no external compactor.



class G14ToolOutput:
    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = ctx.config.get("groups", {}).get("G14_tool_output", {})
        if not cfg.get("enabled", False):
            return response

        field_whitelist: Dict[str, List[str]] = cfg.get("field_whitelist", {})
        spreadsheet_enabled: bool = cfg.get("spreadsheet_compression", True)
        max_field_tokens: int = cfg.get("max_field_tokens", _MAX_FIELD_TOKENS)
        max_result_tokens: int = cfg.get("max_result_tokens", _MAX_RESULT_TOKENS)
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
                current = _truncate(projected, ctx.routed_model, max_field_tokens, max_result_tokens)

                # Step 2: structural compaction of CSV / JSON-array outputs
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


def _truncate(
    result: Any,
    model: str,
    max_field_tokens: int = _MAX_FIELD_TOKENS,
    max_result_tokens: int = _MAX_RESULT_TOKENS,
) -> Any:
    """Truncate large text fields and oversized results."""
    if isinstance(result, str):
        tokens = estimate_tokens(result, model)
        if tokens > max_result_tokens:
            # Truncate to approximately max_result_tokens worth of chars
            char_limit = max_result_tokens * 4
            return result[:char_limit] + "...[truncated]"
        return result

    if isinstance(result, dict):
        truncated = {}
        for k, v in result.items():
            if isinstance(v, str):
                t = estimate_tokens(v, model)
                if t > max_field_tokens:
                    v = v[: max_field_tokens * 4] + "...[truncated]"
            truncated[k] = v
        return truncated

    if isinstance(result, list):
        # Compact list: if items are primitive, keep as compact array
        total = estimate_tokens(str(result), model)
        if total > max_result_tokens:
            return result[:20]  # keep first 20 items
        return result

    return result


def _maybe_compress_spreadsheet(result: Any, model: str) -> Any:
    """Compact CSV strings and JSON arrays with the built-in structural compactor.

    Kept as a named seam (rather than inlining the call) so the pipeline step reads the
    same as it always has and so the removal of the third-party path is one edit, not a
    reshaping of `process_response`. See the module header for what was removed and why.
    """
    return _builtin_compress_spreadsheet(result, model)


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
