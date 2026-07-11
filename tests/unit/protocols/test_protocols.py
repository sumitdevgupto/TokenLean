"""Unit tests for the multi-protocol ingress translators (#4)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest

from protocols import get_protocol, OpenAIProtocol, AnthropicProtocol, GeminiProtocol


def _openai_response(content="Hello", finish="stop", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


def _drain(translator, chunks):
    out = []
    out += list(translator.start())
    for c in chunks:
        out += list(translator.chunk(c))
    out += list(translator.finish())
    return "".join(out)


# ── Registry ────────────────────────────────────────────────────────────────────
def test_registry_resolves_and_defaults_to_openai():
    assert get_protocol("anthropic").name == "anthropic"
    assert get_protocol("gemini").name == "gemini"
    assert get_protocol("openai").name == "openai"
    assert get_protocol("unknown").name == "openai"
    assert get_protocol("").name == "openai"


# ── OpenAI identity ──────────────────────────────────────────────────────────────
def test_openai_identity_parse_and_serialise():
    p = OpenAIProtocol()
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5, "stream": False}
    messages, model, params = p.parse_request(body)
    assert model == "gpt-4o-mini"
    assert messages == [{"role": "user", "content": "hi"}]
    assert params == {"temperature": 0.5, "stream": False}
    resp = _openai_response()
    assert p.serialise_response(resp) is resp  # unchanged


def test_openai_stream_is_data_frames_plus_done():
    body = _drain(OpenAIProtocol().stream_translator(),
                  [{"choices": [{"delta": {"content": "Hi"}}]}])
    assert 'data: {"choices"' in body
    assert body.strip().endswith("data: [DONE]")


# ── Anthropic request ────────────────────────────────────────────────────────────
def test_anthropic_parse_system_and_text():
    body = {"model": "claude-3-5-sonnet", "max_tokens": 100, "system": "Be terse.",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.2, "stop_sequences": ["X"], "stream": True}
    msgs, model, params = AnthropicProtocol().parse_request(body)
    assert model == "claude-3-5-sonnet"
    assert msgs[0] == {"role": "system", "content": "Be terse."}
    assert msgs[1] == {"role": "user", "content": "hello"}
    assert params["max_tokens"] == 100 and params["temperature"] == 0.2
    assert params["stop"] == ["X"] and params["stream"] is True


def test_anthropic_parse_block_list_all_text_collapses_to_string():
    body = {"messages": [{"role": "user",
                          "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert msgs[0]["content"] == "ab"


def test_anthropic_parse_image_block_becomes_openai_data_uri():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}},
    ]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    parts = msgs[0]["content"]
    assert isinstance(parts, list)
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,QQ=="


def test_anthropic_parse_tools():
    body = {"messages": [], "tools": [
        {"name": "get_weather", "description": "w", "input_schema": {"type": "object"}}]}
    _, _, params = AnthropicProtocol().parse_request(body)
    assert params["tools"][0]["function"]["name"] == "get_weather"
    assert params["tools"][0]["function"]["parameters"] == {"type": "object"}


# ── Anthropic response ───────────────────────────────────────────────────────────
def test_anthropic_serialise_text_response():
    out = AnthropicProtocol().serialise_response(_openai_response("Hi there", "stop"))
    assert out["type"] == "message" and out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "Hi there"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 3}


def test_anthropic_serialise_maps_stop_reasons():
    a = AnthropicProtocol()
    assert a.serialise_response(_openai_response(finish="length"))["stop_reason"] == "max_tokens"
    assert a.serialise_response(_openai_response(finish="tool_calls"))["stop_reason"] == "tool_use"


def test_anthropic_serialise_tool_call():
    tc = [{"id": "t1", "type": "function",
           "function": {"name": "f", "arguments": '{"x": 1}'}}]
    out = AnthropicProtocol().serialise_response(_openai_response(content=None, finish="tool_calls", tool_calls=tc))
    blk = [b for b in out["content"] if b["type"] == "tool_use"][0]
    assert blk["name"] == "f" and blk["input"] == {"x": 1}


def test_anthropic_error_envelope():
    body, status = AnthropicProtocol().serialise_error(429, "slow down")
    assert status == 429
    assert body == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}


def test_anthropic_stream_frames():
    body = _drain(AnthropicProtocol().stream_translator(), [
        {"model": "claude-3-5-sonnet", "id": "msg_1", "choices": [{"delta": {"content": "He"}}]},
        {"choices": [{"delta": {"content": "llo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ])
    assert "event: message_start" in body
    assert "event: content_block_start" in body
    assert body.count("event: content_block_delta") == 2
    assert '"text":"He"' in body and '"text":"llo"' in body
    assert "event: content_block_stop" in body
    assert "event: message_delta" in body and '"stop_reason":"end_turn"' in body
    assert body.strip().endswith("event: message_stop\ndata: " + json.dumps(
        {"type": "message_stop"}, separators=(",", ":")))


# ── Gemini request ───────────────────────────────────────────────────────────────
def test_gemini_parse_contents_and_system_and_config():
    body = {
        "systemInstruction": {"parts": [{"text": "sys"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "prev"}]},
        ],
        "generationConfig": {"maxOutputTokens": 64, "temperature": 0.1, "topP": 0.9,
                             "stopSequences": ["Z"]},
    }
    msgs, model, params = GeminiProtocol().parse_request(body, path_model="gemini-2.5-flash")
    assert model == "gemini-2.5-flash"
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2] == {"role": "assistant", "content": "prev"}   # model → assistant
    assert params["max_tokens"] == 64 and params["temperature"] == 0.1
    assert params["top_p"] == 0.9 and params["stop"] == ["Z"]


def test_gemini_parse_strips_models_prefix_and_inline_image():
    body = {"contents": [{"role": "user", "parts": [
        {"text": "x"},
        {"inlineData": {"mimeType": "image/png", "data": "QQ=="}},
    ]}]}
    msgs, model, _ = GeminiProtocol().parse_request(body, path_model="models/gemini-1.5-pro")
    assert model == "gemini-1.5-pro"
    assert msgs[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,QQ=="


def test_gemini_parse_function_declarations():
    body = {"contents": [], "tools": [{"functionDeclarations": [
        {"name": "lookup", "description": "d", "parameters": {"type": "object"}}]}]}
    _, _, params = GeminiProtocol().parse_request(body)
    assert params["tools"][0]["function"]["name"] == "lookup"


# ── Gemini response ──────────────────────────────────────────────────────────────
def test_gemini_serialise_response():
    out = GeminiProtocol().serialise_response(_openai_response("Answer", "stop"))
    cand = out["candidates"][0]
    assert cand["content"] == {"role": "model", "parts": [{"text": "Answer"}]}
    assert cand["finishReason"] == "STOP"
    assert out["usageMetadata"] == {"promptTokenCount": 10, "candidatesTokenCount": 3,
                                    "totalTokenCount": 13}


def test_gemini_serialise_maps_finish_reasons():
    g = GeminiProtocol()
    assert g.serialise_response(_openai_response(finish="length"))["candidates"][0]["finishReason"] == "MAX_TOKENS"
    assert g.serialise_response(_openai_response(finish="content_filter"))["candidates"][0]["finishReason"] == "SAFETY"


def test_gemini_error_envelope():
    body, status = GeminiProtocol().serialise_error(429, "quota")
    assert status == 429
    assert body["error"] == {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}


def test_gemini_stream_frames_carry_final_usage():
    body = _drain(GeminiProtocol().stream_translator(), [
        {"model": "gemini-2.5-flash", "choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 1}},
    ])
    frames = [json.loads(line[len("data: "):]) for line in body.strip().split("\n\n") if line.startswith("data: ")]
    assert frames[0]["candidates"][0]["content"]["parts"][0]["text"] == "Hi"
    assert frames[-1]["candidates"][0]["finishReason"] == "STOP"
    assert frames[-1]["usageMetadata"]["totalTokenCount"] == 5


def test_stream_error_paths_emit_protocol_shaped_error():
    a = "".join(AnthropicProtocol().stream_translator().error("boom"))
    assert "event: error" in a and "api_error" in a
    g = "".join(GeminiProtocol().stream_translator().error("boom"))
    assert '"status":"UNAVAILABLE"' in g


# ── Review fixes (Branch-3 code review, 2026-07-11) ──────────────────────────────
def test_anthropic_stream_emits_tool_use_blocks_F6():
    """Streamed OpenAI tool_call deltas → complete Anthropic tool_use content blocks
    (previously dropped, leaving stop_reason:tool_use with zero tool blocks)."""
    chunks = [
        {"model": "claude-3-5-haiku", "id": "msg_1",
         "choices": [{"delta": {"tool_calls": [
             {"index": 0, "id": "toolu_1", "function": {"name": "get_weather", "arguments": "{\"ci"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
             {"index": 0, "function": {"arguments": "ty\":\"SF\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 8}},
    ]
    out = _drain(AnthropicProtocol().stream_translator(), chunks)
    # A real tool_use block with id + name is emitted, and the input JSON is reassembled.
    assert '"type":"tool_use"' in out
    assert '"id":"toolu_1"' in out and '"name":"get_weather"' in out
    assert '"input_json_delta"' in out
    assert '{\\"city\\":\\"SF\\"}' in out or '{"city":"SF"}' in json.dumps(out)
    # stop_reason still maps to tool_use — now consistent with a block actually present.
    assert '"stop_reason":"tool_use"' in out


def test_gemini_function_parts_degrade_to_text_F7():
    """Gemini functionCall / functionResponse parts render as text instead of vanishing
    into an empty message (parity with the Anthropic tool_result degradation)."""
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "lookup",
                                                          "response": {"result": 42}}}]},
    ]}
    messages, _model, _params = GeminiProtocol().parse_request(body, {}, "gemini-2.5-flash")
    joined = " ".join(m["content"] for m in messages if isinstance(m.get("content"), str))
    assert "functionCall lookup" in joined
    assert "functionResponse lookup" in joined
    assert "42" in joined
    # Neither turn collapsed to empty content.
    assert all(m.get("content") for m in messages)


def test_credential_channels_scoped_per_protocol_F2():
    """?key= / x-api-key are declared only on the protocols whose SDKs use them, so
    _authenticate can scope them per-route (OpenAI = Bearer-only)."""
    assert OpenAIProtocol().credential_headers == ()
    assert OpenAIProtocol().credential_query_param == ""
    assert AnthropicProtocol().credential_headers == ("x-api-key",)
    assert GeminiProtocol().credential_headers == ("x-goog-api-key",)
    assert GeminiProtocol().credential_query_param == "key"
