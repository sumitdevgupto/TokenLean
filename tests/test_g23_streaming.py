"""D3-T: Tests for G23StreamingCompression."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone

from middleware.g23_streaming_compression import G23StreamingCompression, _compress_text
from middleware import RequestContext
from savings.models import SavingsRecord


def _make_ctx(enabled=True, min_repeat=3, ngram_size=5):
    savings = SavingsRecord(
        request_id="req-g23",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=200,
    )
    return RequestContext(
        request_id="req-g23",
        user_id="u1",
        original_messages=[{"role": "user", "content": "explain"}],
        messages=[{"role": "user", "content": "explain"}],
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={
            "groups": {
                "G23_streaming_compression": {
                    "enabled": enabled,
                    "min_repeat": min_repeat,
                    "ngram_size": ngram_size,
                }
            }
        },
        savings=savings,
    )


def _make_response(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


class TestCompressText:
    def test_short_text_unchanged(self):
        text = "Hello world this is short"
        result, saved = _compress_text(text, min_repeat=3, ngram_size=5)
        assert result == text
        assert saved == 0

    def test_repeated_ngram_compressed(self):
        phrase = "the payment gateway connection timeout error"
        # Repeat the 5-word phrase 4 times
        text = (
            f"First occurrence: {phrase}. "
            f"Second occurrence: {phrase}. "
            f"Third occurrence: {phrase}. "
            f"Fourth occurrence: {phrase}."
        )
        compressed, saved = _compress_text(text, min_repeat=3, ngram_size=5)
        assert saved > 0
        assert len(compressed) < len(text)

    def test_unique_content_not_compressed(self):
        text = (
            "The sky is blue today. "
            "Mountains are tall and majestic. "
            "Rivers flow through valleys. "
            "The ocean covers most of Earth. "
            "Forests provide oxygen for life."
        )
        compressed, saved = _compress_text(text, min_repeat=3, ngram_size=5)
        assert saved == 0

    def test_first_occurrence_preserved(self):
        phrase = "please note that this disclaimer applies to all content"
        text = " ".join([phrase] * 5)
        compressed, saved = _compress_text(text, min_repeat=3, ngram_size=5)
        # First occurrence must still be present
        first_words = "please note that this"
        assert first_words in compressed.lower()


class TestG23Middleware:
    @pytest.mark.asyncio
    async def test_disabled_config_noop(self):
        from middleware.g23_streaming_compression import COMPRESSIBLE_OUTPUT_TOKENS
        ctx = _make_ctx(enabled=False)
        ctx.tenant_id = "g23-disabled"
        counter = COMPRESSIBLE_OUTPUT_TOKENS.labels(tenant_id="g23-disabled")
        before = counter._value.get()
        g23 = G23StreamingCompression()
        content = "This is the repeated pattern. " * 10
        response = _make_response(content)
        result = await g23.process_response(ctx, response)
        assert "x_compressed_content" not in result
        assert len(ctx.savings.step_savings) == 0
        assert counter._value.get() == before  # nothing measured either

    @pytest.mark.asyncio
    async def test_the_answer_reaches_the_client_as_it_came(self):
        # The compressed copy went into the body as x_compressed_content (and so into the
        # G05 cache), doubling the answer for a field nothing read.
        import copy
        ctx = _make_ctx(enabled=True, min_repeat=3)
        content = " ".join(["connection timeout to payment gateway occurred"] * 5)
        response = _make_response(content)
        sent = copy.deepcopy(response)
        result = await G23StreamingCompression().process_response(ctx, response)
        assert result == sent

    @pytest.mark.asyncio
    async def test_no_savings_step_is_recorded(self):
        # The client receives the whole answer and nothing replays the compressed copy, so
        # no token was saved: a step here fed group savings and USD_SAVED with phantoms.
        ctx = _make_ctx(enabled=True, min_repeat=3)
        content = " ".join(["the service encountered an unexpected error"] * 6)
        await G23StreamingCompression().process_response(ctx, _make_response(content))
        assert [s for s in ctx.savings.step_savings if s.group == "G23"] == []

    @pytest.mark.asyncio
    async def test_the_repetition_is_measured(self):
        from middleware.g23_streaming_compression import (
            COMPRESSIBLE_OUTPUT_TOKENS, _estimate_tokens_from_chars)
        ctx = _make_ctx(enabled=True, min_repeat=3)
        ctx.tenant_id = "g23-measured"
        content = " ".join(["the distributed system experienced high latency"] * 5)
        _, chars = _compress_text(content, min_repeat=3, ngram_size=5)
        counter = COMPRESSIBLE_OUTPUT_TOKENS.labels(tenant_id="g23-measured")
        before = counter._value.get()
        await G23StreamingCompression().process_response(ctx, _make_response(content))
        assert chars > 0
        assert counter._value.get() - before == _estimate_tokens_from_chars(chars)

    @pytest.mark.asyncio
    async def test_original_response_content_unchanged(self):
        ctx = _make_ctx(enabled=True)
        phrase = "the gateway returned an error code"
        content = " ".join([phrase] * 5)
        response = _make_response(content)
        g23 = G23StreamingCompression()
        result = await g23.process_response(ctx, response)
        # Original content is never modified
        assert result["choices"][0]["message"]["content"] == content

    @pytest.mark.asyncio
    async def test_no_choices_returns_response_unchanged(self):
        ctx = _make_ctx(enabled=True)
        response = {"choices": [], "usage": {}}
        g23 = G23StreamingCompression()
        result = await g23.process_response(ctx, response)
        assert result == response

    @pytest.mark.asyncio
    async def test_none_content_returns_response_unchanged(self):
        ctx = _make_ctx(enabled=True)
        response = {"choices": [{"message": {"role": "assistant", "content": None}}]}
        g23 = G23StreamingCompression()
        result = await g23.process_response(ctx, response)
        assert "x_compressed_content" not in result

    @pytest.mark.asyncio
    async def test_tool_call_response_no_content_unchanged(self):
        ctx = _make_ctx(enabled=True)
        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "function": {"name": "search"}}],
                }
            }]
        }
        g23 = G23StreamingCompression()
        result = await g23.process_response(ctx, response)
        assert "x_compressed_content" not in result

    @pytest.mark.asyncio
    async def test_unique_content_no_savings_recorded(self):
        ctx = _make_ctx(enabled=True)
        content = (
            "Alpha bravo charlie delta echo foxtrot golf hotel india juliet. "
            "Kilo lima mike november oscar papa quebec romeo sierra tango. "
            "Uniform victor whiskey xray yankee zulu one two three four five."
        )
        response = _make_response(content)
        g23 = G23StreamingCompression()
        await g23.process_response(ctx, response)
        g23_steps = [s for s in ctx.savings.step_savings if s.group == "G23"]
        assert len(g23_steps) == 0
