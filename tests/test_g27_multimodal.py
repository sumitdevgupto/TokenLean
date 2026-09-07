"""Tests for G27 — Multimodal Optimiser, which is a RESERVED NO-OP.

G27 carries no image transform: see `src/proxy/middleware/g27_multimodal_optimizer.py`.
These tests pin that it stays a no-op — enabled or disabled, with or without images, and
with or without the removed knobs still sitting in a tenant's stored config.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import base64
import pytest
from datetime import datetime, timezone


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_ctx(messages=None, enabled=True, cfg_extra=None):
    from middleware import RequestContext
    from savings.models import SavingsRecord

    savings = SavingsRecord(
        request_id="req-g27",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=100,
    )
    ctx = RequestContext(
        request_id="req-g27",
        user_id="u1",
        original_messages=list(messages or []),
        messages=list(messages or []),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={"groups": {"G27_multimodal": {"enabled": enabled, **(cfg_extra or {})}}},
        savings=savings,
    )
    return ctx


def _make_jpeg_bytes(size: int = 8192) -> bytes:
    header = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * 12
    return header + b"\xFF" * (size - len(header))


def _data_uri(data: bytes, media_type: str = "image/jpeg") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


def _make_vision_message(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": _data_uri(image_bytes, media_type)}},
        ],
    }


# ─── G27 is a reserved no-op ─────────────────────────────────────────────────

class TestG27IsAReservedNoOp:
    """G27 ships as a reserved slot: whatever the config says, it must leave the request
    exactly as it found it and record nothing.

    The image lever it used to carry was removed on 2026-09-07 along with the third-party
    package behind it. The reason is in the module docstring and is worth restating here,
    because it is what these tests defend: this proxy's token accounting does not measure
    image content at all (``count_messages_tokens`` sums only ``type == "text"`` parts),
    so a byte-level image optimisation could not reduce a token the proxy measures or
    bills — but the old code would still have recorded ``bytes // 4`` as a saving, and that
    number would have reached ``usage_events.group_savings``. Latent, not observed: the
    lever returned early when ``bytes_after >= bytes_before`` and the installed build
    returned the probe JPEG byte-identical, so no such step was ever produced. A stage that
    cannot help must also not be able to claim to.
    """

    @pytest.mark.asyncio
    async def test_disabled_is_a_no_op(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        msgs = [{"role": "user", "content": "hi"}]
        ctx = _make_ctx(msgs, enabled=False)
        ctx = await G27MultimodalOptimizer().process_request(ctx)
        assert ctx.messages == msgs
        assert len(ctx.savings.step_savings) == 0

    @pytest.mark.asyncio
    async def test_enabled_leaves_a_vision_request_byte_identical(self):
        """The case that used to re-encode: enabled, with a real inline image."""
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import copy

        msgs = [_make_vision_message(_make_jpeg_bytes(8192))]
        before = copy.deepcopy(msgs)
        ctx = _make_ctx(msgs)
        ctx = await G27MultimodalOptimizer().process_request(ctx)
        assert ctx.messages == before

    @pytest.mark.asyncio
    async def test_enabled_records_no_savings_step(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        ctx = _make_ctx([_make_vision_message(_make_jpeg_bytes(8192))])
        ctx = await G27MultimodalOptimizer().process_request(ctx)
        assert ctx.savings.step_savings == []

    @pytest.mark.asyncio
    async def test_enabled_with_legacy_knobs_still_changes_nothing(self):
        """A tenant whose stored config still carries the removed quality/min_bytes/provider
        knobs must not crash and must not get different behaviour."""
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import copy

        msgs = [_make_vision_message(_make_jpeg_bytes(8192))]
        before = copy.deepcopy(msgs)
        ctx = _make_ctx(msgs, cfg_extra={"quality": 33, "min_bytes": 2048,
                                         "provider": "anthropic"})
        ctx = await G27MultimodalOptimizer().process_request(ctx)
        assert ctx.messages == before
        assert ctx.savings.step_savings == []

    @pytest.mark.asyncio
    async def test_response_path_returns_response_untouched(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        ctx = _make_ctx([])
        response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        out = await G27MultimodalOptimizer().process_response(ctx, response)
        assert out is response

    @pytest.mark.asyncio
    async def test_pipeline_stub_compatible(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        g = G27MultimodalOptimizer()
        assert hasattr(g, "process_request")
        assert hasattr(g, "process_response")


# ─── Source inspection: the dependency must not creep back ───────────────────

class TestG27ImportsNoImageLibrary:
    """Source-level guards. A future edit that re-adds the image compressor would also
    re-add an accounting path the ledger cannot represent, so pin the absence here rather
    than relying on anyone re-reading the docstring."""

    def _source(self) -> str:
        import middleware.g27_multimodal_optimizer as mod
        with open(mod.__file__, encoding="utf-8") as fh:
            return fh.read()

    def test_module_does_not_import_headroom(self):
        src = self._source()
        assert "import headroom" not in src
        assert "from headroom" not in src

    def test_module_defines_no_compressor_hook(self):
        import middleware.g27_multimodal_optimizer as mod
        for gone in ("_compress_images_fn", "_parse_data_uri", "_count_image_bytes",
                     "_resolve_provider", "_supported_kwargs", "_G27_TUNABLES"):
            assert not hasattr(mod, gone), (
                f"{gone} is back. G27 is a reserved no-op: an image lever here cannot save a "
                "token this proxy measures, and recording one writes fiction into "
                "usage_events.group_savings."
            )

    def test_module_records_no_savings_step(self):
        assert "add_step" not in self._source()
