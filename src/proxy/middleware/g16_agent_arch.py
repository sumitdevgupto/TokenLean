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
import re
from typing import Any, Dict, List

from middleware import RequestContext
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G16"

_MAX_SYSTEM_PROMPT_TOKENS = 4096  # truncate above this (fallback when the config key is absent; matches config.yaml.template)
_MAX_TOOLS_COUNT = 20             # prune above this (role stacking signal; fallback matches config.yaml.template)
_TOOL_SELECTION_STRATEGY = "relevance"  # relevance | order — how to pick which tools to keep when over the cap
_NAME_TOKEN_WEIGHT = 3.0          # a tool-name token matching the request is the strongest relevance signal
_DESC_TOKEN_WEIGHT = 1.0          # description/parameter tokens matter, but less than the name

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set:
    """Lowercase alphanumeric tokens. `get_user_profile` → {get, user, profile}."""
    return set(_TOKEN_RE.findall(text.lower().replace("_", " ")))


def _message_text_tokens(messages: List[Dict[str, Any]]) -> set:
    """Union of tokens across all message contents (str or multimodal parts)."""
    toks: set = set()
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            toks |= _tokenize(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    toks |= _tokenize(part["text"])
    return toks


def _tool_fn(tool: Any) -> Dict[str, Any]:
    """Return the function spec whether the tool is OpenAI-nested ({'function': {...}}) or flat ({'name': ...})."""
    if isinstance(tool, dict):
        fn = tool.get("function")
        return fn if isinstance(fn, dict) else tool
    return {}


def _tool_relevance_score(tool: Any, query_tokens: set) -> float:
    """Lexical overlap between the request and a tool's name / description / parameter names.

    Deterministic and provider-agnostic — no model calls, no provider strings. Name-token
    matches are weighted highest because they are the clearest signal that the current turn
    is asking for this tool (e.g. 'get the user profile' → get_user_profile).
    """
    fn = _tool_fn(tool)
    name_tokens = _tokenize(str(fn.get("name", "")))
    desc_tokens = _tokenize(str(fn.get("description", "")))
    params = fn.get("parameters", {})
    props = params.get("properties", {}) if isinstance(params, dict) else {}
    param_tokens: set = set()
    if isinstance(props, dict):
        for pname, pspec in props.items():
            param_tokens |= _tokenize(str(pname))
            if isinstance(pspec, dict) and isinstance(pspec.get("description"), str):
                param_tokens |= _tokenize(pspec["description"])
    name_overlap = len(name_tokens & query_tokens)
    desc_overlap = len((desc_tokens | param_tokens) & query_tokens)
    return _NAME_TOKEN_WEIGHT * name_overlap + _DESC_TOKEN_WEIGHT * desc_overlap


def _select_tools(tools: List[Dict[str, Any]], messages: List[Dict[str, Any]], max_tools: int) -> List[Dict[str, Any]]:
    """Keep the `max_tools` tools most relevant to the request, preserving their original order.

    Ranking is stable: ties (including the all-equal case) fall back to original list order,
    so this degrades to the historical first-N behaviour when nothing is more relevant than
    anything else — but never silently drops a clearly-referenced tool just because it sat
    late in the list.
    """
    query_tokens = _message_text_tokens(messages)
    ranked = sorted(
        range(len(tools)),
        key=lambda i: (-_tool_relevance_score(tools[i], query_tokens), i),
    )
    keep = sorted(ranked[:max_tools])
    return [tools[i] for i in keep]


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

        # Enforce excessive tools (monolith signal) via pruning. When over the cap, keep the
        # tools most relevant to THIS request rather than the first N by list order — a blind
        # slice silently drops a tool the caller explicitly asked for if it sits late in the
        # list (e.g. get_user_profile at index 11 of 13). Falls back to order on any error.
        max_tools = cfg.get("max_tools_per_agent", _MAX_TOOLS_COUNT)
        if len(tools) > max_tools:
            strategy = cfg.get("tool_selection_strategy", _TOOL_SELECTION_STRATEGY)
            if strategy == "relevance":
                try:
                    ctx.params["tools"] = _select_tools(tools, ctx.messages, max_tools)
                except Exception as exc:  # never crash the request over tool ranking
                    logger.warning(
                        "[%s] G16 relevance tool-selection failed (%s) — falling back to order",
                        ctx.request_id, exc,
                    )
                    ctx.params["tools"] = tools[:max_tools]
            else:
                ctx.params["tools"] = tools[:max_tools]
            warnings.append(
                f"{len(tools)} tools loaded > {max_tools} threshold — "
                f"pruned to {max_tools} by {strategy} (consider sub-agent decomposition and "
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
