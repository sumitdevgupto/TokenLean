"""The OpenAI ingress admits only documented request fields.

The OpenAI adapter used to copy every body field into params, so a client could set the
proxy's `_`-prefixed internal flags and pass fields litellm reads as arguments of the
upstream call. These pin the allowlist contract in protocols/base.py."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import logging

import pytest

from protocols import AnthropicProtocol, OpenAIProtocol
from protocols.base import OPENAI_CHAT_PARAMS, TOKENLEAN_CLIENT_PARAMS, filter_client_params
from providers import is_litellm_control_key


def _body(**extra):
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], **extra}


# Fields litellm reads as arguments of the call (plus `metadata`, which it reads for its
# own logging callbacks) — none is a documented client parameter.
CALL_ARGUMENT_FIELDS = {
    "api_base": "https://attacker.example/v1", "base_url": "https://attacker.example/v1",
    "api_key": "sk-attacker", "custom_llm_provider": "openai", "extra_headers": {"x": "1"},
    "extra_body": {"model": "gpt-4o"}, "fallbacks": ["gpt-4o"], "num_retries": 50,
    "mock_response": "canned", "timeout": 6000, "metadata": {"trace_name": "x"},
}


def test_call_argument_fields_are_dropped():
    _, _, params = OpenAIProtocol().parse_request(_body(**CALL_ARGUMENT_FIELDS))
    assert params == {}


@pytest.mark.parametrize("flag", ["_native_batch", "_auth_admin", "_auth_tenant_id", "_g05_system_tag"])
def test_internal_underscore_flags_are_dropped(flag):
    _, _, params = OpenAIProtocol().parse_request(_body(**{flag: True}))
    assert flag not in params


def test_documented_openai_params_pass_unchanged():
    fields = {
        "temperature": 0.2, "top_p": 0.9, "max_tokens": 64, "max_completion_tokens": 64,
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        "tool_choice": "auto", "response_format": {"type": "json_object"}, "stream": True,
        "stream_options": {"include_usage": True}, "n": 1, "stop": ["\n"], "seed": 7,
        "user": "u-1", "reasoning_effort": "low", "parallel_tool_calls": False,
        "logprobs": True, "top_logprobs": 2, "service_tier": "auto", "prompt_cache_key": "k",
    }
    _, _, params = OpenAIProtocol().parse_request(_body(**fields))
    assert params == fields


def test_tokenlean_client_params_pass_including_any_x_prefixed_field():
    fields = {name: f"v-{name}" for name in TOKENLEAN_CLIENT_PARAMS}
    fields.update({"x_session_id": "s-1", "x_team": "blue", "x_some_future_hint": 1})
    _, _, params = OpenAIProtocol().parse_request(_body(**fields))
    assert params == fields


def test_messages_and_model_are_not_params():
    messages, model, params = OpenAIProtocol().parse_request(_body())
    assert model == "gpt-4o-mini"
    assert messages == [{"role": "user", "content": "hi"}]
    assert params == {}


def test_extra_allowed_readmits_a_named_field():
    _, _, params = OpenAIProtocol().parse_request(
        _body(metadata={"trace_name": "x"}, custom_field=1), extra_allowed=["metadata"])
    assert params == {"metadata": {"trace_name": "x"}}


def test_extra_allowed_cannot_readmit_internal_flags():
    _, _, params = OpenAIProtocol().parse_request(
        _body(_native_batch=True), extra_allowed=["_native_batch"])
    assert params == {}


def test_filter_reports_the_names_it_dropped():
    kept, dropped = filter_client_params({"temperature": 1, "api_base": "x", "_flag": 1})
    assert kept == {"temperature": 1}
    assert sorted(dropped) == ["_flag", "api_base"]


def test_dropped_field_names_are_logged_but_never_their_values(caplog):
    value = "https://attacker.example/collect"
    with caplog.at_level(logging.DEBUG, logger="protocols.base"):
        OpenAIProtocol().parse_request(_body(api_base=value, unlisted_field_zz=1))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "api_base" in text and "unlisted_field_zz" in text
    assert value not in text


def test_allowlist_contains_no_upstream_call_argument():
    assert [n for n in OPENAI_CHAT_PARAMS | TOKENLEAN_CLIENT_PARAMS if is_litellm_control_key(n)] == []


def test_anthropic_ingress_is_unchanged():
    # Builds params from fixed fields, so an unknown field never appeared there — and the
    # fields it does copy (metadata, top_k) keep flowing.
    body = {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"user_id": "u"}, "top_k": 5, "api_base": "https://attacker.example"}
    _, _, params = AnthropicProtocol().parse_request(body)
    assert params["metadata"] == {"user_id": "u"} and params["top_k"] == 5
    assert "api_base" not in params
