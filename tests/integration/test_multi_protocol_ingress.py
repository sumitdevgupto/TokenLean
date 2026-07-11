"""Integration test: native Anthropic + Gemini ingress through the real app (#4).

Drives /v1/messages and /v1beta/models/{model}:generateContent with litellm mocked,
asserting the request is normalised into the pipeline and the OpenAI response is
re-serialised back into the caller's native protocol shape (non-streaming + streaming),
that native-SDK auth headers work, and that errors use the protocol's envelope.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import hashlib
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

import litellm

_KEY = "test-proxy-key-ingress"
_KEY_HASH = hashlib.sha256(_KEY.encode()).hexdigest()

_DISABLED = [
    "G1_compression", "G2_template_registry", "G4_bypass", "G5_cache", "G6_routing",
    "G7_retrieval", "G8_tools", "G9_context_schema", "G10_memory", "G11_output",
    "G12_reasoning", "G13_batch", "G14_tool_output", "G15_server_compute",
    "G16_agent_arch", "G17_loop", "G18_observability", "G19_headroom",
    "G20_prompt_optimization", "G21_cache_alignment", "g22_deduplication",
    "G23_streaming_compression", "G24_adaptive_bypass", "G25_adaptive_reasoning",
    "G27_multimodal", "G28_ccr", "G29_pii_redaction", "G30_guardrails",
]


def _config():
    groups = {k: {"enabled": False} for k in _DISABLED}
    return {
        "proxy": {"port": 4000, "default_provider": "openai"},
        "providers": [
            {"name": "openai", "models": ["gpt-4o-mini"], "model_prefixes": ["gpt-"]},
            {"name": "anthropic", "models": ["claude-3-5-haiku"], "model_prefixes": ["claude"]},
            {"name": "gemini", "models": ["gemini-2.5-flash"], "model_prefixes": ["gemini"]},
        ],
        "groups": groups,
    }


def _resp(content="hello-from-llm", model="claude-3-5-haiku", tool_calls=None):
    m = MagicMock()
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["content"] = None
        msg["tool_calls"] = tool_calls
    m.model_dump.return_value = {
        "id": "chatcmpl-x", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }
    return m


def _stream_chunks(text="streamed-text"):
    async def gen():
        for ch in [
            {"model": "claude-3-5-haiku", "id": "msg_x", "choices": [{"delta": {"content": text}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        ]:
            yield ch
    return gen()


def _drive(acompletion, path, headers, body):
    patches = [
        patch("auth.api_key_manager._fetch_secret", return_value=json.dumps({_KEY_HASH: "u"})),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("main.get_config", return_value=_config()),
        patch("litellm.acompletion", acompletion),
        patch("main.resolve_provider_key", new_callable=AsyncMock, return_value="sk-test"),
        patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
    ]
    for p in patches:
        p.__enter__()
    try:
        import main
        from auth import api_key_manager as _akm
        _akm.replace_cache({_KEY_HASH: "u"})
        client = TestClient(main.app)
        return client.post(path, headers=headers, json=body)
    finally:
        for p in patches:
            p.__exit__(None, None, None)


# ── Anthropic /v1/messages ────────────────────────────────────────────────────
def test_anthropic_non_streaming_returns_message_shape():
    mock = AsyncMock(return_value=_resp("hi-from-claude"))
    body = {"model": "claude-3-5-haiku", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}]}
    resp = _drive(mock, "/v1/messages", {"x-api-key": _KEY}, body)  # native x-api-key auth
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "message" and data["role"] == "assistant"
    assert data["content"][0]["text"] == "hi-from-claude"
    assert data["stop_reason"] == "end_turn"
    assert data["usage"] == {"input_tokens": 12, "output_tokens": 4}


def test_anthropic_streaming_returns_message_events():
    async def acompletion(**kwargs):
        return _stream_chunks("claude-streamed")
    body = {"model": "claude-3-5-haiku", "max_tokens": 50, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = _drive(acompletion, "/v1/messages", {"x-api-key": _KEY}, body)
    assert resp.status_code == 200
    assert "event: message_start" in resp.text
    assert "event: content_block_delta" in resp.text
    assert "claude-streamed" in resp.text
    assert resp.text.strip().endswith('data: {"type":"message_stop"}')


def test_anthropic_bearer_auth_also_works():
    mock = AsyncMock(return_value=_resp())
    body = {"model": "claude-3-5-haiku", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = _drive(mock, "/v1/messages", {"Authorization": f"Bearer {_KEY}"}, body)
    assert resp.status_code == 200


def test_anthropic_auth_failure_uses_anthropic_error_envelope():
    mock = AsyncMock(return_value=_resp())
    body = {"model": "claude-3-5-haiku", "max_tokens": 10, "messages": []}
    resp = _drive(mock, "/v1/messages", {"x-api-key": "wrong-key"}, body)
    assert resp.status_code == 401
    assert resp.json()["type"] == "error"
    assert resp.json()["error"]["type"] == "authentication_error"


# ── Gemini generateContent ────────────────────────────────────────────────────
def test_gemini_non_streaming_returns_candidates_shape():
    mock = AsyncMock(return_value=_resp("hi-from-gemini", model="gemini-2.5-flash"))
    body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    resp = _drive(mock, "/v1beta/models/gemini-2.5-flash:generateContent",
                  {"x-goog-api-key": _KEY}, body)  # native Gemini auth
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidates"][0]["content"]["parts"][0]["text"] == "hi-from-gemini"
    assert data["candidates"][0]["finishReason"] == "STOP"
    assert data["usageMetadata"]["totalTokenCount"] == 16


def test_gemini_streaming_returns_candidate_frames():
    async def acompletion(**kwargs):
        return _stream_chunks("gem-streamed")
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    # SSE streaming is requested with ?alt=sse (the Gemini wire contract).
    resp = _drive(acompletion, "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"candidates"' in resp.text
    assert "gem-streamed" in resp.text


def test_gemini_key_query_param_auth_works():
    mock = AsyncMock(return_value=_resp(model="gemini-2.5-flash"))
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    # ?key= in the URL (Gemini SDK style)
    resp = _drive(mock, f"/v1beta/models/gemini-2.5-flash:generateContent?key={_KEY}", {}, body)
    assert resp.status_code == 200


def test_gemini_provider_error_uses_google_error_envelope():
    mock = AsyncMock(side_effect=litellm.exceptions.RateLimitError(
        message="rl", llm_provider="gemini", model="gemini-2.5-flash"))
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    resp = _drive(mock, "/v1beta/models/gemini-2.5-flash:generateContent",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 429
    assert resp.json()["error"]["status"] == "RESOURCE_EXHAUSTED"


# ── Branch-3 code-review fixes (2026-07-11) ───────────────────────────────────
def test_gemini_stream_default_no_alt_returns_json_array_F10():
    """Without ?alt=sse, Gemini streamGenerateContent returns a JSON array of
    GenerateContentResponse (non-SSE REST clients parse this), not SSE frames."""
    mock = AsyncMock(return_value=_resp("gem-array", model="gemini-2.5-flash"))
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    resp = _drive(mock, "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert isinstance(data, list) and data
    assert data[0]["candidates"][0]["content"]["parts"][0]["text"] == "gem-array"


def test_openai_route_rejects_query_key_F2():
    """?key= is a Gemini-only credential channel — it must NOT authenticate on the
    OpenAI route (where it would only leak the proxy key into access logs)."""
    mock = AsyncMock(return_value=_resp())
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    resp = _drive(mock, f"/v1/chat/completions?key={_KEY}", {}, body)
    assert resp.status_code == 401


def test_openai_route_rejects_x_api_key_F2():
    """x-api-key is an Anthropic-only channel — not accepted on the OpenAI route."""
    mock = AsyncMock(return_value=_resp())
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    resp = _drive(mock, "/v1/chat/completions", {"x-api-key": _KEY}, body)
    assert resp.status_code == 401


def test_gemini_stream_error_not_masked_as_success_F4():
    """A stream that fails emits the protocol error frame and STOPS — never a synthetic
    finishReason:STOP that would mask the upstream failure as a clean completion."""
    async def acompletion(**kwargs):
        raise litellm.exceptions.RateLimitError(
            message="rl", llm_provider="gemini", model="gemini-2.5-flash")
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    resp = _drive(acompletion, "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 200  # stream already opened
    assert '"finishReason":"STOP"' not in resp.text
    assert "UNAVAILABLE" in resp.text  # the error frame is present


# ── Structural tool round-tripping (Branch 4, 2026-07-11) ─────────────────────
# End-to-end through the real app: a prior tool_use/tool_result (Anthropic) or
# functionCall/functionResponse (Gemini) turn in the conversation history must reach
# litellm.acompletion as well-formed OpenAI tool_calls/role:"tool" messages — the
# litellm/provider 400-on-orphan constraint, verified against the ACTUAL call.

def _sent_messages(mock):
    return mock.call_args.kwargs["messages"]


def _assert_well_formed_and_json_args(messages):
    # Each tool_call id → index of the assistant message that declares it.
    declared_at = {tc["id"]: i for i, m in enumerate(messages)
                   for tc in (m.get("tool_calls") or [])}
    tool_msgs = [(i, m) for i, m in enumerate(messages) if m.get("role") == "tool"]
    assert tool_msgs, "expected at least one role:tool message in the sent conversation"
    for i, tm in tool_msgs:
        tcid = tm["tool_call_id"]
        assert tcid in declared_at, f"orphaned tool message sent to litellm: {tm}"
        # litellm/providers also 400 if a tool message precedes its declaring tool_calls.
        assert declared_at[tcid] < i, f"tool message precedes its declaring tool_calls: {tm}"
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            args = tc["function"]["arguments"]
            assert isinstance(args, str), f"arguments must be a JSON string, got {type(args)}"
            json.loads(args)  # must parse


def test_anthropic_multiturn_tool_history_reaches_litellm_well_formed():
    mock = AsyncMock(return_value=_resp("The weather is sunny."))
    body = {
        "model": "claude-3-5-haiku", "max_tokens": 100,
        "tools": [{"name": "get_weather", "description": "Get weather",
                   "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
        "messages": [
            {"role": "user", "content": "What's the weather in SF?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Sunny, 72F"}]},
        ],
    }
    resp = _drive(mock, "/v1/messages", {"x-api-key": _KEY}, body)
    assert resp.status_code == 200
    _assert_well_formed_and_json_args(_sent_messages(mock))


def test_gemini_multiturn_tool_history_reaches_litellm_well_formed():
    mock = AsyncMock(return_value=_resp("It's sunny.", model="gemini-2.5-flash"))
    body = {
        "tools": [{"functionDeclarations": [
            {"name": "get_weather", "description": "Get weather",
             "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}]}],
        "contents": [
            {"role": "user", "parts": [{"text": "What's the weather in SF?"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "SF"}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "get_weather",
                                                              "response": {"temp": "72F"}}}]},
        ],
    }
    resp = _drive(mock, "/v1beta/models/gemini-2.5-flash:generateContent",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 200
    _assert_well_formed_and_json_args(_sent_messages(mock))


# ── Structural tool round-tripping — outbound (assistant call → client) ────────

def test_anthropic_non_streaming_outbound_tool_call_end_to_end():
    tc = [{"id": "toolu_out", "type": "function",
           "function": {"name": "get_weather", "arguments": '{"city": "SF"}'}}]
    mock = AsyncMock(return_value=_resp(tool_calls=tc))
    body = {"model": "claude-3-5-haiku", "max_tokens": 50,
            "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "weather in SF?"}]}
    resp = _drive(mock, "/v1/messages", {"x-api-key": _KEY}, body)
    assert resp.status_code == 200
    data = resp.json()
    blk = [b for b in data["content"] if b["type"] == "tool_use"][0]
    assert blk["id"] == "toolu_out" and blk["name"] == "get_weather"
    assert blk["input"] == {"city": "SF"}
    assert data["stop_reason"] == "tool_use"


def test_gemini_non_streaming_outbound_tool_call_end_to_end():
    tc = [{"id": "ignored", "type": "function",
           "function": {"name": "get_weather", "arguments": '{"city": "SF"}'}}]
    mock = AsyncMock(return_value=_resp(model="gemini-2.5-flash", tool_calls=tc))
    body = {"contents": [{"role": "user", "parts": [{"text": "weather in SF?"}]}]}
    resp = _drive(mock, "/v1beta/models/gemini-2.5-flash:generateContent",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 200
    data = resp.json()
    part = data["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"]["name"] == "get_weather"
    assert part["functionCall"]["args"] == {"city": "SF"}


# ── Structural tool round-tripping — streaming outbound ────────────────────────

def _tool_stream_chunks(name="get_weather", tool_id="", args_json='{"city":"SF"}'):
    async def gen():
        delta = {"function": {"name": name, "arguments": args_json}}
        if tool_id:
            delta["id"] = tool_id
        yield {"model": "claude-3-5-haiku", "id": "msg_e2e",
               "choices": [{"delta": {"tool_calls": [{"index": 0, **delta}]}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 8}}
    return gen()


def test_gemini_streaming_tool_calls_emit_functioncall():
    async def acompletion(**kwargs):
        return _tool_stream_chunks()
    body = {"contents": [{"role": "user", "parts": [{"text": "weather in SF?"}]}]}
    resp = _drive(acompletion, "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse",
                  {"x-goog-api-key": _KEY}, body)
    assert resp.status_code == 200
    assert '"functionCall"' in resp.text
    assert "get_weather" in resp.text
    assert '"SF"' in resp.text


def test_anthropic_streaming_tool_calls_emit_tool_use():
    async def acompletion(**kwargs):
        return _tool_stream_chunks(tool_id="toolu_stream_e2e")
    body = {"model": "claude-3-5-haiku", "max_tokens": 50, "stream": True,
            "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "weather in SF?"}]}
    resp = _drive(acompletion, "/v1/messages", {"x-api-key": _KEY}, body)
    assert resp.status_code == 200
    assert '"type":"tool_use"' in resp.text
    assert '"id":"toolu_stream_e2e"' in resp.text
    assert "get_weather" in resp.text
    assert '"stop_reason":"tool_use"' in resp.text
