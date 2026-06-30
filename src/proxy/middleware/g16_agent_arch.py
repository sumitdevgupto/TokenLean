"""
G16 · Agent Architecture
Stage: Across the Loop (companion guidance — not inline request modifier)
Saving: 5–20% per-agent context via real enforcement (truncation + tool pruning);
        20–60% achievable with full role-decomposition (advisory, manual follow-up)
Technique: Detect monolithic agent anti-patterns (role stacking, oversized context)
           and enforce hard limits — truncate oversized system prompts and prune
           excess tool definitions — recording the real token delta. Starter kit
           templates in src/templates/ provide LangGraph + Temporal OSS patterns
           for the larger, advisory-only role-decomposition gains.
"""
import json
import logging
from typing import Any, Dict, List

from middleware import RequestContext
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G16"

_MAX_SYSTEM_PROMPT_TOKENS = 800   # truncate above this
_MAX_TOOLS_COUNT = 10              # prune above this (role stacking signal)


def _truncate_to_tokens(text: str, max_tokens: int, model: str) -> str:
    """Truncate text to at most max_tokens, using the same char/4 ceiling as estimate_tokens."""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text, model) <= max_tokens:
        return text
    max_chars = max_tokens * 4
    truncated = text[:max_chars]
    while truncated and estimate_tokens(truncated, model) > max_tokens:
        truncated = truncated[:-50] if len(truncated) > 50 else truncated[:-1]
    return truncated


def _tools_tokens(tools: List[Dict[str, Any]], model: str) -> int:
    return sum(estimate_tokens(json.dumps(t), model) for t in tools)


class G16AgentArch:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G16_agent_arch", {})
        if not cfg.get("enabled", False):
            return ctx

        warnings: List[str] = []
        tools = ctx.params.get("tools", [])

        tokens_before = count_messages_tokens(ctx.messages, ctx.model) + _tools_tokens(tools, ctx.model)

        # Enforce oversized system prompt (role stacking) via truncation
        system_tokens = sum(
            count_messages_tokens([m], ctx.model)
            for m in ctx.messages
            if m.get("role") == "system"
        )
        max_sys = cfg.get("max_system_prompt_tokens", _MAX_SYSTEM_PROMPT_TOKENS)
        if system_tokens > max_sys:
            for m in ctx.messages:
                if m.get("role") == "system" and isinstance(m.get("content"), str):
                    overhead = count_messages_tokens([{"role": m["role"], "content": ""}], ctx.model)
                    content_budget = max(0, max_sys - overhead)
                    m["content"] = _truncate_to_tokens(m["content"], content_budget, ctx.model)
            warnings.append(
                f"System prompt {system_tokens}t > {max_sys}t threshold — "
                "truncated to fit (consider role decomposition, G16 one-role-one-agent)"
            )

        # Enforce excessive tools (monolith signal) via pruning
        max_tools = cfg.get("max_tools_per_agent", _MAX_TOOLS_COUNT)
        if len(tools) > max_tools:
            ctx.params["tools"] = tools[:max_tools]
            warnings.append(
                f"{len(tools)} tools loaded > {max_tools} threshold — "
                f"pruned to {max_tools} (consider sub-agent decomposition and "
                "intent-based tool pruning, G08)"
            )

        if warnings:
            ctx.params.setdefault("_token_opt_warnings", [])
            ctx.params["_token_opt_warnings"].extend(warnings)
            for w in warnings:
                logger.warning("[%s] G16 arch enforcement: %s", ctx.request_id, w)

            tokens_after = count_messages_tokens(ctx.messages, ctx.model) + _tools_tokens(
                ctx.params.get("tools", []), ctx.model
            )
            ctx.savings.add_step(
                GROUP,
                f"Arch enforcement: {len(warnings)} issue(s) mitigated",
                tokens_before,
                tokens_after,
            )

        return ctx
