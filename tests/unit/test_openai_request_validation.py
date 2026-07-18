"""Unit tests for the OpenAI-ingress light request validator (main._validate_openai_request).

A malformed OpenAI body must produce a clean 400 (matching the Anthropic/Gemini
routes) instead of a 500 / provider round-trip. The validator is a pure function, so
these need no live app."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest
from fastapi import HTTPException

from main import _validate_openai_request


def _expect_400(messages):
    with pytest.raises(HTTPException) as ei:
        _validate_openai_request(messages)
    assert ei.value.status_code == 400
    return ei.value


# ── valid shapes pass ────────────────────────────────────────────────────────

def test_valid_single_user_message_passes():
    _validate_openai_request([{"role": "user", "content": "hi"}])


def test_valid_multi_role_conversation_passes():
    _validate_openai_request([
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": "{}", "tool_call_id": "call_1"},
    ])


# ── malformed envelopes → 400 ────────────────────────────────────────────────

def test_missing_messages_is_400():
    # OPENAI.parse_request returns [] when `messages` is absent → empty list here.
    _expect_400([])


def test_messages_not_a_list_is_400():
    _expect_400("not a list")
    _expect_400({"role": "user"})   # a dict, not a list


def test_message_not_an_object_is_400():
    exc = _expect_400([{"role": "user", "content": "ok"}, "oops"])
    assert "messages[1]" in exc.detail


def test_message_missing_role_is_400():
    exc = _expect_400([{"content": "no role here"}])
    assert "role" in exc.detail


def test_message_non_string_role_is_400():
    _expect_400([{"role": 123, "content": "x"}])


def test_message_invalid_role_is_400():
    exc = _expect_400([{"role": "wizard", "content": "x"}])
    assert "wizard" in exc.detail
