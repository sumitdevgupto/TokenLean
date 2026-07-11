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


def test_gemini_function_parts_roundtrip_structurally_F7():
    """Gemini functionCall / functionResponse parts round-trip STRUCTURALLY (not as
    text): a model-turn functionCall becomes the assistant's own OpenAI tool_calls
    (with a synthesised id — Gemini parts carry none), and the following
    functionResponse becomes a role:"tool" message correlated to that call."""
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "lookup",
                                                          "response": {"result": 42}}}]},
    ]}
    messages, _model, _params = GeminiProtocol().parse_request(body, {}, "gemini-2.5-flash")
    assert len(messages) == 2
    call_msg, tool_msg = messages
    assert call_msg["role"] == "assistant" and call_msg["content"] is None
    tc = call_msg["tool_calls"][0]
    assert tc["type"] == "function" and tc["function"]["name"] == "lookup"
    assert json.loads(tc["function"]["arguments"]) == {"q": "x"}
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == tc["id"]
    assert json.loads(tool_msg["content"]) == {"result": 42}


def test_credential_channels_scoped_per_protocol_F2():
    """?key= / x-api-key are declared only on the protocols whose SDKs use them, so
    _authenticate can scope them per-route (OpenAI = Bearer-only)."""
    assert OpenAIProtocol().credential_headers == ()
    assert OpenAIProtocol().credential_query_param == ""
    assert AnthropicProtocol().credential_headers == ("x-api-key",)
    assert GeminiProtocol().credential_headers == ("x-goog-api-key",)
    assert GeminiProtocol().credential_query_param == "key"


# ── Structural tool round-tripping (Branch 4, 2026-07-11) ────────────────────────
# Inbound tool_use/tool_result (Anthropic) and functionCall/functionResponse (Gemini)
# now fan out into well-formed OpenAI tool_calls/role:"tool" messages instead of
# degrading to text — see protocols/anthropic_ingress.py and gemini_ingress.py.

def _tool_call_ids(messages):
    return {tc["id"] for m in messages for tc in (m.get("tool_calls") or [])}


def _assert_well_formed(messages):
    """Every role:"tool" message must (a) match some assistant tool_calls[].id and
    (b) appear AFTER the assistant message that declares that id — both constraints
    litellm/providers enforce (an orphan OR an out-of-order tool message → 400)."""
    # Map each tool_call id to the index of the assistant message that declares it.
    declared_at = {}
    for i, m in enumerate(messages):
        for tc in (m.get("tool_calls") or []):
            declared_at[tc["id"]] = i
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            tcid = m["tool_call_id"]
            assert tcid in declared_at, f"orphaned tool message (no declaring call): {m}"
            assert declared_at[tcid] < i, f"tool message precedes its declaring tool_calls: {m}"


# --- Anthropic inbound fan-out ---------------------------------------------------

def test_anthropic_parse_tool_use_becomes_tool_calls():
    body = {"messages": [{"role": "assistant", "content": [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}},
    ]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant" and msgs[0]["content"] == "Let me check."
    tc = msgs[0]["tool_calls"][0]
    assert tc["id"] == "toolu_1" and tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "SF"}


def test_anthropic_parse_tool_use_only_content_is_null():
    body = {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
    ]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert msgs[0]["content"] is None


def test_anthropic_parse_tool_result_becomes_role_tool():
    body = {"messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Sunny, 72F"}]},
    ]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert len(msgs) == 2
    assert msgs[1] == {"role": "tool", "tool_call_id": "toolu_1", "content": "Sunny, 72F"}
    _assert_well_formed(msgs)


def test_anthropic_parse_tool_result_content_block_list_flattens_to_text():
    body = {"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "part-a"}, {"type": "text", "text": "part-b"}]}]},
    ]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert msgs[1]["content"] == "part-apart-b"


def test_anthropic_parse_parallel_tool_calls_and_results():
    body = {"messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {"city": "SF"}},
            {"type": "tool_use", "id": "t2", "name": "get_time", "input": {"tz": "PST"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Sunny"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "3pm"},
        ]},
    ]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert len(msgs[0]["tool_calls"]) == 2
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {"t1", "t2"}
    _assert_well_formed(msgs)


def test_anthropic_parse_orphan_tool_result_degrades_to_text():
    """A tool_result with no matching prior tool_use is malformed input -- degrade to
    text rather than emit an orphaned role:"tool" (litellm/providers 400 on that)."""
    body = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_ghost", "content": "orphaned"}]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "toolu_ghost" in msgs[0]["content"] and "orphaned" in msgs[0]["content"]
    assert not any(m.get("role") == "tool" for m in msgs)


def test_anthropic_parse_text_only_conversation_is_byte_identical():
    """No-tool conversations are unaffected by the fan-out -- locks backwards compat."""
    body = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
    ]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    assert msgs == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "ab"},
    ]


def test_anthropic_serialise_then_parse_roundtrip_preserves_tool_call():
    """serialise_response (outbound) -> wrap as an inbound turn -> parse_request: id,
    name, and args survive the round trip."""
    tc = [{"id": "toolu_rt", "type": "function",
           "function": {"name": "search", "arguments": '{"q": "docs"}'}}]
    outbound = AnthropicProtocol().serialise_response(
        _openai_response(content=None, finish="tool_calls", tool_calls=tc))
    body = {"messages": [{"role": "assistant", "content": outbound["content"]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    rt = msgs[0]["tool_calls"][0]
    assert rt["id"] == "toolu_rt" and rt["function"]["name"] == "search"
    assert json.loads(rt["function"]["arguments"]) == {"q": "docs"}


# --- Anthropic streaming tool_calls (already fixed pre-Branch-4; parity check) ---

def test_anthropic_stream_tool_use_is_well_formed_against_prior_call():
    """The id the stream accumulates matches what parse_request would echo back as
    tool_use_id on the client's next turn -- end-to-end id consistency."""
    chunks = [
        {"model": "claude-3-5-haiku", "id": "msg_1",
         "choices": [{"delta": {"tool_calls": [
             {"index": 0, "id": "toolu_rt2", "function": {"name": "f", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    out = _drain(AnthropicProtocol().stream_translator(), chunks)
    assert '"id":"toolu_rt2"' in out
    # That id is exactly what a client's next-turn tool_result.tool_use_id would carry.
    body = {"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_rt2", "name": "f", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_rt2", "content": "ok"}]},
    ]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    _assert_well_formed(msgs)


# --- Gemini inbound fan-out -------------------------------------------------------

def test_gemini_parse_functioncall_becomes_tool_calls():
    body = {"contents": [{"role": "model", "parts": [
        {"functionCall": {"name": "lookup", "args": {"q": "x"}}}]}]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    tc = msgs[0]["tool_calls"][0]
    assert tc["id"] == "call_0" and tc["function"]["name"] == "lookup"
    assert json.loads(tc["function"]["arguments"]) == {"q": "x"}


def test_gemini_parse_parallel_correlation_by_name_and_order():
    """Two functionCalls of the SAME name, answered by two functionResponses of the
    same name -- Gemini has no id, so correlation is FIFO per function name."""
    body = {"contents": [
        {"role": "model", "parts": [
            {"functionCall": {"name": "search", "args": {"q": "a"}}},
            {"functionCall": {"name": "search", "args": {"q": "b"}}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "search", "response": {"r": "result-for-a"}}},
            {"functionResponse": {"name": "search", "response": {"r": "result-for-b"}}},
        ]},
    ]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    call_ids = [tc["id"] for tc in msgs[0]["tool_calls"]]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == call_ids  # FIFO order preserved
    assert json.loads(tool_msgs[0]["content"]) == {"r": "result-for-a"}
    assert json.loads(tool_msgs[1]["content"]) == {"r": "result-for-b"}
    _assert_well_formed(msgs)


def test_gemini_parse_orphan_functionresponse_degrades_to_text():
    body = {"contents": [{"role": "user", "parts": [
        {"functionResponse": {"name": "ghost", "response": {"x": 1}}}]}]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "ghost" in msgs[0]["content"]
    assert not any(m.get("role") == "tool" for m in msgs)


def test_gemini_parse_text_only_conversation_is_byte_identical():
    body = {"contents": [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "prev"}]},
    ]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "prev"},
    ]


def test_gemini_serialise_then_parse_roundtrip_preserves_name_and_args():
    """Gemini has no id on functionCall, so a round trip re-synthesises one -- name and
    args survive; the pairing is still well-formed."""
    tc = [{"id": "ignored", "type": "function",
           "function": {"name": "search", "arguments": '{"q": "docs"}'}}]
    outbound = GeminiProtocol().serialise_response(
        _openai_response(content=None, finish="tool_calls", tool_calls=tc))
    fc_part = outbound["candidates"][0]["content"]["parts"][0]
    assert "functionCall" in fc_part and "id" not in fc_part["functionCall"]
    body = {"contents": [{"role": "model", "parts": [fc_part]}]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    rt = msgs[0]["tool_calls"][0]
    assert rt["function"]["name"] == "search"
    assert json.loads(rt["function"]["arguments"]) == {"q": "docs"}


# --- Gemini streaming tool_calls (new -- was text-only before Branch 4) ----------

def test_gemini_stream_emits_functioncall_parts():
    chunks = [
        {"model": "gemini-2.5-flash",
         "choices": [{"delta": {"tool_calls": [
             {"index": 0, "function": {"name": "get_weather", "arguments": "{\"ci"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
             {"index": 0, "function": {"arguments": "ty\":\"SF\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 8}},
    ]
    out = _drain(GeminiProtocol().stream_translator(), chunks)
    frames = [json.loads(line[len("data: "):]) for line in out.strip().split("\n\n") if line.startswith("data: ")]
    last = frames[-1]
    fc = last["candidates"][0]["content"]["parts"][0]["functionCall"]
    assert fc["name"] == "get_weather"
    assert fc["args"] == {"city": "SF"}
    assert last["candidates"][0]["finishReason"] == "STOP"
    assert last["usageMetadata"]["totalTokenCount"] == 13


def test_gemini_stream_single_chunk_carries_tool_call_and_finish():
    """The force-stream/cache-hit synthesis path (_completion_to_stream_chunk) delivers
    tool_calls and finish_reason in ONE chunk -- accumulate-then-emit must handle it."""
    chunks = [
        {"model": "gemini-2.5-flash",
         "choices": [{"delta": {"tool_calls": [
             {"index": 0, "function": {"name": "get_weather", "arguments": '{"city":"SF"}'}}]},
             "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 8}},
    ]
    out = _drain(GeminiProtocol().stream_translator(), chunks)
    frames = [json.loads(line[len("data: "):]) for line in out.strip().split("\n\n") if line.startswith("data: ")]
    fc = frames[-1]["candidates"][0]["content"]["parts"][0]["functionCall"]
    assert fc["name"] == "get_weather" and fc["args"] == {"city": "SF"}


def test_gemini_stream_text_only_still_matches_prior_behavior():
    """Regression guard: a stream with no tool_calls at all behaves exactly as before
    (single text part, no functionCall key)."""
    chunks = [
        {"model": "gemini-2.5-flash", "choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 1}},
    ]
    out = _drain(GeminiProtocol().stream_translator(), chunks)
    frames = [json.loads(line[len("data: "):]) for line in out.strip().split("\n\n") if line.startswith("data: ")]
    assert frames[0]["candidates"][0]["content"]["parts"][0]["text"] == "Hi"
    assert "functionCall" not in frames[0]["candidates"][0]["content"]["parts"][0]
    assert frames[-1]["candidates"][0]["content"]["parts"] == [{"text": ""}]
    assert frames[-1]["candidates"][0]["finishReason"] == "STOP"


# ── Branch 4 review fixes (2026-07-11) ───────────────────────────────────────────

def test_gemini_multicycle_fifo_correlation_across_turns():
    """FIFO-by-name must correlate the RIGHT response to the RIGHT call across multiple
    call/response cycles (drain-and-refill), not just within one turn pair."""
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "search", "args": {"q": "a"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "search", "response": {"r": "res-a"}}}]},
        {"role": "model", "parts": [{"functionCall": {"name": "search", "args": {"q": "b"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "search", "response": {"r": "res-b"}}}]},
    ]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    _assert_well_formed(msgs)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    calls = [tc for m in msgs for tc in (m.get("tool_calls") or [])]
    # cycle 1: call_0 (q=a) answered by res-a; cycle 2: call_1 (q=b) answered by res-b
    assert calls[0]["id"] == "call_0" and json.loads(calls[0]["function"]["arguments"]) == {"q": "a"}
    assert tool_msgs[0]["tool_call_id"] == "call_0" and json.loads(tool_msgs[0]["content"]) == {"r": "res-a"}
    assert calls[1]["id"] == "call_1" and json.loads(calls[1]["function"]["arguments"]) == {"q": "b"}
    assert tool_msgs[1]["tool_call_id"] == "call_1" and json.loads(tool_msgs[1]["content"]) == {"r": "res-b"}


def test_gemini_model_turn_text_plus_functioncall_preserves_both():
    """A model turn mixing prose and a functionCall keeps the text as assistant content
    AND emits the tool_call (parity with the Anthropic text+tool_use case)."""
    body = {"contents": [{"role": "model", "parts": [
        {"text": "Let me look that up."},
        {"functionCall": {"name": "search", "args": {"q": "x"}}},
    ]}]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant" and msgs[0]["content"] == "Let me look that up."
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "search"


def test_gemini_functionresponse_non_dict_is_json_serialised():
    """functionResponse.response may be a scalar/list, not a dict — it must still
    serialise to a valid JSON string tool content (no crash, no blanking)."""
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "calc", "args": {}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "calc", "response": [1, 2, 3]}}]},
    ]}
    msgs, _, _ = GeminiProtocol().parse_request(body)
    tool_msg = [m for m in msgs if m.get("role") == "tool"][0]
    assert json.loads(tool_msg["content"]) == [1, 2, 3]


def test_same_turn_tool_use_and_result_does_not_emit_out_of_order_tool_msg():
    """A single turn carrying BOTH a tool_use and a same-id tool_result is malformed —
    the result must NOT correlate (that would put role:"tool" before its assistant
    tool_calls). It degrades to text; no role:"tool" is emitted."""
    body = {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
    ]}]}
    msgs, _, _ = AnthropicProtocol().parse_request(body)
    _assert_well_formed(msgs)  # would fail if a role:tool preceded its assistant
    assert not any(m.get("role") == "tool" for m in msgs)
    # The tool_use still becomes a structured call; the same-turn result degrades to text.
    assert msgs[0]["tool_calls"][0]["id"] == "t1"


def test_gemini_stream_finish_chunk_keeps_text_alongside_tool_call():
    """When a finish chunk carries BOTH text and a tool_call, the narration text is
    preserved in the terminal frame (not shadowed by the functionCall)."""
    chunks = [
        {"model": "gemini-2.5-flash",
         "choices": [{"delta": {"content": "Here is the forecast",
                                 "tool_calls": [{"index": 0, "function": {
                                     "name": "get_weather", "arguments": '{"city":"SF"}'}}]},
                      "finish_reason": "tool_calls"}]},
    ]
    out = _drain(GeminiProtocol().stream_translator(), chunks)
    frames = [json.loads(line[len("data: "):]) for line in out.strip().split("\n\n") if line.startswith("data: ")]
    parts = frames[-1]["candidates"][0]["content"]["parts"]
    assert {"text": "Here is the forecast"} in parts
    assert any("functionCall" in p and p["functionCall"]["name"] == "get_weather" for p in parts)


def test_stream_accumulator_tolerates_null_index():
    """A tool_call delta with an explicit index:null must not crash the accumulator
    (sorted() over a None key would TypeError)."""
    for proto in (AnthropicProtocol(), GeminiProtocol()):
        chunks = [
            {"model": "m", "id": "x", "choices": [{"delta": {"tool_calls": [
                {"index": None, "id": "t", "function": {"name": "f", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        out = _drain(proto.stream_translator(), chunks)  # must not raise
        assert "f" in out
