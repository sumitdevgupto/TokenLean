"""A stream=true OpenAI request gets a stream on every path, not only a live model call.

A cache hit, a G04 bypass, a G29/G30/G31 block and an F2 agent answer produce a whole JSON
completion. Sent as-is to a client that asked for a stream, the OpenAI SDK's stream reader
finds no events: the user sees an empty reply (a block's refusal text is lost), while the
request is billed as served. The Anthropic and Gemini routes already re-sent such a body as
a one-chunk stream; the OpenAI route returned it untouched.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import json

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletionChunk

import main
from guardrails import content_filter_response
from middleware.g00_rate_limit import RateLimitExceeded
from middleware.g32_tool_eligibility import G32ToolEligibility

_client = TestClient(main.app)
_CFG = {"groups": {}, "providers": []}
_USAGE = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}


def _completion(content="Paris", message=None, finish="stop"):
    return {
        "id": "chatcmpl-cached", "object": "chat.completion", "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0,
                     "message": message or {"role": "assistant", "content": content},
                     "finish_reason": finish}],
        "usage": dict(_USAGE),
    }


def _cache_hit(ctx):
    ctx.cache_hit = ctx.savings.cache_hit = True     # as G05 records an L1 hit
    ctx.cache_level = ctx.savings.cache_level = "L1"
    ctx.cache_response = _completion()


def _bypass(ctx):
    ctx.bypassed = True
    ctx.cache_response = {        # G04's shape: no `created`, a `_token_opt` block
        "id": "bypass-1", "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "_token_opt": {"bypassed": True, "rule": "G04"},
    }


def _security_block(ctx):
    ctx.security_blocked = True
    ctx.security_block_response = content_filter_response(
        ctx.request_id, "gpt-4o-mini", "This request was blocked by the safety guardrails.")


def _agent(ctx):
    ctx.agent_dispatched = True
    ctx.agent_response = _completion("Answer from the billing agent")


_PATHS = {
    "cache-hit": (_cache_hit, "Paris", "stop"),
    "bypass": (_bypass, "OK", "stop"),
    "security-block": (_security_block,
                       "This request was blocked by the safety guardrails.", "content_filter"),
    "agent": (_agent, "Answer from the billing agent", "stop"),
}


class _Pipeline:
    """Stands in for OptimisationPipeline: `request_side(ctx)` is what the request side did."""

    def __init__(self, request_side):
        self._request_side = request_side
        self.g32 = G32ToolEligibility()

    async def process_request(self, ctx, request_headers=None):
        self._request_side(ctx)
        return ctx

    async def process_response(self, ctx, response):
        return ctx, response


@pytest.fixture(autouse=True)
def _no_billing(monkeypatch):
    monkeypatch.setattr(main, "_usage_meter", None)


async def _fake_auth(request):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "enterprise"}


def _post(monkeypatch, request_side, config=_CFG, **body):
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: config)
    monkeypatch.setattr(main, "_pipeline", _Pipeline(request_side))
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], **body}
    return _client.post("/v1/chat/completions", json=payload,
                        headers={"Authorization": "Bearer tok-x"})


def _frames(resp):
    """The SSE data frames as dicts; the stream must end with [DONE]."""
    assert resp.headers["content-type"].startswith("text/event-stream")
    data = [part[len("data: "):] for part in resp.text.split("\n\n") if part.startswith("data: ")]
    assert data and data[-1] == "[DONE]"
    return [json.loads(d) for d in data[:-1]]


def _text(frames):
    return "".join(c["delta"].get("content") or "" for f in frames for c in f["choices"])


@pytest.mark.parametrize("path", list(_PATHS))
def test_a_streaming_client_gets_a_stream_on_every_short_circuit(monkeypatch, path):
    request_side, text, finish = _PATHS[path]
    resp = _post(monkeypatch, request_side, stream=True)
    assert resp.status_code == 200
    frames = _frames(resp)
    for frame in frames:
        ChatCompletionChunk.model_validate(frame)      # the model the OpenAI SDK parses into
    assert _text(frames) == text
    assert frames[0]["choices"][0]["finish_reason"] == finish
    assert frames[0]["choices"][0]["delta"]["role"] == "assistant"


def test_the_openai_sdk_reads_a_cached_answer_as_a_stream(monkeypatch):
    """The real SDK, over the app: before the fix it read no events and returned nothing."""
    import openai
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: _CFG)
    monkeypatch.setattr(main, "_pipeline", _Pipeline(_cache_hit))
    sdk = openai.OpenAI(api_key="tok-x", base_url="http://testserver/v1", http_client=_client,
                        max_retries=0)
    stream = sdk.chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], stream=True,
        stream_options={"include_usage": True})
    chunks = list(stream)
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Paris"
    assert chunks[-1].usage.total_tokens == _USAGE["total_tokens"]


def test_a_non_streaming_client_still_gets_json(monkeypatch):
    resp = _post(monkeypatch, _cache_hit)
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["choices"][0]["message"]["content"] == "Paris"


def test_usage_arrives_in_its_own_last_chunk_like_a_live_stream(monkeypatch):
    frames = _frames(_post(monkeypatch, _cache_hit, stream=True))
    assert len(frames) == 2
    assert "usage" not in frames[0]
    assert frames[1]["choices"] == [] and frames[1]["usage"] == _USAGE
    assert frames[1]["id"] == frames[0]["id"] == "chatcmpl-cached"


@pytest.mark.parametrize("stream_options,sent", [
    ({"include_usage": True}, True),
    ({"include_usage": False}, False),
    ({}, False),
], ids=["asked", "turned-off", "options-without-it"])
def test_stream_options_decide_the_usage_chunk_as_on_a_live_stream(
        monkeypatch, stream_options, sent):
    frames = _frames(_post(monkeypatch, _cache_hit, stream=True, stream_options=stream_options))
    assert any("usage" in f for f in frames) is sent
    assert _text(frames) == "Paris"


def test_the_savings_headers_survive(monkeypatch):
    resp = _post(monkeypatch, _cache_hit, stream=True)
    assert resp.headers.get("x-tokenlean-cache") == "hit:L1"
    assert resp.headers.get("x-tokenlean-request-id")


def test_a_cached_tool_call_streams_with_its_index(monkeypatch):
    call = {"id": "call_1", "type": "function",
            "function": {"name": "lookup_order", "arguments": '{"id": 7}'}}

    def cached_call(ctx):
        ctx.cache_hit = True
        ctx.cache_response = _completion(
            message={"role": "assistant", "content": None, "tool_calls": [call]},
            finish="tool_calls")

    frames = _frames(_post(monkeypatch, cached_call, stream=True))
    ChatCompletionChunk.model_validate(frames[0])
    delta = frames[0]["choices"][0]["delta"]
    assert delta["tool_calls"] == [{**call, "index": 0}]
    assert frames[0]["choices"][0]["finish_reason"] == "tool_calls"


def test_the_tool_policy_still_applies_before_the_stream(monkeypatch):
    """G32 strips a denied cached call on the short-circuit; the stream carries the result."""
    policy = {"groups": {"G32_tool_eligibility": {
        "enabled": True, "mode": "block", "policy": {"deny": ["shell_exec"]}}}, "providers": []}

    def cached_call(ctx):
        ctx.cache_hit = True
        ctx.cache_response = _completion(
            message={"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "shell_exec", "arguments": "{}"}}]},
            finish="tool_calls")

    frames = _frames(_post(monkeypatch, cached_call, config=policy, stream=True))
    assert not any(c["delta"].get("tool_calls") for f in frames for c in f["choices"])
    assert _text(frames)


def test_a_refusal_before_the_call_stays_a_json_error(monkeypatch):
    def rate_limited(ctx):
        raise RateLimitExceeded(retry_after=30, limit_type="rps", scope="acme")

    resp = _post(monkeypatch, rate_limited, stream=True)
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"
    assert resp.headers["retry-after"] == "30"


def test_a_batched_request_keeps_its_202_body(monkeypatch):
    def deferred(ctx):
        ctx.batch_deferred = True

    resp = _post(monkeypatch, deferred, stream=True)
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


def test_a_live_stream_is_passed_through_untouched():
    async def body():
        yield "data: [DONE]\n\n"

    live = StreamingResponse(body(), media_type="text/event-stream")
    assert main._as_requested_stream(live, withhold_usage=False) is live


def test_a_json_body_without_choices_is_passed_through():
    from fastapi.responses import JSONResponse
    control = JSONResponse(content={"status": "queued", "request_id": "r-1"})
    assert main._as_requested_stream(control, withhold_usage=False) is control


@pytest.mark.parametrize("status", [202, 400, 429, 502])
def test_a_non_200_status_is_never_turned_into_a_stream(status):
    """Even a body shaped like a completion: an HTTP error must reach the SDK as one."""
    from fastapi.responses import JSONResponse
    resp = JSONResponse(status_code=status, content=_completion())
    assert main._as_requested_stream(resp, withhold_usage=False) is resp


def test_the_cached_body_is_not_mutated():
    cached = _completion()
    before = json.dumps(cached, sort_keys=True)

    async def drain():
        return [line async for line in main._openai_one_shot_stream(cached, withhold_usage=False)]

    lines = asyncio.run(drain())
    assert lines[-1] == "data: [DONE]\n\n"
    assert json.dumps(cached, sort_keys=True) == before
