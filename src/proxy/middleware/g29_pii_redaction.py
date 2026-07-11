"""
G29 — PII Detection & Redaction

Detects PII (email, US SSN, credit card, phone, IPv4 — plus optional Presidio) in the
conversation and applies the per-tenant policy. Runs directly after G30 and before the
G04-bypass / G05-cache stage, OUTSIDE the `ctx.skip_groups` loops, so redaction happens
**before** any content-persisting stage sees the text — the G05 L1 key + L2 embedding,
G07 RAG query, G10 memory, and G28 CCR blocks only ever see redacted content.

Policy modes (per-tenant via `groups.G29_pii_redaction.mode`, deep-merged into ctx.config):
  * ``off``   — passthrough; the detector does not run.
  * ``flag``  — detect + record (count, entity types, metric, PII-free audit); never
                mutate the messages. **Default** (non-mutating → reproducible).
  * ``mask``  — replace each PII span in place. Reversible by default: the model sees a
                numbered placeholder (`[PII:EMAIL:1]`) so it can still reason about the
                fields, and the non-streaming response restores them for the data owner.
  * ``block`` — refuse the request (content-filter 200) if any PII is present.

Escape hatches (mask mode): the raw ``ctx.params["rag_query"]`` snapshot (taken in
main.py before the pipeline) and the ``ctx.original_messages`` copy are masked with the
same detector so no raw PII leaks past this stage into retrieval or trace/audit
consumers. Those copies are never echoed to the model, so their masked form is discarded
(not added to the reversible vault or the redaction count — the canonical count comes
from ``ctx.messages`` only, to avoid double-counting the same span across copies).

Streaming caveat: ``_stream_response`` bypasses the response pipeline, so response-side
redaction (masking model-generated PII / restoring placeholders) is **non-streaming
only** in this version; request-side redaction applies to every request. Metrics/audit
carry entity TYPES + counts only — never the matched value.

This middleware is a process singleton shared across concurrent requests, so it holds
NO per-request state — every count/vault is a local threaded through return values.

Reference: #2 in the TokenLean vs OmniRoute commercial-gap roadmap.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from guardrails import content_filter_response
from guardrails.pii import PiiDetector, mask_matches, unmask_text

logger = logging.getLogger(__name__)
GROUP = "G29"

_VALID_MODES = ("off", "flag", "mask", "block")
_DEFAULT_SCAN_ROLES = ("user", "assistant", "tool")

# (counts-by-entity-type, reversible-vault)
_Found = Tuple[Dict[str, int], Dict[str, str]]


class G29PiiRedaction:
    """Apply the PII redaction policy to each request (and non-streaming response)."""

    def __init__(self) -> None:
        # (sig, PiiDetector) held in ONE attribute so a concurrent rebuild swaps it
        # atomically (GIL) — a snapshot read can never pair a stale sig with another
        # config's detector, which would silently under-mask (a PII leak). See F1.
        self._detector_cache: Optional[tuple] = None

    def _config(self, ctx: RequestContext) -> Dict[str, Any]:
        return ctx.config.get("groups", {}).get("G29_pii_redaction", {}) or {}

    def _mode(self, cfg: Dict[str, Any]) -> str:
        mode = str(cfg.get("mode", "flag")).lower()
        return mode if mode in _VALID_MODES else "flag"

    def _get_detector(self, cfg: Dict[str, Any]) -> PiiDetector:
        entities = cfg.get("entities") or None
        use_presidio = bool(cfg.get("use_presidio", False))
        sig = (tuple(entities) if entities else None, use_presidio)
        cache = self._detector_cache            # single atomic read (no torn pair)
        if cache is not None and cache[0] == sig:
            return cache[1]
        det = PiiDetector(entities=entities, use_presidio=use_presidio)
        self._detector_cache = (sig, det)       # single atomic swap
        return det                              # return the LOCAL, never self._…

    # ── Request path ──────────────────────────────────────────────────────────
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = self._config(ctx)
        if not cfg.get("enabled", True):
            return ctx
        mode = self._mode(cfg)
        if mode == "off":
            return ctx

        detector = self._get_detector(cfg)
        reversible = bool(cfg.get("reversible", True))
        scan_roles = set(cfg.get("scan_roles", _DEFAULT_SCAN_ROLES))
        do_mask = mode == "mask"

        counts: Dict[str, int] = {}
        vault: Dict[str, str] = {}
        for msg in ctx.messages:
            if msg.get("role") not in scan_roles:
                continue
            self._apply_to_message(msg, detector, do_mask, reversible, counts, vault)

        total = sum(counts.values())
        if total == 0:
            return ctx

        ctx.pii_action = mode
        _merge(ctx.pii_entities, sorted(counts))
        ctx.pii_redactions += total
        _emit_metric(ctx, counts, mode, cfg)

        if do_mask:
            # Masking makes the G05 cache key lossy — bypass the cache for this request
            # so a masked look-alike can never collide with (and be served) another
            # caller's PII-derived answer. See RequestContext.no_cache.
            ctx.no_cache = True
            # Escape hatches — scrub the raw retrieval snapshot + the original-message
            # copy with the SAME detector (masked form discarded; not counted/vaulted).
            self._mask_escape_hatches(ctx, detector, reversible)
            if reversible and vault:
                ctx.pii_vault.update(vault)
        elif mode == "block":
            ctx.security_blocked = True
            ctx.security_block_response = content_filter_response(
                ctx.request_id, ctx.routed_model or ctx.model,
                cfg.get("block_message",
                        "This request was blocked because it contained personal data "
                        "and was not sent to the model."),
            )

        logger.info(
            "[%s] G29 %s: %d PII span(s) across %s",
            ctx.request_id, mode, total, ",".join(sorted(counts)) or "-",
        )
        return ctx

    # ── Response path (NON-STREAMING only; see module docstring) ────────────────
    async def process_response(self, ctx: RequestContext, response: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self._config(ctx)
        if not cfg.get("enabled", True):
            return response
        mode = self._mode(cfg)
        if mode not in ("mask", "flag"):
            return response

        detector = self._get_detector(cfg)
        vault = getattr(ctx, "pii_vault", None)
        counts: Dict[str, int] = {}

        for choice in response.get("choices", []) or []:
            msg = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            matches = detector.detect(content)
            if matches:
                _count(counts, [m.entity_type for m in matches])
                if mode == "mask":
                    # Mask model-generated PII (irreversible — it isn't the caller's own
                    # data to restore). Vault placeholders are not PII-shaped, so this
                    # never re-masks a restored token.
                    content = mask_matches(content, matches, reversible=False).text
            if mode == "mask" and vault:
                # Restore the caller's own request PII that the model echoed back.
                content = unmask_text(content, vault)
            msg["content"] = content

        total = sum(counts.values())
        if total:
            _merge(ctx.pii_entities, sorted(counts))
            ctx.pii_redactions += total
            ctx.pii_action = ctx.pii_action or mode
            _emit_metric(ctx, counts, mode, cfg)
        return response

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _apply_to_message(
        self, msg: Dict[str, Any], detector: PiiDetector, do_mask: bool,
        reversible: bool, counts: Dict[str, int], vault: Dict[str, str],
    ) -> None:
        """Detect (and optionally mask) PII in every text slot of a message, in place.

        Handles both plain-string content and the OpenAI multimodal list-of-parts
        shape. Accumulates counts + vault into the caller-supplied dicts."""
        def _slot(container: Dict[str, Any], key: str) -> None:
            val = container.get(key)
            if isinstance(val, str) and val:
                new = self._scan_text(val, detector, do_mask, reversible, counts, vault)
                if do_mask and new is not None:
                    container[key] = new

        content = msg.get("content")
        if isinstance(content, str) and content:
            _slot(msg, "content")
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    _slot(part, "text")

        # Tool-call arguments are a JSON *string* that routinely echoes user PII in
        # agentic turns (e.g. {"to": "alice@x.com"}); scan them too. Placeholders like
        # [PII:EMAIL:1] are valid inside a JSON string value, so the JSON stays parseable.
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                _slot(fn, "arguments")
        fc = msg.get("function_call")           # legacy single-function-call shape
        if isinstance(fc, dict):
            _slot(fc, "arguments")

    @staticmethod
    def _scan_text(text, detector, do_mask, reversible, counts, vault) -> Optional[str]:
        matches = detector.detect(text)
        if not matches:
            return None
        _count(counts, [m.entity_type for m in matches])
        if not do_mask:
            return None
        res = mask_matches(text, matches, reversible=reversible)
        vault.update(res.vault)
        return res.text

    def _mask_escape_hatches(self, ctx, detector, reversible) -> None:
        """Scrub the copies that would otherwise leak raw PII past this stage.

        Masked in place, throwaway counts/vault — these copies are never echoed to the
        model, so their placeholder numbering is irrelevant and must not pollute the
        canonical count or the restore vault."""
        rq = ctx.params.get("rag_query")
        if isinstance(rq, str) and rq:
            ctx.params["rag_query"] = self._mask_copy(rq, detector, reversible)
        for msg in getattr(ctx, "original_messages", None) or []:
            if not isinstance(msg, dict):
                continue
            self._apply_to_message(msg, detector, do_mask=True, reversible=reversible,
                                   counts={}, vault={})

    @staticmethod
    def _mask_copy(text: str, detector: PiiDetector, reversible: bool) -> str:
        matches = detector.detect(text)
        return mask_matches(text, matches, reversible=reversible).text if matches else text


def _emit_metric(ctx, counts: Dict[str, int], mode: str, cfg: Dict[str, Any]) -> None:
    if not cfg.get("metrics_enabled", True):
        return
    try:
        from middleware.g18_observability import PII_REDACTIONS_TOTAL
        for etype, n in counts.items():
            PII_REDACTIONS_TOTAL.labels(
                tenant_id=getattr(ctx, "tenant_id", "default"),
                entity_type=etype, action=mode,
            ).inc(n)
    except Exception as exc:
        logger.debug("[%s] G29 metric emit failed: %s", getattr(ctx, "request_id", "?"), exc)


def _merge(dst: List[str], src) -> None:
    for x in src:
        if x not in dst:
            dst.append(x)


def _count(dst: Dict[str, int], src) -> None:
    for x in src:
        dst[x] = dst.get(x, 0) + 1
