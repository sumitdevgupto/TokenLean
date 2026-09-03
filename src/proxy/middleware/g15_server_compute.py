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

from middleware import RequestContext, resolve_group_config
from savings.calculator import estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G15"

# Headroom MCP tool names dispatched server-side by G15
# ONE definition, imported from G28 rather than duplicated. The two copies had already
# drifted in intent: G28's comment claimed G15 imported it while G15 kept its own.
from middleware.g28_ccr import _CCR_MCP_TOOLS as _HEADROOM_MCP_TOOLS


def authorize_dispatch(ctx: RequestContext, tool_name: str):
    """Thin re-export so the dispatch loop reads straightforwardly; the policy lives
    in G32, which owns tool authorization. Imported lazily to keep module import order
    free of a G15<->G32 cycle."""
    from middleware.g32_tool_eligibility import authorize_dispatch as _authz
    return _authz(ctx, tool_name)


def record_dispatch_block(ctx: RequestContext, tool_name: str, reason: str) -> None:
    from middleware.g32_tool_eligibility import record_dispatch_block as _rec
    _rec(ctx, tool_name, reason)


class G15ServerCompute:
    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        # resolve_group_config, not a raw .get() chain: the OPERATOR overlay
        # (tenants.<id>.groups.G15_server_compute in config.yaml) was silently
        # ignored here, so an operator could not disable headroom_mcp_server for a
        # single tenant. It also carries the type guards that stop a mis-indented
        # tenants: block raising AttributeError and 500-ing every request.
        cfg = resolve_group_config(ctx, "G15_server_compute")
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
                    # Matching by bare name is not enough to justify EXECUTING.
                    # authorize_dispatch refuses anything the proxy did not itself
                    # advertise (a tenant's own same-named tool, or a name a model
                    # produced unprompted) and anything the tenant's tool policy
                    # denies. A refused call is left in the response untouched and
                    # simply not acted on — stripping is the response gate's job.
                    reason = authorize_dispatch(ctx, fn_name)
                    if reason:
                        record_dispatch_block(ctx, fn_name, reason)
                        continue
                    await _dispatch_headroom_tool(tc, ctx)
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


async def _dispatch_headroom_tool(tc: Dict, ctx: RequestContext) -> None:
    """Dispatch a headroom_* tool call server-side and store result in tc."""
    from middleware.g28_ccr import ccr_available, dispatch_mcp_tool
    fn = tc.get("function", {})
    tool_name = fn.get("name", "")
    try:
        arguments = json.loads(fn.get("arguments", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    cfg = resolve_group_config(ctx, "G28_ccr")
    # G15 must not execute a CCR tool that G28 itself would refuse to run. This path ships
    # ENABLED by default while G28 does not, and it previously consulted neither the
    # group's `enabled` flag nor the store-availability guard — the only thing keeping it
    # dormant was that ctx.ccr_tools_injected is set solely by G28. That made a single
    # stray flag assignment enough to start dispatching into an unavailable store.
    if not ccr_available(cfg, ctx):
        fn["result"] = {"error": "CCR is not available"}
        logger.debug("[%s] G15 refused %s: CCR unavailable", ctx.request_id, tool_name)
        return
    ttl = cfg.get("ttl_seconds", 86400)
    # `prefix` is NOT optional here: it is what scopes the store to one tenant.
    result = await dispatch_mcp_tool(
        tool_name, arguments, ttl,
        prefix=getattr(ctx, "redis_prefix", ""),
        max_store_chars=int(cfg.get("max_store_chars", 200_000)),
    )
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
