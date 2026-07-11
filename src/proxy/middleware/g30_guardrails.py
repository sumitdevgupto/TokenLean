"""
G30 — Prompt-Injection / Jailbreak Guardrails

Scans the user-supplied prompt for injection / jailbreak attempts and, depending on
the per-tenant policy, flags or blocks the request. Runs **early and unconditionally**
— inserted directly after G24 and before the G04-bypass / G05-cache stage, OUTSIDE the
`ctx.skip_groups` loops — so:

  * G24 adaptive-bypass can never disable it,
  * bypass / cache-hit traffic is still guarded, and
  * a malicious prompt is refused before any optimisation spends tokens on it.

Policy modes (per-tenant via `groups.G30_guardrails.mode`, deep-merged into ctx.config):
  * ``allow``  — passthrough; the scanner does not run (zero overhead).
  * ``flag``   — scan; on a hit annotate the request + emit a metric + write a
                 PII-free audit row, but let the request through. **Default.**
  * ``block``  — scan; on a hit short-circuit with a structured content-filter refusal
                 (HTTP 200, ``finish_reason: content_filter``), billed like a bypass.

The engine lives in ``guardrails/injection.py`` (OSS core). This middleware only
applies policy + records observability. It carries attack *categories* and rule ids —
never raw prompt text — into metrics/audit.

Reference: #3 in the TokenLean vs OmniRoute commercial-gap roadmap.
"""
import logging
from typing import Any, Dict, List, Optional

from middleware import RequestContext
from guardrails import content_filter_response
from guardrails.injection import InjectionScanner, InjectionVerdict

logger = logging.getLogger(__name__)
GROUP = "G30"

_VALID_MODES = ("allow", "flag", "block")


class G30Guardrails:
    """Apply the injection-guardrail policy to each request."""

    def __init__(self) -> None:
        # (sig, InjectionScanner) in ONE attribute so a concurrent rebuild swaps it
        # atomically (GIL) — a snapshot read can never return another config's scanner
        # (which would misjudge flag/block for this tenant). See F1. Rebuilt only on a
        # config change (hot-reload safe) — recompiling ~14 regexes per request is waste.
        self._scanner_cache: Optional[tuple] = None

    def _config(self, ctx: RequestContext) -> Dict[str, Any]:
        return ctx.config.get("groups", {}).get("G30_guardrails", {}) or {}

    def _get_scanner(self, cfg: Dict[str, Any]) -> InjectionScanner:
        threshold = float(cfg.get("threshold", 0.5))
        # extra_rules: list of [id, category, severity, pattern] (managed feed / operator).
        extra = cfg.get("extra_rules") or []
        extra_tuples = tuple(tuple(r) for r in extra if isinstance(r, (list, tuple)) and len(r) == 4)
        sig = (threshold, extra_tuples)
        cache = self._scanner_cache             # single atomic read (no torn pair)
        if cache is not None and cache[0] == sig:
            return cache[1]
        scanner = InjectionScanner(extra_rules=list(extra_tuples), threshold=threshold)
        self._scanner_cache = (sig, scanner)    # single atomic swap
        return scanner                          # return the LOCAL, never self._…

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = self._config(ctx)
        if not cfg.get("enabled", True):
            return ctx

        mode = str(cfg.get("mode", "flag")).lower()
        if mode not in _VALID_MODES:
            mode = "flag"
        if mode == "allow":
            return ctx  # passthrough — scanner does not run

        scan_roles = set(cfg.get("scan_roles", ["user"]))
        verdict = self._scan(self._get_scanner(cfg), ctx.messages, scan_roles)
        if not verdict.matched:
            return ctx

        ctx.guardrail_action = mode
        ctx.guardrail_categories = list(verdict.categories)
        self._emit_metric(ctx, verdict.categories, mode, cfg)
        logger.warning(
            "[%s] G30 %s injection: category=%s rule=%s",
            ctx.request_id, mode, verdict.category, verdict.rule_id,
        )

        if mode == "block":
            ctx.security_blocked = True
            ctx.security_block_response = self._refusal(ctx, cfg)
        return ctx

    def _scan(self, scanner: InjectionScanner, messages: List[Dict[str, Any]], scan_roles) -> InjectionVerdict:
        """Scan the in-scope roles; return the highest-severity match with the union
        of all attack categories seen across the request."""
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

    def _emit_metric(self, ctx, categories, mode, cfg) -> None:
        if not cfg.get("metrics_enabled", True):
            return
        try:
            from middleware.g18_observability import GUARDRAIL_EVENTS_TOTAL
            for category in categories or ["unknown"]:
                GUARDRAIL_EVENTS_TOTAL.labels(
                    tenant_id=getattr(ctx, "tenant_id", "default"),
                    category=category, action=mode,
                ).inc()
        except Exception as exc:  # never let metrics break the request
            logger.debug("[%s] G30 metric emit failed: %s", ctx.request_id, exc)

    def _refusal(self, ctx: RequestContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
        message = cfg.get(
            "block_message",
            "This request was blocked by the safety guardrails and was not sent to the model.",
        )
        return content_filter_response(ctx.request_id, ctx.routed_model or ctx.model, message)


def _iter_text(content: Any):
    """Yield the text of a message whether content is a plain string or the
    OpenAI multimodal list-of-parts shape."""
    if isinstance(content, str):
        if content:
            yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str) and t:
                    yield t
