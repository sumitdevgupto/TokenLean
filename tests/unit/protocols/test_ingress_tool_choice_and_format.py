"""Anthropic and Gemini request fields that steer tool use and output format reach the
provider as the client meant them, and a field the proxy cannot carry is refused with a 400
naming it rather than dropped.

Anthropic `tool_choice: {type: none}` became "auto" (the model could call the tools the
client had just forbidden) and `disable_parallel_tool_use` was ignored. On the Gemini route,
`toolConfig`, JSON mode (`responseMimeType` / `responseSchema`), `topK` and `thinkingConfig`
were silently dropped.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from protocols import AnthropicProtocol, GeminiProtocol
from protocols.base import UnsupportedRequestField

_A_TOOLS = [{"name": "get_weather", "input_schema": {"type": "object"}},
            {"name": "get_time", "input_schema": {"type": "object"}}]


def _anthropic(**body):
    base = {"model": "claude-x", "max_tokens": 10, "tools": _A_TOOLS,
            "messages": [{"role": "user", "content": "hi"}]}
    return AnthropicProtocol().parse_request({**base, **body}, {}, "")[2]


@pytest.mark.parametrize("choice, expected", [
    ({"type": "none"}, "none"),
    ({"type": "auto"}, "auto"),
    ({"type": "any"}, "required"),
    ({"type": "tool", "name": "get_time"}, {"type": "function", "function": {"name": "get_time"}}),
])
def test_anthropic_tool_choice_keeps_its_meaning(choice, expected):
    assert _anthropic(tool_choice=choice)["tool_choice"] == expected


@pytest.mark.parametrize("choice", [{"type": "auto"}, {"type": "any"},
                                    {"type": "tool", "name": "get_time"}])
def test_anthropic_disable_parallel_tool_use_is_carried(choice):
    params = _anthropic(tool_choice={**choice, "disable_parallel_tool_use": True})
    assert params.get("parallel_tool_calls") is False


def test_anthropic_parallel_tool_use_stays_the_provider_default_unless_disabled():
    assert "parallel_tool_calls" not in _anthropic(tool_choice={"type": "auto"})
    assert "parallel_tool_calls" not in _anthropic(
        tool_choice={"type": "auto", "disable_parallel_tool_use": False})


@pytest.mark.parametrize("choice", [{"type": "sometimes"}, {"type": "tool"}, "auto", ["any"]])
def test_anthropic_tool_choice_it_cannot_carry_is_refused(choice):
    with pytest.raises(UnsupportedRequestField, match="tool_choice"):
        _anthropic(tool_choice=choice)


_G_TOOLS = [{"functionDeclarations": [
    {"name": "get_weather", "parameters": {"type": "OBJECT"}},
    {"name": "get_time", "parameters": {"type": "OBJECT"}},
    {"name": "get_date", "parameters": {"type": "OBJECT"}}]}]


def _gemini(**body):
    base = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "tools": _G_TOOLS}
    return GeminiProtocol().parse_request({**base, **body}, {}, "gemini-x")[2]


def _names(params):
    return [t["function"]["name"] for t in params["tools"]]


@pytest.mark.parametrize("mode, expected", [("NONE", "none"), ("AUTO", "auto"), ("ANY", "required")])
def test_gemini_function_calling_mode_is_carried(mode, expected):
    params = _gemini(toolConfig={"functionCallingConfig": {"mode": mode}})
    assert params.get("tool_choice") == expected
    assert _names(params) == ["get_weather", "get_time", "get_date"]


def test_gemini_allowed_function_names_narrow_the_tools():
    params = _gemini(toolConfig={"functionCallingConfig": {
        "mode": "ANY", "allowedFunctionNames": ["get_time", "get_date"]}})
    assert params.get("tool_choice") == "required"
    assert _names(params) == ["get_time", "get_date"]


def test_gemini_one_allowed_function_is_a_named_choice():
    params = _gemini(tool_config={"function_calling_config": {
        "mode": "ANY", "allowed_function_names": ["get_time"]}})
    assert params.get("tool_choice") == {"type": "function", "function": {"name": "get_time"}}
    assert _names(params) == ["get_time"]


@pytest.mark.parametrize("config", [
    {"functionCallingConfig": {"mode": "SOMETIMES"}},
    {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["no_such_tool"]}},
])
def test_gemini_tool_config_it_cannot_carry_is_refused(config):
    with pytest.raises(UnsupportedRequestField, match="toolConfig"):
        _gemini(toolConfig=config)


def test_gemini_json_mode_is_carried():
    params = _gemini(generationConfig={"responseMimeType": "application/json"})
    assert params.get("response_format") == {"type": "json_object"}


def test_gemini_response_schema_becomes_a_json_schema():
    schema = {"type": "OBJECT", "properties": {"city": {"type": "STRING"},
                                               "days": {"type": "ARRAY", "items": {"type": "INTEGER"}}}}
    params = _gemini(generationConfig={"responseMimeType": "application/json",
                                       "responseSchema": schema})
    assert params.get("response_format") == {"type": "json_schema", "json_schema": {
        "name": "response", "schema": {"type": "object", "properties": {
            "city": {"type": "string"},
            "days": {"type": "array", "items": {"type": "integer"}}}}}}


def test_gemini_response_json_schema_passes_as_written():
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    params = _gemini(generation_config={"response_mime_type": "application/json",
                                        "response_json_schema": schema})
    assert params.get("response_format", {}).get("json_schema", {}).get("schema") == schema


def test_gemini_plain_text_needs_no_format():
    assert "response_format" not in _gemini(generationConfig={"responseMimeType": "text/plain"})


def test_gemini_sampling_and_thinking_fields_are_carried():
    params = _gemini(generationConfig={
        "topK": 40, "candidateCount": 2, "seed": 7, "presencePenalty": 0.5,
        "frequencyPenalty": 0.25, "thinkingConfig": {"thinkingBudget": 0, "includeThoughts": True}})
    carried = {k: params.get(k) for k in ("top_k", "n", "seed", "presence_penalty",
                                          "frequency_penalty", "thinking_config")}
    assert carried == {"top_k": 40, "n": 2, "seed": 7, "presence_penalty": 0.5,
                       "frequency_penalty": 0.25,
                       "thinking_config": {"thinking_budget": 0, "include_thoughts": True}}


@pytest.mark.parametrize("gen", [
    {"responseMimeType": "text/x.enum"},
    {"responseModalities": ["AUDIO"]},
    {"speechConfig": {"voiceConfig": {}}},
])
def test_gemini_generation_config_it_cannot_carry_is_refused(gen):
    with pytest.raises(UnsupportedRequestField, match="generationConfig"):
        _gemini(generationConfig=gen)


def test_gemini_an_unset_field_is_not_refused():
    # A client library that sends every field, unset ones as null, asks for nothing.
    params = _gemini(generationConfig={"responseModalities": None, "speechConfig": None,
                                       "temperature": 0.1})
    assert params.get("temperature") == 0.1


def test_gemini_existing_fields_are_unchanged():
    params = _gemini(generationConfig={"maxOutputTokens": 50, "temperature": 0.2, "topP": 0.9,
                                       "stopSequences": ["END"]})
    assert (params["max_tokens"], params["temperature"], params["top_p"], params["stop"]) == (
        50, 0.2, 0.9, ["END"])
