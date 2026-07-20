"""
G27 · Multimodal Image Optimizer
Stage: Into the LLM (after G01 compression, before G07 retrieval)
Saving: 40–70% of image token cost on repeated or compressible vision payloads
Technique:
  Detects image blocks in OpenAI-format messages (content list with
  {"type": "image_url", "image_url": {"url": "data:image/..."}}) and delegates
  compression to Headroom's image optimiser, which is *message-level*:
    headroom.image.compress_images(messages, provider) -> messages
  (ML-routed compression; tile optimisation; provider-aware re-encoding).

  Savings are measured by summing decoded data-URI bytes before vs after.

  No-op when:
    - G27 is disabled in config
    - headroom.image is not installed (it ships behind the [image] extra)
    - the messages contain no inline data: image_url blocks
    - compression would not reduce total image bytes

  Config key: G27_multimodal
    provider: override the provider hint passed to Headroom (default: adapter name)

  NOTE: the previous implementation called a non-existent top-level
  ``headroom.compress_image()`` (byte-level), so it was always a no-op. The real
  API lives under ``headroom.image`` and operates on the message list.
"""
import base64
import inspect
import logging
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing

logger = logging.getLogger(__name__)
GROUP = "G27"

# Optional tuning knobs forwarded to headroom.image.compress_images — passed ONLY when the
# installed function's signature accepts them.
_G27_TUNABLES = ("quality", "min_bytes")

# Last-seen (fn, supported-names) — `_compress_images_fn` is a module-level singleton resolved
# once at import and never reassigned in production, so its signature is invariant for the life
# of the process; a single-slot `is`-keyed cache avoids re-introspecting it on every request that
# fires G27, while still recomputing correctly when tests `patch.object` in a different callable.
_sig_cache_fn: Any = None
_sig_cache_supported: frozenset = frozenset()


def _supported_kwarg_names(fn) -> frozenset:
    global _sig_cache_fn, _sig_cache_supported
    if fn is _sig_cache_fn:
        return _sig_cache_supported
    try:
        params = inspect.signature(fn).parameters
        has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
        supported = frozenset(n for n in _G27_TUNABLES if has_var_kw or n in params)
    except (TypeError, ValueError):
        supported = frozenset()
    _sig_cache_fn, _sig_cache_supported = fn, supported
    return supported


def _supported_kwargs(fn, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of {quality, min_bytes} that `fn` accepts AND the tenant has configured.

    Returns {} when the optimiser takes neither (behaviour identical to the prior call), so the
    portal knobs are honoured where supported and harmlessly ignored otherwise."""
    out: Dict[str, Any] = {}
    for name in _supported_kwarg_names(fn):
        val = cfg.get(name)
        if val is not None:
            out[name] = val
    return out

# ─── Optional headroom.image import ───────────────────────────────────────────
_compress_images_fn = None
try:
    from headroom.image import compress_images as _compress_images_fn  # type: ignore
except (ImportError, AttributeError):
    pass


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_data_uri(uri: str) -> Tuple[Optional[str], Optional[bytes]]:
    """Split a data URI into (media_type, raw_bytes). Returns (None, None) on error."""
    try:
        if not uri.startswith("data:"):
            return None, None
        header, encoded = uri.split(",", 1)
        media_part = header[5:]  # strip "data:"
        media_type = media_part.replace(";base64", "") if ";base64" in media_part else media_part
        return media_type, base64.b64decode(encoded)
    except Exception:
        return None, None


def _count_image_bytes(messages: List[Dict[str, Any]]) -> int:
    """Sum decoded bytes of all inline data: image_url blocks across messages."""
    total = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            url_obj = block.get("image_url", {})
            uri = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
            if uri.startswith("data:"):
                _, raw = _parse_data_uri(uri)
                if raw:
                    total += len(raw)
    return total


def _resolve_provider(ctx: RequestContext, cfg: Dict[str, Any]) -> str:
    """Provider hint for Headroom: config override → adapter name → default provider."""
    from config_loader import get_default_provider
    return (
        cfg.get("provider")
        or getattr(getattr(ctx, "provider_adapter", None), "name", None)
        or get_default_provider()
    )


class G27MultimodalOptimizer:
    """
    Compress inline image blocks before the LLM call via Headroom's image optimiser.
    Reference: G27 in token_optimization_playbook_v7.md
    """

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G27_multimodal", {})
        if not cfg.get("enabled", False):
            return ctx

        if _compress_images_fn is None:
            logger.debug("[%s] G27 skipped: headroom.image not installed", ctx.request_id)
            return ctx

        bytes_before = _count_image_bytes(ctx.messages)
        if bytes_before == 0:
            return ctx  # no inline images to compress

        provider = _resolve_provider(ctx, cfg)
        # Pass the tenant's quality/min_bytes knobs THROUGH to the optimiser, but only the kwargs
        # its installed signature actually accepts (the [image] extra's API may vary) — otherwise
        # behave exactly as before. Prevents exposing dead portal knobs.
        extra_kwargs = _supported_kwargs(_compress_images_fn, cfg)
        try:
            new_messages = _compress_images_fn(ctx.messages, provider=provider, **extra_kwargs)
        except Exception as exc:
            logger.debug("[%s] G27 headroom.image compression failed: %s", ctx.request_id, exc)
            return ctx

        if not isinstance(new_messages, list):
            return ctx

        bytes_after = _count_image_bytes(new_messages)
        if bytes_after >= bytes_before:
            return ctx  # no improvement — leave messages untouched

        ctx.messages = new_messages
        pct = (1.0 - bytes_after / bytes_before) * 100
        ctx.savings.add_step(
            GROUP,
            f"G27 image compression: {bytes_before // 1024}KB → {bytes_after // 1024}KB ({pct:.1f}%)",
            bytes_before // 4,   # rough token estimate: ~4 bytes per token for images
            bytes_after // 4,
        )
        langfuse_tracing.add_span(
            ctx,
            name="G27-multimodal-optimizer",
            span_input={"bytes_before": bytes_before},
            output={"bytes_after": bytes_after},
            metadata={"pct_saved": round(pct, 1), "provider": provider},
        )
        logger.debug(
            "[%s] G27 compressed images: %dKB → %dKB (%.1f%%)",
            ctx.request_id, bytes_before // 1024, bytes_after // 1024, pct,
        )
        return ctx

    async def process_response(self, ctx: RequestContext, response: Dict[str, Any]) -> Dict[str, Any]:
        return response
