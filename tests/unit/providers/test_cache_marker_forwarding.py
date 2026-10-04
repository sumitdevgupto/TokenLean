"""Prompt-cache markers go only to a provider that caches by marker.

A `cache_control` marker is an Anthropic cache breakpoint. litellm hands it on to Anthropic
and Bedrock, strips it for OpenAI, turns it into a separately created explicit cache for
Gemini (billed by the hour, and priced by nothing here), and passes it to a custom
OpenAI-compatible endpoint that may reject it. So a request's messages, tools and
request-level marker reach a provider with their markers only when its adapter caches by
marker (`prompt_cache_needs_marker`); every other provider gets them without."""
import copy
import json
from types import SimpleNamespace

import pytest

from providers import (
    build_batch_jsonl, get_adapter_by_name, outgoing_messages_for, outgoing_params_for,
    without_cache_markers,
)

MARK = {"type": "ephemeral"}
MARKED = [
    {"role": "system", "content": [{"type": "text", "text": "rules", "cache_control": MARK}]},
    {"role": "user", "content": "hi", "cache_control": MARK},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"},
         "cache_control": MARK}]},
    {"role": "tool", "tool_call_id": "t1", "content": [
        {"type": "text", "text": "out", "cache_control": MARK}]},
]
UNMARKED = [
    {"role": "system", "content": [{"type": "text", "text": "rules"}]},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "out"}]},
]
TOOLS = [
    {"type": "function", "function": {"name": "f", "parameters": {}}},
    {"type": "function", "function": {"name": "g", "parameters": {}, "cache_control": MARK},
     "cache_control": MARK},
]
BARE_TOOLS = [
    {"type": "function", "function": {"name": "f", "parameters": {}}},
    {"type": "function", "function": {"name": "g", "parameters": {}}},
]


def _markers(obj) -> int:
    """How many `cache_control` keys ``obj`` holds, at any depth."""
    if isinstance(obj, dict):
        return ("cache_control" in obj) + sum(_markers(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_markers(v) for v in obj)
    return 0


def test_every_marker_is_removed_and_nothing_else():
    assert without_cache_markers(MARKED) == UNMARKED


def test_the_messages_passed_in_are_not_changed():
    before = copy.deepcopy(MARKED)
    without_cache_markers(MARKED)
    assert MARKED == before


def test_unmarked_messages_come_back_as_the_same_list():
    assert without_cache_markers(UNMARKED) is UNMARKED


def test_malformed_messages_pass_through_untouched():
    odd = [None, "x", {"role": "user", "content": {"cache_control": MARK}},
           {"role": "assistant", "tool_calls": "x"}]
    assert without_cache_markers(odd) == odd


@pytest.mark.parametrize("name", ["anthropic", "bedrock"])
def test_a_provider_that_caches_by_marker_gets_the_markers(name):
    assert outgoing_messages_for(get_adapter_by_name(name), MARKED) is MARKED


@pytest.mark.parametrize("name", ["openai", "azure", "gemini", "deepseek"])
def test_every_other_provider_gets_no_marker(name):
    assert outgoing_messages_for(get_adapter_by_name(name), MARKED) == UNMARKED


def test_no_adapter_gets_no_marker():
    assert outgoing_messages_for(None, MARKED) == UNMARKED


def _params_for(name, model):
    ctx = SimpleNamespace(params={"tools": TOOLS, "cache_control": MARK, "temperature": 0},
                          tenant_id="t1", output_budget_raised=None)
    return outgoing_params_for(ctx, get_adapter_by_name(name), model, {}, "req-1")


def test_a_provider_that_caches_by_marker_gets_marked_tools_and_the_request_marker():
    out = _params_for("anthropic", "claude-sonnet-4-5")
    assert out["tools"] == TOOLS
    assert out["cache_control"] == MARK


@pytest.mark.parametrize("name,model", [("openai", "gpt-4o-mini"),
                                        ("gemini", "gemini-2.5-flash")])
def test_every_other_provider_gets_tools_and_the_request_without_a_marker(name, model):
    out = _params_for(name, model)
    assert out["tools"] == BARE_TOOLS
    assert "cache_control" not in out
    assert out["temperature"] == 0


def test_the_request_tools_are_not_changed():
    before = copy.deepcopy(TOOLS)
    _params_for("openai", "gpt-4o-mini")
    assert TOOLS == before


def _batch_line(adapter):
    items = [{"request_id": "r1", "model": "m", "messages": MARKED,
              "params": {"tools": TOOLS, "cache_control": MARK, "temperature": 0}}]
    return json.loads(build_batch_jsonl(items, adapter))


def test_a_batch_line_for_a_provider_that_caches_by_marker_keeps_them():
    body = _batch_line(get_adapter_by_name("anthropic"))["body"]
    assert body["messages"] == MARKED
    assert body["tools"] == TOOLS and body["cache_control"] == MARK


@pytest.mark.parametrize("name", ["openai", "gemini"])
def test_a_batch_line_for_any_other_provider_carries_no_marker(name):
    body = _batch_line(get_adapter_by_name(name))["body"]
    assert _markers(body) == 0
    assert body["messages"] == UNMARKED and body["tools"] == BARE_TOOLS
    assert body["temperature"] == 0


def test_every_native_batch_submission_names_its_provider():
    """`submit_batch` builds its batch file with ``build_batch_jsonl(items, self)``: a call
    that leaves the adapter out gets no markers at all, which would silently cost an
    Anthropic batch its caching."""
    import inspect
    from providers import ProviderAdapter
    from providers.openai_adapter import OpenAIAdapter
    for cls in (ProviderAdapter, OpenAIAdapter):
        source = inspect.getsource(cls.submit_batch)
        assert "build_batch_jsonl(items, self)" in source, cls.__name__
