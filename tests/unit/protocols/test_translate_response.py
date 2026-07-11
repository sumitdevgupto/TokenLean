"""Unit tests for main's protocol response/stream translation helpers.

Covers the Branch-3 code-review fixes (2026-07-11):
  * F3 — client asked for a stream but the pipeline short-circuited (cache hit / bypass)
    with a JSON body → synthesise a native one-chunk stream so the SSE contract holds.
  * F5 — a non-completion control body (batch-defer 202) is passed through untranslated
    so the request_id survives (not mangled into an empty "successful" message).
  * F8 — Retry-After is preserved when an error is translated (native SDK backoff needs it).
  * F4 — a stream that errored does NOT get a synthetic success termination from finish().
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

import main
from protocols import ANTHROPIC, GEMINI


def _completion_json(content="hi", finish="stop"):
    return JSONResponse(content={
        "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    })


async def _collect(streaming_response):
    out = []
    async for part in streaming_response.body_iterator:
        out.append(part if isinstance(part, str) else part.decode())
    return "".join(out)


@pytest.mark.asyncio
async def test_want_stream_synthesises_native_stream_on_json_body_F3():
    resp = _completion_json("cached-answer")
    out_resp = await main._translate_response(ANTHROPIC, resp, want_stream=True)
    assert isinstance(out_resp, StreamingResponse)
    body = await _collect(out_resp)
    assert "event: message_start" in body
    assert "cached-answer" in body
    assert body.strip().endswith('data: {"type":"message_stop"}')


@pytest.mark.asyncio
async def test_batch_defer_202_passthrough_preserves_request_id_F5():
    resp = JSONResponse(status_code=202,
                        content={"status": "queued", "request_id": "req-xyz"})
    out_resp = await main._translate_response(ANTHROPIC, resp)
    body = json.loads(bytes(out_resp.body).decode())
    assert out_resp.status_code == 202
    assert body.get("request_id") == "req-xyz"  # not fabricated into an empty message
    assert body.get("status") == "queued"


@pytest.mark.asyncio
async def test_error_translation_preserves_retry_after_F8():
    resp = JSONResponse(
        status_code=429,
        content={"error": {"message": "rate limited", "type": "rate_limit_exceeded",
                           "code": "rate_limit_exceeded"}},
        headers={"Retry-After": "7"},
    )
    out_resp = await main._translate_response(ANTHROPIC, resp)
    assert out_resp.status_code == 429
    assert out_resp.headers.get("Retry-After") == "7"
    body = json.loads(bytes(out_resp.body).decode())
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"  # Anthropic envelope


@pytest.mark.asyncio
async def test_stream_error_not_masked_by_finish_F4():
    async def _source():
        yield b'data: {"error":"upstream boom"}\n\n'
    translator = GEMINI.stream_translator()
    out = "".join([line async for line in main._translate_stream(translator, _source())])
    # The error frame is emitted, but NO synthetic success termination follows it.
    assert "UNAVAILABLE" in out
    assert '"finishReason":"STOP"' not in out


def test_completion_to_stream_chunk_maps_message_to_delta():
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "x"},
                         "finish_reason": "stop"}]}
    chunk = main._completion_to_stream_chunk(body)
    choice = chunk["choices"][0]
    assert "message" not in choice
    assert choice["delta"] == {"role": "assistant", "content": "x"}
    assert choice["finish_reason"] == "stop"


def test_completion_to_stream_chunk_stamps_tool_call_indices_without_mutating():
    """A completion's tool_calls carry no streaming `index`; the force-stream/cache-hit
    path must stamp distinct indices so the native-stream accumulators don't collapse
    multiple tool_calls into slot 0 — and must not mutate the (possibly cached) body."""
    tcs = [
        {"id": "a", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
    ]
    body = {"choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": tcs}}]}
    chunk = main._completion_to_stream_chunk(body)
    stamped = chunk["choices"][0]["delta"]["tool_calls"]
    assert [tc["index"] for tc in stamped] == [0, 1]
    # Original tool_call dicts untouched (no `index` leaked back into the cached body).
    assert "index" not in tcs[0] and "index" not in tcs[1]
