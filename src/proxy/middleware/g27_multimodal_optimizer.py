"""
G27 · Multimodal Image Optimizer — RESERVED SLOT, no image transform ships
Stage: Into the LLM (after G01 compression, before G07 retrieval)
Saving: none. This stage is a deliberate no-op and records no savings step.

Why there is no image transform here (2026-09-07):
  This proxy's token accounting does not measure image content at all.
  ``savings.calculator.count_messages_tokens`` sums only content parts whose
  ``type`` is ``"text"``; an ``image_url`` part contributes nothing to
  ``baseline_tokens`` or to ``final_tokens_sent``. No pricing entry prices an
  image and no provider adapter models one. A byte-level image optimisation
  therefore cannot move any number this proxy measures or bills, while
  re-encoding is a lossy change to the caller's own payload.

  The previous implementation delegated to a third-party byte-level image
  compressor and, on any byte reduction, would have recorded ``bytes // 4`` as a
  token saving — a quantity on a scale that appears nowhere else in the ledger,
  which would have reached ``usage_events.group_savings`` and from there the
  adaptive-bypass learning signal. Latent rather than observed: the lever
  returned early when ``bytes_after >= bytes_before``, and the installed build
  returned the probe JPEG byte-identical, so no such step was ever produced on
  that evidence. The dependency and that accounting were both removed; nothing is
  substituted, because a substitute would have the same problem.

  A genuine multimodal optimisation needs three things this repo does not have:
  an image-token model, image-aware pricing, and a vision quality gate. Until
  those exist, a saving claimed here could be neither measured honestly nor
  shown to be lossless, so the stage stays reserved rather than pretending.

  The stage is deliberately KEPT in the pipeline so the slot, its config key and
  its portal toggle stay stable for that future work.

  Config key: G27_multimodal
    enabled: ships false. When true this stage still returns the request
             unchanged and records no savings step.
"""
import logging
from typing import Any, Dict

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G27"


class G27MultimodalOptimizer:
    """
    Reserved multimodal stage — returns the request unchanged, records no savings.
    Reference: G27 in token_optimization_playbook_v7.md
    """

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G27_multimodal", {})
        if not cfg.get("enabled", False):
            return ctx
        # Enabled, but there is nothing to apply: see the module docstring. Messages are
        # returned untouched and no savings step is recorded, so a tenant that has this
        # group switched on sees a byte-identical request and an honest empty ledger.
        logger.debug(
            "[%s] G27 enabled, but no image transform ships in this release — "
            "request passed through unchanged",
            ctx.request_id,
        )
        return ctx

    async def process_response(self, ctx: RequestContext, response: Dict[str, Any]) -> Dict[str, Any]:
        return response
