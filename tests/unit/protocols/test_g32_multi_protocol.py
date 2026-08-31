"""G32 enforcement is protocol-invariant; only its ANNOTATION is OpenAI-only.

The pipeline is OpenAI-shaped internally and the ingress protocols re-serialise the
already-served OpenAI body on the way out, so G32 always evaluates OpenAI
``function.name`` regardless of whether the caller spoke Anthropic, Gemini or OpenAI —
tool names round-trip verbatim (only Gemini *ids* are synthesised).

The flip side, and the reason this file exists: ``_token_opt`` is dropped for
Anthropic/Gemini egress, so those callers see the tool call *missing* with no
annotation saying why. Enforcement holds; the explanation does not. That asymmetry is
documented as a known limitation and is pinned here.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timezone

import pytest

from middleware import RequestContext
from middleware.g32_tool_eligibility import G32ToolEligibility
from protocols.anthropic_ingress import AnthropicProtocol
from protocols.gemini_ingress import GeminiProtocol
from savings.models import SavingsRecord


def _ctx(mode="block"):
    return RequestContext(
        request_id="req-proto", user_id="u", original_messages=[], messages=[],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params={},
        config={"groups": {"G32_tool_eligibility": {
            "enabled": True, "mode": mode, "policy": {"deny": ["shell_exec"]}}}},
        savings=SavingsRecord(request_id="req-proto", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


def _openai_response():
    """What the pipeline sees, whatever protocol the caller used."""
    return {
        "id": "chatcmpl-1", "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "on it",
                        "tool_calls": [
                            {"id": "call_1", "type": "function",
                             "function": {"name": "shell_exec", "arguments": "{}"}},
                            {"id": "call_2", "type": "function",
                             "function": {"name": "db_read", "arguments": "{}"}},
                        ]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.mark.asyncio
async def test_anthropic_egress_never_carries_the_denied_tool():
    ctx = _ctx()
    gated = await G32ToolEligibility().process_response(ctx, _openai_response())
    body = AnthropicProtocol().serialise_response(gated)

    names = [b.get("name") for b in body["content"] if b.get("type") == "tool_use"]
    assert names == ["db_read"], f"denied tool leaked to an Anthropic client: {names}"
    assert ctx.tool_eligibility_denied == ["shell_exec"]


@pytest.mark.asyncio
async def test_gemini_egress_never_carries_the_denied_tool():
    ctx = _ctx()
    gated = await G32ToolEligibility().process_response(ctx, _openai_response())
    body = GeminiProtocol().serialise_response(gated)

    parts = body["candidates"][0]["content"]["parts"]
    names = [p["functionCall"]["name"] for p in parts if "functionCall" in p]
    assert names == ["db_read"], f"denied tool leaked to a Gemini client: {names}"


@pytest.mark.asyncio
async def test_annotation_is_lost_on_non_openai_egress_but_enforcement_is_not():
    """Documents the asymmetry rather than asserting it is desirable."""
    ctx = _ctx()
    gated = await G32ToolEligibility().process_response(ctx, _openai_response())
    assert gated["_token_opt"]["tool_eligibility"]["denied"] == ["shell_exec"]

    body = AnthropicProtocol().serialise_response(gated)
    assert "_token_opt" not in body            # the caller is told nothing…
    names = [b.get("name") for b in body["content"] if b.get("type") == "tool_use"]
    assert "shell_exec" not in names           # …but the call is still gone


@pytest.mark.asyncio
async def test_tool_names_are_matched_verbatim_across_protocols():
    """Anthropic preserves the client's own tool_use id and Gemini synthesises one, but
    NEITHER rewrites the tool NAME — which is what the policy matches on."""
    ctx = _ctx()
    resp = _openai_response()
    resp["choices"][0]["message"]["tool_calls"][0]["id"] = "toolu_client_supplied"
    gated = await G32ToolEligibility().process_response(ctx, resp)
    assert ctx.tool_eligibility_denied == ["shell_exec"]

    for protocol in (AnthropicProtocol(), GeminiProtocol()):
        body = protocol.serialise_response(gated)
        assert "shell_exec" not in str(body)
