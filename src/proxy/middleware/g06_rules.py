"""
G06 · Declarative Routing Rules — OSS-core engine.

Per-tenant, deterministic, in-proxy routing rules evaluated BETWEEN the caller's
per-request ``x_complexity`` override and the classifier dispatch inside
``G06Routing.process_request``. Rules are pure functions over the request
(keywords / regex / token-size / requested-model / tools / header-tags /
user-id) — no LLM calls, no HTTP, no I/O — so evaluation adds zero latency and is
safe on the hot path.

Design invariants (see the plan `check-g6-routing-logic-zany-leaf.md`):

* **Default OFF / byte-identical.** An empty or absent ``rules`` list is a no-op;
  ``match_for_ctx`` returns ``None`` and G06 falls through to its classifier
  exactly as before. This protects the published 54.1% savings baseline.
* **Never mutate the config.** When a tenant has no per-tenant overrides,
  ``ctx.config`` *is* the process-shared ``get_config()`` dict. ``effective_cfg``
  copies before applying knob overrides and ``normalize_rules`` uses ``sorted``
  (never ``list.sort``), so the shared config/rules list is never reordered or
  edited in place.
* **First match wins**, rules ordered by ``priority`` descending (stable on
  ties, preserving authoring order).
* **Fail safe.** A malformed rule, an invalid/over-long regex, or a bad field
  value can only make a matcher *fail to match* — never crash, never
  match-everything.

* **Time-limited patterns.** Rule regexes are tenant-authored and run on the event
  loop every tenant shares, so one catastrophic pattern (``(a|aa)+c`` on a run of
  "a"s backtracks exponentially) could freeze a worker. Searches use the ``regex``
  module, which the proxy pins, with one time budget (``MATCH_BUDGET_SECONDS``) for
  all of a request's pattern searches; a search that runs out counts as no match.

Reference: G06 in AGENTS.md. This module is intentionally dependency-free
(stdlib only, plus ``regex`` when it is installed) so the commercial portal dry-run
tester and the run-readiness probe can import it without pulling in the middleware
stack. Without ``regex`` the stdlib engine is used, with no time limit.
"""
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # time-limited searches; pinned in src/proxy/requirements.txt
    import regex as _engine
except ImportError:  # pragma: no cover - tooling without the proxy's dependencies
    _engine = None

logger = logging.getLogger(__name__)

# Knob keys a rule may override for the traffic it matches. Every key here is
# read at the TOP LEVEL of the G06 config dict — by ``_select_from_tier``
# (strategy / strategy_weights / canary_pct), the cost-floor guard
# (max_escalation_cost_usd / expected_output_tokens_estimate), or the cascade
# classifier (cascade_confidence_threshold) — so a shallow copy in
# ``effective_cfg`` fully isolates the override. (``least_latency`` EWMA alpha is
# deliberately NOT here: it is a call-site default of ``record_model_latency``,
# not a cfg read, so a per-rule override would be inert.)
RULE_OVERRIDE_KEYS = frozenset({
    "strategy",
    "strategy_weights",
    "canary_pct",
    "max_escalation_cost_usd",
    "cascade_confidence_threshold",
    "expected_output_tokens_estimate",
})

VALID_TIERS = ("simple", "medium", "complex")

# Caps (defense-in-depth; the portal validator enforces the same on save, but
# config-authored rules bypass the portal so the engine must self-protect).
MAX_RULES = 100
MAX_PATTERN_LEN = 512          # per-pattern char cap → ReDoS surface bound
MAX_MATCH_TEXT_CHARS = 20_000  # truncate the joined prompt before regex
# All of one request's rule-pattern searches together; an ordinary search over 20k
# chars takes well under a millisecond, so only a runaway pattern ever reaches this.
MATCH_BUDGET_SECONDS = 0.05
_PATTERN_CACHE_CAP = 4096
_TIMEOUT_LOG_EVERY_SECONDS = 60.0
_last_timeout_log: Dict[str, float] = {}

# Compiled-pattern cache. None is a VALID cached value (invalid/over-long regex),
# so a distinct sentinel marks a true cache miss.
_MISS = object()
_PATTERN_CACHE: Dict[str, Any] = {}

__all__ = [
    "RULE_OVERRIDE_KEYS",
    "VALID_TIERS",
    "MAX_RULES",
    "MAX_PATTERN_LEN",
    "MATCH_BUDGET_SECONDS",
    "normalize_rules",
    "rule_matches",
    "evaluate_rules",
    "match_for_ctx",
    "effective_cfg",
    "has_nested_quantifier",
]


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce to int, tolerating bad config without raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compile_pattern(pattern: str) -> Optional["re.Pattern"]:
    """Compile a rule regex (IGNORECASE), cached. Over-length or invalid patterns
    return ``None`` (cached) and log once — an unusable pattern simply cannot
    contribute a match; it never raises and never matches everything."""
    if not isinstance(pattern, str) or not pattern:
        return None
    cached = _PATTERN_CACHE.get(pattern, _MISS)
    if cached is not _MISS:
        return cached
    compiled: Optional[re.Pattern] = None
    if len(pattern) > MAX_PATTERN_LEN:
        logger.warning(
            "G06 rules: regex over %d chars skipped (len=%d)", MAX_PATTERN_LEN, len(pattern)
        )
    else:
        try:
            if _engine is not None:
                compiled = _engine.compile(pattern, _engine.IGNORECASE)
            else:
                compiled = re.compile(pattern, re.IGNORECASE)
        except Exception as exc:  # re.error / regex.error: an unusable pattern never matches
            logger.warning("G06 rules: invalid regex %r skipped: %s", pattern[:80], exc)
            compiled = None
    if len(_PATTERN_CACHE) >= _PATTERN_CACHE_CAP:
        _PATTERN_CACHE.clear()
    _PATTERN_CACHE[pattern] = compiled
    return compiled


class _Budget:
    """What is left of one request's pattern-search time. The first search that runs out
    spends all of it, so every later pattern counts as no match without running."""

    __slots__ = ("deadline",)

    def __init__(self, seconds: float = MATCH_BUDGET_SECONDS) -> None:
        self.deadline = time.perf_counter() + seconds

    def remaining(self) -> float:
        return self.deadline - time.perf_counter()

    def spend(self) -> None:
        self.deadline = float("-inf")


def _search(rx, pattern: str, text: str, budget: "_Budget",
            on_timeout: Optional[Callable[[str], None]]) -> Optional[bool]:
    """``rx.search(text)`` within what is left of the request's budget: True/False, or
    None when the budget ran out (then the pattern counts as no match)."""
    if _engine is None:
        return rx.search(text) is not None
    remaining = budget.remaining()
    try:
        if remaining <= 0:
            raise TimeoutError
        return rx.search(text, timeout=remaining) is not None
    except TimeoutError:
        budget.spend()
        now = time.monotonic()
        if now - _last_timeout_log.get(pattern, float("-inf")) >= _TIMEOUT_LOG_EVERY_SECONDS:
            _last_timeout_log[pattern] = now
            logger.warning("G06 rules: pattern %r ran out of its %d ms budget on a %d-char "
                           "prompt and counts as no match", pattern[:80],
                           int(MATCH_BUDGET_SECONDS * 1000), len(text))
        if on_timeout is not None:
            try:
                on_timeout(pattern)
            except Exception as exc:  # noqa: BLE001 — a metrics hook never breaks routing
                logger.debug("G06 rules: timeout hook failed: %s", exc)
        return None


def has_nested_quantifier(pattern: str) -> bool:
    """True when an unbounded repeat (``*``, ``+``, ``{n,}``) contains another repeat
    that can run more than once — ``(a+)+``, ``(\\w+\\s?)*``: the classic shape whose
    backtracking grows exponentially. The portal refuses it on save; the engine still
    runs any pattern, time-limited, which also covers what this does not catch (such as
    ``(a|aa)+``). Fixed counts (``(\\w+\\s){3}``) and optional items (``(a\\s?)+``) pass.
    An unparseable pattern returns False (compiling it reports the real error)."""
    try:
        from re import _parser as parser  # the stdlib's own regex parser (3.11+)
    except ImportError:  # pragma: no cover - older Pythons
        import sre_parse as parser
    try:
        parsed = parser.parse(pattern)
    except Exception:
        return False
    repeats = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
    unbounded = getattr(parser, "MAXREPEAT", 2 ** 32 - 1)

    def walk(items, inside_unbounded: bool) -> bool:
        for op, av in items:
            name = str(op)
            if name in repeats:
                lo, hi, sub = av
                if inside_unbounded and lo != hi and hi != 1:
                    return True
                if walk(sub, inside_unbounded or hi == unbounded):
                    return True
            elif name == "SUBPATTERN":
                if walk(av[-1], inside_unbounded):
                    return True
            elif name == "BRANCH":
                if any(walk(alt, inside_unbounded) for alt in av[1]):
                    return True
            elif name in ("ASSERT", "ASSERT_NOT"):
                if walk(av[1], inside_unbounded):
                    return True
            elif name == "ATOMIC_GROUP":
                if walk(av, inside_unbounded):
                    return True
        return False

    return walk(parsed, False)


def _priority(rule: Dict[str, Any]) -> int:
    return _as_int(rule.get("priority", 0), 0)


def normalize_rules(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the enabled rules from ``cfg['rules']`` ordered by priority desc.

    Drops non-dict entries and ``enabled: false`` rules, caps at ``MAX_RULES``
    (keeping the highest-priority ones), and NEVER mutates the input list — the
    ``rules`` list may be the shared base-config object when the tenant has no
    override, so this uses ``sorted`` (stable) rather than ``list.sort``.
    """
    if not isinstance(cfg, dict):
        return []
    raw = cfg.get("rules")
    if not raw or not isinstance(raw, list):
        return []
    rules = [r for r in raw if isinstance(r, dict) and r.get("enabled", True)]
    ordered = sorted(rules, key=lambda r: -_priority(r))
    if len(ordered) > MAX_RULES:
        logger.warning(
            "G06 rules: %d enabled rules exceeds cap %d; using top %d by priority",
            len(ordered), MAX_RULES, MAX_RULES,
        )
        ordered = ordered[:MAX_RULES]
    return ordered


def rule_matches(
    rule: Dict[str, Any],
    *,
    text: str,
    prompt_tokens: int,
    model: str,
    has_tools: bool,
    params: Dict[str, Any],
    user_id: str,
    budget: Optional[_Budget] = None,
    on_timeout: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Evaluate one rule's ``match`` block against the request primitives.

    Every PRESENT matcher key must pass (AND); within a value list it is any-of.
    Returns ``(matched, failed_field)`` — ``failed_field`` names the first matcher
    that failed (for the dry-run explain trace) and is ``""`` on a full match.
    Pure function on primitives so the portal tester can call it directly.
    ``budget`` caps the pattern searches (shared across a request's rules by
    ``evaluate_rules``); unset, this rule gets ``MATCH_BUDGET_SECONDS`` of its own.
    ``on_timeout(pattern)`` hears of each pattern stopped by the budget.
    """
    match = rule.get("match")
    if not isinstance(match, dict) or not match:
        # A rule with no matchers would match every request — refuse it defensively
        # (the portal validator rejects this on save with a 422).
        return False, "match"

    # Content — keywords: any-of, case-insensitive substring.
    keywords = match.get("keywords")
    if keywords:
        low = text.lower()
        if not any(isinstance(k, str) and k and k.lower() in low for k in keywords):
            return False, "keywords"

    # Content — patterns: any-of regex search over the (truncated) prompt text, within
    # the request's time budget; a pattern that runs out of it counts as no match.
    patterns = match.get("patterns")
    if patterns:
        if budget is None:
            budget = _Budget()
        hit, timed_out = False, False
        for pat in patterns:
            rx = _compile_pattern(pat)
            if rx is None:
                continue
            found = _search(rx, pat, text, budget, on_timeout)
            if found:
                hit = True
                break
            timed_out = timed_out or found is None
        if not hit:
            return False, "patterns (timed out)" if timed_out else "patterns"

    # Size — token-count bounds (0/absent = no bound).
    min_tok = _as_int(match.get("min_prompt_tokens"), 0)
    if min_tok and prompt_tokens < min_tok:
        return False, "min_prompt_tokens"
    max_tok = _as_int(match.get("max_prompt_tokens"), 0)
    if max_tok and prompt_tokens > max_tok:
        return False, "max_prompt_tokens"

    # Metadata — requested model (exact, any-of).
    models = match.get("models")
    if models and model not in models:
        return False, "models"

    # Metadata — tools presence.
    if "has_tools" in match:
        if bool(has_tools) != bool(match.get("has_tools")):
            return False, "has_tools"

    # Metadata — header tags mapped to x_* params (AND across keys, any-of within).
    param_match = match.get("params")
    if isinstance(param_match, dict):
        for key, wanted in param_match.items():
            actual = params.get(key)
            wanted_list = wanted if isinstance(wanted, list) else [wanted]
            if not any(str(actual) == str(w) for w in wanted_list):
                return False, "params.{}".format(key)

    # Metadata — caller user id (X-User-ID → ctx.user_id, NOT a param).
    user_ids = match.get("user_ids")
    if user_ids and (user_id or "") not in user_ids:
        return False, "user_ids"

    return True, ""


def evaluate_rules(
    rules: List[Dict[str, Any]],
    *,
    text: str,
    prompt_tokens: int,
    model: str,
    has_tools: bool,
    params: Dict[str, Any],
    user_id: str,
    explain: bool = False,
    on_timeout: Optional[Callable[[str], None]] = None,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """First-match-wins evaluation over an already-normalized rule list.

    Returns ``(matched_rule_or_None, trace)``. ``trace`` is populated only when
    ``explain`` is true (one ``{id, matched, failed_on}`` row per rule up to and
    including the match) — used by the portal dry-run tester. All the rules' pattern
    searches share one ``MATCH_BUDGET_SECONDS`` budget, so no rule count can stretch
    the time a request holds the event loop.
    """
    trace: List[Dict[str, Any]] = []
    budget = _Budget()
    for rule in rules:
        matched, failed = rule_matches(
            rule,
            text=text,
            prompt_tokens=prompt_tokens,
            model=model,
            has_tools=has_tools,
            params=params,
            user_id=user_id,
            budget=budget,
            on_timeout=on_timeout,
        )
        if explain:
            trace.append(
                {"id": rule.get("id", "?"), "matched": matched, "failed_on": failed}
            )
        if matched:
            return rule, trace
    return None, trace


def _match_text(messages: List[Dict[str, Any]]) -> str:
    """Join the non-system string turns (fallback: all string turns), truncated to
    ``MAX_MATCH_TEXT_CHARS``. Mirrors ``_classify_heuristic``'s extraction so rules
    see the same query text the heuristic classifier does — the system prompt is
    fixed infrastructure and excluded."""
    query = [
        m for m in messages
        if m.get("role") != "system" and isinstance(m.get("content"), str)
    ]
    if not query:
        query = [m for m in messages if isinstance(m.get("content"), str)]
    text = " ".join(m.get("content", "") for m in query)
    return text[:MAX_MATCH_TEXT_CHARS]


def match_for_ctx(ctx, cfg: Dict[str, Any],
                  on_timeout: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """Adapter: extract the request primitives from ``ctx`` and return the first
    matching rule (or ``None``). Cheap no-op when there are no rules. ``on_timeout``
    is told of each pattern that ran out of the time budget."""
    rules = normalize_rules(cfg)
    if not rules:
        return None
    params = ctx.params if isinstance(getattr(ctx, "params", None), dict) else {}
    # Every X-* header is a routing hint a rule may match, but ctx.params keeps only
    # TokenLean's own (it is persisted), so the rest arrive in ctx.routing_headers. A header
    # wins over a body field of the same name, as when headers were copied into params.
    headers = getattr(ctx, "routing_headers", None)
    hints = {**params, **headers} if isinstance(headers, dict) else params
    try:
        prompt_tokens = int(ctx.current_token_count)
    except Exception:  # pragma: no cover - token estimate must never break routing
        prompt_tokens = 0
    rule, _ = evaluate_rules(
        rules,
        text=_match_text(getattr(ctx, "messages", []) or []),
        prompt_tokens=prompt_tokens,
        model=getattr(ctx, "model", "") or "",
        has_tools=bool(params.get("tools")),
        params=hints,
        user_id=getattr(ctx, "user_id", "") or "",
        on_timeout=on_timeout,
    )
    return rule


def effective_cfg(cfg: Dict[str, Any], rule: Dict[str, Any]) -> Dict[str, Any]:
    """Return the G06 config the matched rule should route under.

    When the rule sets neither knob overrides nor ``allow_escalation`` the ORIGINAL
    ``cfg`` object is returned (identity — the zero-copy fast path that keeps the
    no-op case byte-identical). Otherwise a SHALLOW COPY is returned with the
    whitelisted overrides applied and, if ``action.allow_escalation`` is true, with
    ``allow_escalation_above_requested`` set so the downstream cost-floor guard lets
    this rule route above the caller's model. The input ``cfg`` is never mutated.
    """
    action = rule.get("action")
    if not isinstance(action, dict):
        return cfg
    overrides = action.get("overrides")
    overrides = overrides if isinstance(overrides, dict) else {}
    allow_escalation = bool(action.get("allow_escalation", False))
    applicable = {k: v for k, v in overrides.items() if k in RULE_OVERRIDE_KEYS}
    if not applicable and not allow_escalation:
        return cfg
    eff = dict(cfg)
    eff.update(applicable)
    if allow_escalation:
        eff["allow_escalation_above_requested"] = True
    return eff
