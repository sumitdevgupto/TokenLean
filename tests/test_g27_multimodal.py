"""Tests for G27 — Multimodal Image Optimizer (headroom.image message-level API)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import base64
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock


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


def _fake_compress_images(new_bytes):
    """Return a stand-in for headroom.image.compress_images that swaps every
    inline image for one encoding `new_bytes`."""
    def _fn(messages, provider="openai"):
        out = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                new_content = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        new_content.append(
                            {"type": "image_url", "image_url": {"url": _data_uri(new_bytes)}}
                        )
                    else:
                        new_content.append(block)
                out.append({**m, "content": new_content})
            else:
                out.append(m)
        return out
    return _fn


# ─── _parse_data_uri ────────────────────────────────────────────────────────

class TestParseDataUri:

    def test_valid_jpeg_uri(self):
        from middleware.g27_multimodal_optimizer import _parse_data_uri
        raw = b"\xFF\xD8" + b"\x00" * 10
        media_type, parsed = _parse_data_uri(_data_uri(raw))
        assert media_type == "image/jpeg"
        assert parsed == raw

    def test_non_data_uri_returns_none(self):
        from middleware.g27_multimodal_optimizer import _parse_data_uri
        media_type, raw = _parse_data_uri("https://example.com/image.jpg")
        assert media_type is None and raw is None

    def test_malformed_returns_none(self):
        from middleware.g27_multimodal_optimizer import _parse_data_uri
        media_type, raw = _parse_data_uri("data:notvalidbase64!!!")
        assert media_type is None or raw is None


# ─── _count_image_bytes ───────────────────────────────────────────────────────

class TestCountImageBytes:

    def test_counts_inline_images_only(self):
        from middleware.g27_multimodal_optimizer import _count_image_bytes
        raw = _make_jpeg_bytes(8192)
        msgs = [
            {"role": "system", "content": "text"},
            _make_vision_message(raw),
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/x.jpg"}},
            ]},
        ]
        assert _count_image_bytes(msgs) == len(raw)  # external URL not counted

    def test_zero_when_no_images(self):
        from middleware.g27_multimodal_optimizer import _count_image_bytes
        assert _count_image_bytes([{"role": "user", "content": "no images"}]) == 0


# ─── _resolve_provider ─────────────────────────────────────────────────────────

class TestResolveProvider:

    def test_config_override_wins(self):
        from middleware.g27_multimodal_optimizer import _resolve_provider
        ctx = _make_ctx([], cfg_extra={"provider": "anthropic"})
        cfg = ctx.config["groups"]["G27_multimodal"]
        assert _resolve_provider(ctx, cfg) == "anthropic"

    def test_defaults_to_openai(self):
        from middleware.g27_multimodal_optimizer import _resolve_provider
        ctx = _make_ctx([])
        cfg = ctx.config["groups"]["G27_multimodal"]
        assert _resolve_provider(ctx, cfg) == "openai"


# ─── G27MultimodalOptimizer ─────────────────────────────────────────────────

class TestG27MultimodalOptimizer:

    @pytest.mark.asyncio
    async def test_disabled_skips(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        ctx = _make_ctx([{"role": "user", "content": "hi"}], enabled=False)
        ctx = await G27MultimodalOptimizer().process_request(ctx)
        assert len(ctx.savings.step_savings) == 0

    @pytest.mark.asyncio
    async def test_headroom_image_unavailable_is_noop(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod
        msgs = [_make_vision_message(_make_jpeg_bytes(8192))]
        ctx = _make_ctx(msgs)
        with patch.object(mod, "_compress_images_fn", None):
            ctx = await G27MultimodalOptimizer().process_request(ctx)
        assert ctx.messages == msgs
        assert len(ctx.savings.step_savings) == 0

    @pytest.mark.asyncio
    async def test_no_inline_images_is_noop(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod
        msgs = [{"role": "user", "content": "Just text, no images."}]
        ctx = _make_ctx(msgs)
        mock_fn = MagicMock()
        with patch.object(mod, "_compress_images_fn", mock_fn):
            ctx = await G27MultimodalOptimizer().process_request(ctx)
        mock_fn.assert_not_called()  # short-circuits before calling headroom
        assert ctx.messages == msgs

    @pytest.mark.asyncio
    async def test_compression_applied_and_savings_recorded(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod

        raw = _make_jpeg_bytes(8192)
        compressed = raw[:4000]  # ~51% reduction
        ctx = _make_ctx([_make_vision_message(raw)])

        with patch.object(mod, "_compress_images_fn", _fake_compress_images(compressed)):
            ctx = await G27MultimodalOptimizer().process_request(ctx)

        assert len(ctx.savings.step_savings) == 1
        step = ctx.savings.step_savings[0]
        assert step.group == "G27"
        assert step.tokens_after < step.tokens_before

        new_url = ctx.messages[0]["content"][1]["image_url"]["url"]
        decoded = base64.b64decode(new_url.split(",", 1)[1])
        assert decoded == compressed

    @pytest.mark.asyncio
    async def test_no_improvement_leaves_messages_unchanged(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod

        raw = _make_jpeg_bytes(8192)
        msgs = [_make_vision_message(raw)]
        ctx = _make_ctx(msgs)

        # headroom returns an identical-size image → no net saving
        with patch.object(mod, "_compress_images_fn", _fake_compress_images(raw)):
            ctx = await G27MultimodalOptimizer().process_request(ctx)

        assert len(ctx.savings.step_savings) == 0
        assert ctx.messages == msgs

    @pytest.mark.asyncio
    async def test_compress_exception_leaves_original(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod

        raw = _make_jpeg_bytes(8192)
        msgs = [_make_vision_message(raw)]
        ctx = _make_ctx(msgs)

        with patch.object(mod, "_compress_images_fn", MagicMock(side_effect=RuntimeError("boom"))):
            ctx = await G27MultimodalOptimizer().process_request(ctx)

        assert ctx.messages == msgs
        assert len(ctx.savings.step_savings) == 0

    @pytest.mark.asyncio
    async def test_langfuse_span_added_on_compression(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod

        raw = _make_jpeg_bytes(8192)
        ctx = _make_ctx([_make_vision_message(raw)])

        with patch.object(mod, "_compress_images_fn", _fake_compress_images(raw[:2000])), \
             patch.object(mod, "langfuse_tracing") as mock_lf:
            ctx = await G27MultimodalOptimizer().process_request(ctx)
            mock_lf.add_span.assert_called_once()
            assert mock_lf.add_span.call_args[1]["name"] == "G27-multimodal-optimizer"

    @pytest.mark.asyncio
    async def test_pipeline_stub_compatible(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        g = G27MultimodalOptimizer()
        assert hasattr(g, "process_request")
        assert hasattr(g, "process_response")


# ─── quality / min_bytes knob pass-through (portal-tunable) ───────────────────

class TestSupportedKwargs:
    def test_only_signature_supported_kwargs_pass(self):
        from middleware.g27_multimodal_optimizer import _supported_kwargs
        cfg = {"quality": 40, "min_bytes": 1024}

        def takes_quality(messages, provider="openai", quality=None):
            return messages

        def takes_none(messages, provider="openai"):
            return messages

        def takes_var_kw(messages, provider="openai", **kw):
            return messages

        assert _supported_kwargs(takes_quality, cfg) == {"quality": 40}
        assert _supported_kwargs(takes_none, cfg) == {}
        assert _supported_kwargs(takes_var_kw, cfg) == {"quality": 40, "min_bytes": 1024}

    def test_unset_knobs_omitted(self):
        from middleware.g27_multimodal_optimizer import _supported_kwargs

        def takes_both(messages, provider="openai", quality=None, min_bytes=None):
            return messages

        assert _supported_kwargs(takes_both, {"quality": 50}) == {"quality": 50}


class TestKnobForwarding:
    @pytest.mark.asyncio
    async def test_quality_and_min_bytes_forwarded(self):
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod

        received = {}

        def _recording(messages, provider="openai", quality=None, min_bytes=None):
            received["quality"] = quality
            received["min_bytes"] = min_bytes
            return _fake_compress_images(_make_jpeg_bytes(1024))(messages, provider=provider)

        ctx = _make_ctx([_make_vision_message(_make_jpeg_bytes(8192))],
                        cfg_extra={"quality": 33, "min_bytes": 2048})
        with patch.object(mod, "_compress_images_fn", _recording):
            await G27MultimodalOptimizer().process_request(ctx)

        assert received == {"quality": 33, "min_bytes": 2048}

    @pytest.mark.asyncio
    async def test_legacy_compressor_signature_still_works(self):
        # A compressor that predates the knobs (no quality/min_bytes) must NOT get them
        # and must still compress — behaviour identical to before this change.
        from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
        import middleware.g27_multimodal_optimizer as mod

        ctx = _make_ctx([_make_vision_message(_make_jpeg_bytes(8192))],
                        cfg_extra={"quality": 33, "min_bytes": 2048})
        with patch.object(mod, "_compress_images_fn",
                          _fake_compress_images(_make_jpeg_bytes(3000))):
            ctx = await G27MultimodalOptimizer().process_request(ctx)

        assert len(ctx.savings.step_savings) == 1  # still compressed, no TypeError
