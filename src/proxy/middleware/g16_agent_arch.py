"""
G16 · Agent Architecture
Stage: Across the Loop (companion guidance — not inline request modifier)
Saving: 5–20% per-agent context via real enforcement (tool pruning always;
        system-prompt compaction only when the operator opts in, see
        system_prompt_overflow);
        20–60% achievable with full role-decomposition (advisory, manual follow-up)
Technique: Detect monolithic agent anti-patterns (role stacking, oversized context)
           and enforce hard limits — truncate oversized system prompts and prune
           excess tool definitions — recording the real token delta. Starter kit
           templates in src/templates/ provide LangGraph OSS patterns
           for the larger, advisory-only role-decomposition gains.
"""
import json
import logging
import re
from typing import Any, Dict, List

from middleware import RequestContext
from savings.calculator import count_messages_tokens, count_tools_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G16"

_MAX_SYSTEM_PROMPT_TOKENS = 4096  # truncate above this (fallback when the config key is absent; matches config.yaml.template)
_SYSTEM_PROMPT_OVERFLOW = "warn"  # warn | compact — what to do when the system prompt exceeds the cap
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


_ELISION = (
    "\n\n[… {n} token(s) omitted from the middle of this system prompt to fit the "
    "{cap}-token limit; the opening and closing instructions are intact …]\n\n"
)
_ELISION_PLAIN = " […] "


_BLOCK_SEPARATORS = ("\n\n", "\n", ". ")
_HARD_SLICE_CHARS = 2000


def _split_blocks(text: str, max_unit_chars: int = _HARD_SLICE_CHARS) -> List[str]:
    """Split into paragraph-then-line-then-sentence units, kept or dropped whole.

    Units are atomic, so their size is a floor on how precisely the budget can be filled.
    Paragraphs alone are too coarse: a 924-token paragraph cannot fit a 796-token budget, is
    dropped entire, and leaves 644 tokens of budget unspent — deleting far more of the
    customer's prompt than the cap requires. Refining all the way to sentences fixes that.

    It deliberately stops AT the sentence: splitting on words would fill the budget marginally
    better while starting the surviving tail mid-sentence, which is the half-written text this
    whole change exists to avoid. Only a unit with no usable boundary left — one still longer
    than `max_unit_chars` after sentence splitting — is hard-sliced. That bound is kept
    generous relative to the budget so ordinary sentences are never cut, while an unbroken run
    (a pasted blob, minified text) is still divided finely enough to be placeable.
    """
    units = [text]
    for sep in _BLOCK_SEPARATORS:
        refined: List[str] = []
        for u in units:
            if sep in u:
                parts = u.split(sep)
                refined.extend([p + sep for p in parts[:-1]] + [parts[-1]])
            else:
                refined.append(u)
        units = refined
    out: List[str] = []
    for u in units:
        while len(u) > max_unit_chars:
            out.append(u[:max_unit_chars])
            u = u[max_unit_chars:]
        if u:
            out.append(u)
    return out


def _head_tail_chars(text: str, max_tokens: int, model: str) -> str:
    """Character-level both-ends keep, for a prompt with no paragraph or line structure."""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(_ELISION_PLAIN, model) * 2 >= max_tokens:
        # Too small to split usefully: a marker that eats half the budget leaves a fragment at
        # each end and no instruction at either. Keep the END, where policy lives — the
        # opposite of what the tail cut this replaces would do.
        out = text[-max_tokens * 4:]
        while out and estimate_tokens(out, model) > max_tokens:
            out = out[50:] if len(out) > 50 else out[1:]
        return out
    keep = max_tokens * 4
    while keep > 0:
        half = keep // 2
        tail_len = keep - half
        if half + tail_len > len(text):
            # `keep` is a CHARACTER budget derived from a TOKEN budget, and dense text can run
            # well under 4 chars/token — so the two slices can overlap and duplicate content
            # instead of eliding any. Clamp them to disjoint halves; the loop shrinks `keep`
            # until the result genuinely fits.
            half = len(text) // 2
            tail_len = len(text) - half
        out = text[:half] + _ELISION_PLAIN + text[len(text) - tail_len:]
        if estimate_tokens(out, model) <= max_tokens:
            return out
        keep -= 50 if keep > 50 else 1
    return ""


def _compact_to_tokens(text: str, max_tokens: int, model: str) -> str:
    """Fit `text` into `max_tokens` by dropping its MIDDLE, keeping both ends.

    This replaces a straight tail cut (pre-2026-09-06). The END of a system prompt is where
    operating policy lives — closure rules, escalation-audit rules, "never include raw
    credentials" — so cutting the tail deletes exactly the instructions that most need to
    survive, while the OPENING carries the role the model needs to behave at all. Keeping both
    ends costs the same tokens as keeping one, and the elision marker tells the model something
    was removed instead of letting it read a truncated policy as a complete one.

    Deterministic: cuts land on paragraph (then line) boundaries, so no sentence is left
    half-written; a prompt with no such boundaries falls back to a character-level both-ends
    keep. Provider-agnostic — no model call, no provider strings.
    """
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text, model) <= max_tokens:
        return text

    marker_cost = estimate_tokens(_ELISION.format(n=len(text), cap=max_tokens), model)
    budget = max_tokens - marker_cost
    if budget <= 0:
        return _head_tail_chars(text, max_tokens, model)

    # Keep the no-boundary fallback slice generous next to the budget: fine enough that a
    # blob is placeable, coarse enough that a normal sentence is never cut in half.
    blocks = _split_blocks(text, max_unit_chars=max(400, budget * 2))
    if len(blocks) < 3:
        return _head_tail_chars(text, max_tokens, model)

    head: List[str] = []
    tail: List[str] = []
    used = 0
    i, j = 0, len(blocks) - 1
    prefer_head = True
    stalled = 0
    # Alternate ends so both survive without a magic head/tail ratio. Terminates: each pass
    # either consumes a block or increments `stalled`, and `stalled` resets only on progress.
    while i <= j and stalled < 2:
        idx = i if prefer_head else j
        cost = estimate_tokens(blocks[idx], model)
        if used + cost <= budget:
            used += cost
            if prefer_head:
                head.append(blocks[idx])
                i += 1
            else:
                tail.append(blocks[idx])
                j -= 1
            stalled = 0
        else:
            stalled += 1
        prefer_head = not prefer_head

    if not head and not tail:
        return _head_tail_chars(text, max_tokens, model)

    dropped = estimate_tokens("".join(blocks[i:j + 1]), model)
    return "".join(head) + _ELISION.format(n=dropped, cap=max_tokens) + "".join(reversed(tail))


def _tools_tokens(tools: List[Dict[str, Any]], model: str) -> int:
    # Delegate to the shared estimator (packed-signature form). Counting raw
    # json.dumps here overstated tool tokens ~2.4x vs provider billing, inflating
    # G16's recorded per-step savings (found via DS13, 2026-08-08).
    return count_tools_tokens(tools, model)


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
        overflow = str(cfg.get("system_prompt_overflow", _SYSTEM_PROMPT_OVERFLOW)).strip().lower()
        if overflow not in ("warn", "compact"):
            logger.warning(
                "[%s] G16 unknown system_prompt_overflow=%r — leaving the prompt intact",
                ctx.request_id, overflow,
            )
            overflow = "warn"
        if system_tokens > max_sys:
            compactable = [
                m for m in ctx.messages
                if m.get("role") == "system" and isinstance(m.get("content"), str)
            ]
            if overflow == "compact" and not compactable:
                # Every system message is multimodal (list content), which this group does not
                # rewrite. Say so, rather than reporting a compaction that did not happen.
                overflow = "warn"
            if overflow == "compact":
                # Share the cap across the system messages in proportion to their size. Giving
                # each message the FULL cap (pre-2026-09-06) enforced nothing whenever the
                # prompt was split: three 2,006t blocks summed past a 4,096t cap, tripped the
                # threshold, and were returned byte-identical — while the warning still
                # said "truncated to fit". Same customer, same tokens, opposite treatment
                # depending only on how many system messages they happened to write.
                sys_msgs = compactable
                sizes = [count_messages_tokens([m], ctx.model) for m in sys_msgs]
                total = sum(sizes) or 1
                for m, size in zip(sys_msgs, sizes):
                    overhead = count_messages_tokens([{"role": m["role"], "content": ""}], ctx.model)
                    budget = max(0, int(max_sys * (size / total)) - overhead)
                    m["content"] = _compact_to_tokens(m["content"], budget, ctx.model)
                warnings.append(
                    f"System prompt {system_tokens}t > {max_sys}t threshold — compacted "
                    "to fit by dropping the middle and keeping both ends (consider role "
                    "decomposition, G16 one-role-one-agent)"
                )
            else:
                warnings.append(
                    f"System prompt {system_tokens}t > {max_sys}t threshold — left intact. "
                    "Set groups.G16_agent_arch.system_prompt_overflow: compact to enforce the "
                    "cap, or decompose the role (G16 one-role-one-agent)"
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
