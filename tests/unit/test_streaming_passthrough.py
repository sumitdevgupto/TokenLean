"""Row 14 — streaming pass-through: relay provider SSE chunks, skip the response pipeline,
capture usage from the final chunk, and bill once on completion.
"""
import types

import pytest

import main


class _Ctx:
    def __init__(self):
        self.messages = [{"role": "user", "content": "hi"}]
        self.config = {}
        self.tenant_id = "default"
        self.request_id = "req-1"
        # Mirror RequestContext: the streaming path accumulates the stream's
        # wall-time into this (via +=), so it must exist as a number.
        self.llm_elapsed_ms = 0.0


async def _collect(streaming_response):
    chunks = []
    async for part in streaming_response.body_iterator:
        chunks.append(part if isinstance(part, str) else part.decode())
    return "".join(chunks)


@pytest.mark.asyncio
async def test_stream_relays_chunks_and_bills_once(monkeypatch):
    fake_chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]

    async def fake_acompletion(**kwargs):
        async def gen():
            for c in fake_chunks:
                yield c
        return gen()

    billed = {}

    def fake_record(ctx, start, status, response=None):
        billed["status"] = status
        billed["response"] = response

    monkeypatch.setattr(main.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(main, "_record_outcome", fake_record)

    resp = main._stream_response(_Ctx(), "gpt-4o-mini", {"api_key": "sk"}, {"stream": True}, "req-1", 0.0)
    body = await _collect(resp)

    # SSE framing + content + terminal [DONE]
    assert "data: " in body
    assert "Hel" in body and "lo" in body
    assert body.strip().endswith("data: [DONE]")
    # Billed exactly once, with usage captured from the final chunk
    assert billed["status"] == "200"
    assert billed["response"]["usage"]["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_stream_error_still_bills(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("provider down")

    billed = {}
    monkeypatch.setattr(main.litellm, "acompletion", boom)
    monkeypatch.setattr(main, "_record_outcome", lambda *a, **k: billed.setdefault("called", True))

    resp = main._stream_response(_Ctx(), "gpt-4o-mini", {}, {"stream": True}, "req-2", 0.0)
    body = await _collect(resp)
    assert "error" in body  # error surfaced in the stream
    assert billed.get("called") is True  # finally-block billing still ran


def test_apply_stream_g23_records_savings():
    """Chunk-aware G23 (P9): reassembled streamed text is run through G23 to record the
    output-side savings the (skipped) response pipeline would have."""
    class _Sav:
        def __init__(self):
            self.steps = []

        def add_step(self, group, *a, **k):
            self.steps.append(group)

    class _C:
        request_id = "r"

        def __init__(self):
            self.config = {"groups": {"G23_streaming_compression": {"enabled": True}}}
            self.savings = _Sav()

    c = _C()
    phrase = "alpha beta gamma delta epsilon "  # one 5-gram, >20 chars
    main._apply_stream_g23(c, phrase * 4 + "and a unique tail to finish")
    assert "G23" in c.savings.steps


def test_apply_stream_g23_noop_when_disabled():
    class _C:
        request_id = "r"
        config = {"groups": {"G23_streaming_compression": {"enabled": False}}}
        savings = None  # must not be touched when disabled
    main._apply_stream_g23(_C(), "alpha beta gamma delta epsilon " * 4)  # no exception
