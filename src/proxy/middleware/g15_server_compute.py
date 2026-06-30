"""
G15 · Server-Side Computation & MCP Offloading
Stage: After the Response
Saving: 40–80% context tokens on data-heavy tasks
Technique: Intercept tool result messages and apply server-side filter/sort/project
           before they re-enter the LLM context on the next turn.
           Hooks: filter_fn, sort_key, field_project, top_n.
           Headroom MCP server: headroom_compress / headroom_retrieve / headroom_stats
           tool calls are dispatched server-side using G28's dispatch_mcp_tool.
"""
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from middleware import RequestContext
from savings.calculator import estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G15"

# Headroom MCP tool names dispatched server-side by G15
_HEADROOM_MCP_TOOLS = frozenset({"headroom_compress", "headroom_retrieve", "headroom_stats"})


class G15ServerCompute:
    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = ctx.config.get("groups", {}).get("G15_server_compute", {})
        if not cfg.get("enabled", False):
            return response

        headroom_mcp_enabled: bool = cfg.get("headroom_mcp_server", True)
        hooks: List[Dict] = cfg.get("hooks", [])

        choices = response.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")

                # ── Headroom MCP server dispatch ─────────────────────────────
                if headroom_mcp_enabled and fn_name in _HEADROOM_MCP_TOOLS:
                    _dispatch_headroom_tool(tc, ctx)
                    continue

                # ── Config-driven hooks (existing logic) ─────────────────────
                if not hooks:
                    continue
                hook = next((h for h in hooks if h.get("tool") == fn_name), None)
                if not hook:
                    continue
                result = fn.get("result")
                if result is None:
                    continue
                tokens_before = estimate_tokens(str(result), ctx.routed_model)
                result = _apply_hook(result, hook)
                tokens_after = estimate_tokens(str(result), ctx.routed_model)
                if tokens_after < tokens_before:
                    fn["result"] = result
                    ctx.savings.add_step(
                        GROUP,
                        f"Server-side compute hook on '{fn_name}': {tokens_before}→{tokens_after}t",
                        tokens_before,
                        tokens_after,
                    )

        return response


def _dispatch_headroom_tool(tc: Dict, ctx: RequestContext) -> None:
    """Dispatch a headroom_* tool call server-side and store result in tc."""
    from middleware.g28_ccr import dispatch_mcp_tool
    fn = tc.get("function", {})
    tool_name = fn.get("name", "")
    try:
        arguments = json.loads(fn.get("arguments", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    redis_client = getattr(ctx, "redis_client", None)
    ttl = ctx.config.get("groups", {}).get("G28_ccr", {}).get("ttl_seconds", 86400)
    result = dispatch_mcp_tool(tool_name, arguments, redis_client, ttl)
    fn["result"] = result
    logger.debug("[%s] G15 headroom MCP server: %s → %r", ctx.request_id, tool_name, result)


def _apply_hook(data: Any, hook: Dict) -> Any:
    """Apply server-side compute transformations defined in config."""
    # filter: keep only items matching a field value
    if isinstance(data, list) and hook.get("filter_field"):
        field = hook["filter_field"]
        value = hook.get("filter_value")
        data = [item for item in data if isinstance(item, dict) and item.get(field) == value]

    # sort: sort list by a key
    if isinstance(data, list) and hook.get("sort_key"):
        sort_key = hook["sort_key"]
        reverse = hook.get("sort_desc", False)
        try:
            data = sorted(
                data,
                key=lambda x: x.get(sort_key, 0) if isinstance(x, dict) else x,
                reverse=reverse,
            )
        except Exception:
            pass

    # top_n: keep only first N items
    if isinstance(data, list) and hook.get("top_n"):
        data = data[: hook["top_n"]]

    # field_project: keep only specified fields
    if isinstance(data, list) and hook.get("fields"):
        fields = hook["fields"]
        data = [
            {k: v for k, v in item.items() if k in fields}
            if isinstance(item, dict)
            else item
            for item in data
        ]

    return data
