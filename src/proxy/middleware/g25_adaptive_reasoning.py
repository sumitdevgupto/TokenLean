"""
G25 · Adaptive Reasoning
Stage: Request-side (just before G12 Reasoning Budget)
Saving: 30–70% reasoning tokens on low-complexity queries
Technique:
  Classifies the incoming request's complexity into LOW / MEDIUM / HIGH
  using configurable keyword heuristics, then writes
  `ctx.params["reasoning_effort"]` so G12 applies the correct budget.

  Only fires on reasoning-capable models (o1/o3/o4, Claude extended-thinking).
  On non-reasoning models the middleware is a transparent no-op.

  Complexity tiers:
    HIGH   — formal proofs, multi-step code, optimisation, "prove that", "derive"
    MEDIUM — explanations, comparisons, root-cause analysis, "explain why"
    LOW    — factual lookup, summarise, yes/no, direct recall

  The caller can force an effort level by setting
  `ctx.params["reasoning_effort"]` before this stage; G25 skips classification
  when the effort is already set (opt-out / override path).
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing

logger = logging.getLogger(__name__)
GROUP = "G25"

# ─── Default keyword sets ─────────────────────────────────────────────────────

_DEFAULT_HIGH_KEYWORDS = [
    r"\bprove\b", r"\bderive\b", r"\boptimis[ez]\b", r"\bsolve step[- ]by[- ]step\b",
    r"\bformal proof\b", r"\bmathematical(?:ly)?\b", r"\brefactor.*complex\b",
    r"\bdesign.*algorithm\b", r"\bcomplexity analysis\b", r"\btime complexity\b",
    r"\bNP[- ]hard\b", r"\bdynamic programming\b",
]

_DEFAULT_MEDIUM_KEYWORDS = [
    r"\bexplain\b", r"\bwhy\b", r"\bhow does\b", r"\bcompare\b",
    r"\banalyse\b", r"\banalyze\b", r"\bdiagnose\b", r"\broot[- ]cause\b",
    r"\bdebug\b", r"\btroubleshoot\b", r"\bimplications?\b", r"\btrade[- ]off\b",
    r"\bpros? and cons?\b",
]

_DEFAULT_LOW_KEYWORDS = [
    r"\bwhat is\b", r"\bwho is\b", r"\bwhen (?:was|did|is)\b", r"\bwhere is\b",
    r"\blist\b", r"\bsummarise\b", r"\bsummarize\b", r"\btranslate\b",
    r"\bdefine\b", r"\byes or no\b", r"\btrue or false\b",
]

def _build_patterns(keywords: List[str]) -> List[re.Pattern]:
    return [re.compile(kw, re.IGNORECASE) for kw in keywords]


def _extract_user_text(messages: List[Dict]) -> str:
    """Concatenate user and system message content for complexity scoring."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        if role in ("user", "system"):
            content = m.get("content", "")
            if isinstance(content, str):
                parts.append(content)
    return "\n".join(parts)


def _classify_complexity(
    text: str,
    high_patterns: List[re.Pattern],
    medium_patterns: List[re.Pattern],
    low_patterns: List[re.Pattern],
) -> Tuple[str, str]:
    """Return (effort_level, reason) for the given request text.

    Priority: HIGH > MEDIUM > LOW > MEDIUM (default when nothing matches).
    """
    for pat in high_patterns:
        m = pat.search(text)
        if m:
            return "high", f"high-complexity keyword: {m.group(0)!r}"

    for pat in medium_patterns:
        m = pat.search(text)
        if m:
            return "medium", f"medium-complexity keyword: {m.group(0)!r}"

    for pat in low_patterns:
        m = pat.search(text)
        if m:
            return "low", f"low-complexity keyword: {m.group(0)!r}"

    return "medium", "no keyword match — defaulting to medium"


def _is_reasoning_model(model: str, extra_prefixes: List[str], adapter=None) -> bool:
    """Return True if the model supports reasoning-budget injection.

    Delegates to ctx.provider_adapter.supports_reasoning() when available;
    falls back to config-driven extra_prefixes for unregistered models.
    """
    if adapter is not None:
        if adapter.supports_reasoning(model):
            return True
    return any(model.lower().startswith(p) for p in extra_prefixes)


class G25AdaptiveReasoning:
    """
    Classify request complexity and set reasoning_effort before G12.
    Reference: G25 in token_optimization_playbook_v7.md
    """

    def __init__(self) -> None:
        self._high_patterns: Optional[List[re.Pattern]] = None
        self._medium_patterns: Optional[List[re.Pattern]] = None
        self._low_patterns: Optional[List[re.Pattern]] = None
        self._patterns_cfg_hash: Optional[int] = None

    def _get_patterns(self, cfg: Dict[str, Any]) -> Tuple[
        List[re.Pattern], List[re.Pattern], List[re.Pattern]
    ]:
        """Lazily compile keyword patterns, re-compiling only when config changes."""
        high_kw = tuple(cfg.get("high_keywords", _DEFAULT_HIGH_KEYWORDS))
        med_kw = tuple(cfg.get("medium_keywords", _DEFAULT_MEDIUM_KEYWORDS))
        low_kw = tuple(cfg.get("low_keywords", _DEFAULT_LOW_KEYWORDS))
        cfg_hash = hash((high_kw, med_kw, low_kw))

        if cfg_hash != self._patterns_cfg_hash:
            self._high_patterns = _build_patterns(list(high_kw))
            self._medium_patterns = _build_patterns(list(med_kw))
            self._low_patterns = _build_patterns(list(low_kw))
            self._patterns_cfg_hash = cfg_hash

        return self._high_patterns, self._medium_patterns, self._low_patterns  # type: ignore[return-value]

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G25_adaptive_reasoning", {})
        if not cfg.get("enabled", False):
            return ctx

        # Skip if caller already set reasoning_effort (opt-out / override)
        if ctx.params.get("reasoning_effort"):
            logger.debug(
                "[%s] G25 skipped: reasoning_effort already set to %r",
                ctx.request_id, ctx.params["reasoning_effort"],
            )
            return ctx

        # Only act on reasoning-capable models
        extra_prefixes: List[str] = cfg.get("extra_reasoning_prefixes", [])
        if not _is_reasoning_model(ctx.routed_model, extra_prefixes, adapter=ctx.provider_adapter):
            logger.debug(
                "[%s] G25 skipped: %r is not a reasoning model",
                ctx.request_id, ctx.routed_model,
            )
            return ctx

        high_patterns, medium_patterns, low_patterns = self._get_patterns(cfg)
        user_text = _extract_user_text(ctx.messages)
        effort, reason = _classify_complexity(user_text, high_patterns, medium_patterns, low_patterns)

        # Apply per-complexity floor/ceiling from config
        effort_floor: str = cfg.get("effort_floor", "low")
        effort_ceiling: str = cfg.get("effort_ceiling", "high")
        _order = {"low": 0, "medium": 1, "high": 2}
        effort_idx = max(
            _order.get(effort_floor, 0),
            min(_order.get(effort_ceiling, 2), _order.get(effort, 1)),
        )
        effort = ["low", "medium", "high"][effort_idx]

        ctx.params["reasoning_effort"] = effort
        logger.debug(
            "[%s] G25 adaptive reasoning: effort=%s (%s)",
            ctx.request_id, effort, reason,
        )

        # Savings note: G25 can't measure reasoning token savings pre-LLM.
        # Record a zero-token-delta step so the group appears in observability.
        tokens = ctx.current_token_count
        ctx.savings.add_step(
            GROUP,
            f"Adaptive reasoning: effort={effort} ({reason})",
            tokens,
            tokens,
        )
        langfuse_tracing.add_span(
            ctx,
            name="G25-adaptive-reasoning",
            span_input={"model": ctx.routed_model, "text_length": len(user_text)},
            output={"effort": effort},
            metadata={"reason": reason, "effort": effort},
        )

        return ctx

    async def process_response(self, ctx: RequestContext, response: Dict[str, Any]) -> Dict[str, Any]:
        return response
