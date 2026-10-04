"""A caller's own prompt-cache markers are noticed on the request as it arrives.

G18 prices the savings baseline (the caller's own request, sent straight to the model it
asked for) with the provider's prompt-cache discount whenever that request would have been
cached without the proxy. A provider such as Anthropic caches only a prompt the request
marks, so whether the caller marked it decides whether the discount is the proxy's saving.
Translation can drop a marker (the Anthropic adapter flattens system and text blocks), so
the markers are read from the body before it is translated."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from protocols import AnthropicProtocol, OpenAIProtocol
from protocols.base import CALLER_CACHE_MARKERS, carries_cache_markers

MARK = {"type": "ephemeral"}
USER = {"role": "user", "content": "hi"}


def _openai(**extra):
    return {"model": "claude-sonnet-4-5", "messages": [USER], **extra}


def _anthropic(**extra):
    return {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [USER], **extra}


OPENAI_MARKED = {
    "message": _openai(messages=[
        {"role": "system", "content": "rules", "cache_control": MARK}, USER]),
    "content part": _openai(messages=[
        {"role": "system", "content": [{"type": "text", "text": "rules", "cache_control": MARK}]},
        USER]),
    "tool": _openai(tools=[
        {"type": "function", "function": {"name": "f", "parameters": {}}, "cache_control": MARK}]),
}

ANTHROPIC_MARKED = {
    "system block": _anthropic(system=[{"type": "text", "text": "rules", "cache_control": MARK}]),
    "content block": _anthropic(messages=[
        {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": MARK}]}]),
    "block inside a tool_result": _anthropic(messages=[
        USER,
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "output", "cache_control": MARK}]}]},
    ]),
    "tool": _anthropic(tools=[
        {"name": "f", "input_schema": {"type": "object"}, "cache_control": MARK}]),
}


@pytest.mark.parametrize("body", OPENAI_MARKED.values(), ids=OPENAI_MARKED.keys())
def test_the_openai_ingress_flags_a_request_the_caller_marked(body):
    _, _, params = OpenAIProtocol().parse_request(body)
    assert params.get(CALLER_CACHE_MARKERS) is True


@pytest.mark.parametrize("body", ANTHROPIC_MARKED.values(), ids=ANTHROPIC_MARKED.keys())
def test_the_anthropic_ingress_flags_a_request_the_caller_marked(body):
    _, _, params = AnthropicProtocol().parse_request(body)
    assert params.get(CALLER_CACHE_MARKERS) is True


def test_an_unmarked_request_is_not_flagged():
    tool = {"type": "function", "function": {"name": "f", "parameters": {}}}
    _, _, params = OpenAIProtocol().parse_request(_openai(tools=[tool]))
    assert CALLER_CACHE_MARKERS not in params
    _, _, params = AnthropicProtocol().parse_request(
        _anthropic(system=[{"type": "text", "text": "rules"}],
                   tools=[{"name": "f", "input_schema": {"type": "object"}}]))
    assert CALLER_CACHE_MARKERS not in params


def test_a_request_level_marker_counts():
    # Anthropic's automatic caching: one marker on the request, placed by the provider.
    _, _, params = AnthropicProtocol().parse_request(_anthropic(cache_control=MARK))
    assert params.get(CALLER_CACHE_MARKERS) is True
    assert carries_cache_markers(_openai(cache_control=MARK)) is True


@pytest.mark.parametrize("body", [
    _openai(messages=[{"role": "system", "content": "rules", "cache_control": None}, USER]),
    _anthropic(system=[{"type": "text", "text": "rules", "cache_control": None}]),
    _anthropic(cache_control=None),
    _anthropic(tools=[{"name": "f", "input_schema": {"type": "object"}, "cache_control": "on"}]),
], ids=["null on a message", "null on a system block", "null on the request", "not an object"])
def test_a_marker_that_is_not_an_object_does_not_count(body):
    assert carries_cache_markers(body) is False


def test_a_client_cannot_set_the_flag_itself():
    _, _, params = OpenAIProtocol().parse_request(_openai(**{CALLER_CACHE_MARKERS: True}))
    assert CALLER_CACHE_MARKERS not in params


def test_a_tool_parameter_named_cache_control_is_not_a_marker():
    schema = {"type": "object", "properties": {"cache_control": {"type": "string"}}}
    body = _openai(tools=[{"type": "function", "function": {"name": "f", "parameters": schema}}])
    assert carries_cache_markers(body) is False


@pytest.mark.parametrize("body", [
    None, [], "cache_control", {"messages": "cache_control"}, {"messages": [None, 3, "x"]},
    {"messages": [{"role": "user", "content": {"cache_control": MARK}}]},
    {"tools": {"cache_control": MARK}}, {"system": "cache_control"},
])
def test_malformed_shapes_are_not_markers_and_never_raise(body):
    try:
        found = carries_cache_markers(body)
    except Exception as exc:  # the ingress must never fail a request over this check
        pytest.fail(f"raised {exc!r}")
    assert found is False
