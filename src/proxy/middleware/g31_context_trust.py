"""
G31 — Context-Trust Scan (indirect prompt-injection defence)

G30 scans the untrusted **user** prompt before optimisation spends tokens. But
retrieval (G07) and memory (G10) then APPEND new content — retrieved RAG documents
and stored memories — into the prompt as `system` / `tool` messages, AFTER G30 has
already run. A poisoned document in the vector store, or a poisoned stored memory,
is therefore injected into the model prompt with zero inspection. This is the classic
*indirect* / RAG prompt-injection path.

G31 closes it by re-running the same OSS-core `InjectionScanner` over the **assembled**
context — the roles that retrieval/memory write into (`system`, `tool`) — AFTER those
stages have run. It is the complement of G30 (which scans `user`).

Placement: runs right after the Stage-3 retrieval/memory/dedup block in
``pipeline.py``, and is **non-bypassable** — it is NOT gated by ``ctx.skip_groups``,
so G24 adaptive-bypass can never disable it. (Bypass / cache-hit traffic never reaches
this stage because it has no retrieval step and short-circuits earlier.)

Policy modes (per-tenant via ``groups.G31_context_trust.mode``):
  * ``allow`` — passthrough; the scanner does not run (zero overhead).
  * ``flag``  — scan; on a hit annotate + emit a metric + audit, but let the request
                through unchanged. **Default** (non-mutating, safe token accounting).
  * ``block`` — scan; on a hit short-circuit with a content-filter 200 (like G30 block).
  * ``strip`` — scan; DROP the offending injected message (or multimodal text part)
                and continue with the cleaned context. Mutating but non-blocking.

The engine lives in ``guardrails/injection.py`` (OSS core). This middleware only
applies policy + records observability; it carries attack *categories* + rule ids —
never raw content — into metrics/audit. The managed red-team ruleset feed (extra_rules)
is the commercial enrichment; the static default ruleset ships OSS.

Reference: G31 in the Input-Safety / Context-Quality / Output-Reliability plan.
"""
import logging
from typing import Any, Dict, List, Optional

from middleware import RequestContext
from guardrails import content_filter_response
from guardrails.injection import InjectionScanner, InjectionVerdict

logger = logging.getLogger(__name__)
GROUP = "G31"

_VALID_MODES = ("allow", "flag", "block", "strip")
_DEFAULT_SCAN_ROLES = ["system", "tool"]


class G31ContextTrust:
    """Apply the context-trust (indirect-injection) policy to injected context."""

    def __init__(self) -> None:
        # (sig, InjectionScanner) in ONE attribute so a concurrent config rebuild swaps
        # it atomically (GIL) — mirrors G30. Recompiling the ruleset per request is waste.
        self._scanner_cache: Optional[tuple] = None

    def _config(self, ctx: RequestContext) -> Dict[str, Any]:
        return ctx.config.get("groups", {}).get("G31_context_trust", {}) or {}

    def _get_scanner(self, cfg: Dict[str, Any]) -> InjectionScanner:
        threshold = float(cfg.get("threshold", 0.5))
        extra = cfg.get("extra_rules") or []
        extra_tuples = tuple(tuple(r) for r in extra if isinstance(r, (list, tuple)) and len(r) == 4)
        sig = (threshold, extra_tuples)
        cache = self._scanner_cache             # single atomic read (no torn pair)
        if cache is not None and cache[0] == sig:
            return cache[1]
        scanner = InjectionScanner(extra_rules=list(extra_tuples), threshold=threshold)
        self._scanner_cache = (sig, scanner)    # single atomic swap
        return scanner

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = self._config(ctx)
        if not cfg.get("enabled", True):
            return ctx
        # A prior stage may already have blocked (G29/G30); nothing more to do.
        if getattr(ctx, "security_blocked", False):
            return ctx

        mode = str(cfg.get("mode", "flag")).lower()
        if mode not in _VALID_MODES:
            mode = "flag"
        if mode == "allow":
            return ctx  # passthrough — scanner does not run

        scan_roles = set(cfg.get("scan_roles", _DEFAULT_SCAN_ROLES))
        scanner = self._get_scanner(cfg)

        if mode == "strip":
            verdict = self._scan_and_strip(scanner, ctx.messages, scan_roles)
        else:
            verdict = self._scan(scanner, ctx.messages, scan_roles)

        if not verdict.matched:
            return ctx

        ctx.context_trust_action = mode
        ctx.context_trust_categories = list(verdict.categories)
        self._emit_metric(ctx, verdict.categories, mode, cfg)
        logger.warning(
            "[%s] G31 %s context-injection: category=%s rule=%s",
            ctx.request_id, mode, verdict.category, verdict.rule_id,
        )

        if mode == "block":
            ctx.security_blocked = True
            ctx.security_block_response = self._refusal(ctx, cfg)
        return ctx

    def _scan(self, scanner: InjectionScanner, messages, scan_roles) -> InjectionVerdict:
        """Scan the in-scope roles; return the highest-severity match with the union of
        all attack categories seen. Non-mutating (flag / block)."""
        best: Optional[InjectionVerdict] = None
        categories: List[str] = []
        for msg in messages or []:
            if msg.get("role") not in scan_roles:
                continue
            for text in _iter_text(msg.get("content")):
                v = scanner.scan(text)
                if not v.matched:
                    continue
                for c in v.categories:
                    if c not in categories:
                        categories.append(c)
                if best is None or v.score > best.score:
                    best = v
        if best is None:
            return InjectionVerdict(matched=False)
        return InjectionVerdict(
            matched=True, category=best.category, rule_id=best.rule_id,
            score=best.score, categories=categories, evidence=best.evidence,
        )

    def _scan_and_strip(self, scanner: InjectionScanner, messages, scan_roles) -> InjectionVerdict:
        """Scan the in-scope roles and DROP offending injected content in place — remove
        a whole message whose string content trips, or the individual tripping text parts
        of a multimodal message. Returns the union verdict of everything stripped."""
        best: Optional[InjectionVerdict] = None
        categories: List[str] = []
        survivors: List[Dict[str, Any]] = []
        for msg in messages or []:
            if msg.get("role") not in scan_roles:
                survivors.append(msg)
                continue
            content = msg.get("content")
            if isinstance(content, str):
                v = scanner.scan(content) if content else InjectionVerdict(matched=False)
                if v.matched:
                    best, categories = _fold(best, categories, v)
                    continue  # drop the whole poisoned message
                survivors.append(msg)
            elif isinstance(content, list):
                kept_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text")
                        if isinstance(t, str) and t:
                            v = scanner.scan(t)
                            if v.matched:
                                best, categories = _fold(best, categories, v)
                                continue  # drop this text part
                    kept_parts.append(part)
                if kept_parts:
                    msg["content"] = kept_parts
                    survivors.append(msg)
                # else: message emptied by stripping → drop it entirely
            else:
                survivors.append(msg)

        if best is None:
            return InjectionVerdict(matched=False)
        # Mutate the caller's list in place (RequestContext contract: ctx.messages).
        messages[:] = survivors
        return InjectionVerdict(
            matched=True, category=best.category, rule_id=best.rule_id,
            score=best.score, categories=categories, evidence=best.evidence,
        )

    def _emit_metric(self, ctx, categories, mode, cfg) -> None:
        if not cfg.get("metrics_enabled", True):
            return
        try:
            from middleware.g18_observability import CONTEXT_TRUST_EVENTS_TOTAL
            for category in categories or ["unknown"]:
                CONTEXT_TRUST_EVENTS_TOTAL.labels(
                    tenant_id=getattr(ctx, "tenant_id", "default"),
                    category=category, action=mode,
                ).inc()
        except Exception as exc:  # never let metrics break the request
            logger.debug("[%s] G31 metric emit failed: %s", ctx.request_id, exc)

    def _refusal(self, ctx: RequestContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
        message = cfg.get(
            "block_message",
            "This request was blocked because retrieved context contained a "
            "prompt-injection attempt and was not sent to the model.",
        )
        return content_filter_response(ctx.request_id, ctx.routed_model or ctx.model, message)


def _fold(best: Optional[InjectionVerdict], categories: List[str], v: InjectionVerdict):
    """Accumulate a match into the running (best, categories) pair."""
    for c in v.categories:
        if c not in categories:
            categories.append(c)
    if best is None or v.score > best.score:
        best = v
    return best, categories


def _iter_text(content: Any):
    """Yield the text of a message whether content is a plain string or the OpenAI
    multimodal list-of-parts shape."""
    if isinstance(content, str):
        if content:
            yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str) and t:
                    yield t
