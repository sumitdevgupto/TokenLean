"""
G21 ROI ablation — DS6 High-Volume FAQ.

Validates:
  - Baseline (alignment off): system prompt interleaved, no cache markers
  - Isolated (alignment on): system prompt at position 0, cache markers set
  - Gain: cost-discount step recorded for provider prefix
  - Quality gate: output messages identical (deterministic reorder only)
"""
import copy
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g21_cache_alignment import G21CacheAlignment
from middleware import RequestContext
from tests.conftest import _make_savings


def _make_ctx(messages, model="gpt-4o", params=None, config=None):
    if params is None:
        params = {}
    if config is None:
        config = {
            "groups": {
                "G21_cache_alignment": {
                    "enabled": True,
                    "providers": {
                        "openai": {"auto": True},
                        "anthropic": {"marker": True, "cache_type": "ephemeral"},
                    },
                }
            }
        }
    savings = _make_savings(messages, model)
    return RequestContext(
        request_id="req-g21-faq",
        user_id="faq-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model=model,
        routed_model=model,
        params=dict(params),
        config=config,
        savings=savings,
    )


def _faq_messages():
    """DS6-style high-volume FAQ: system prompt after user turn (common in multi-turn)."""
    return [
        {"role": "user", "content": "What are your return policies?"},
        {"role": "assistant", "content": "You can return items within 30 days."},
        {"role": "user", "content": "Do you offer free shipping?"},
        {"role": "system", "content": "You are an FAQ bot for Acme Corp. Be concise."},
        {"role": "user", "content": "How do I track my order?"},
    ]


@pytest.mark.asyncio
async def test_faq_baseline_no_alignment():
    """Baseline: G21 disabled, messages stay as-is."""
    msgs = _faq_messages()
    ctx = _make_ctx(msgs, config={
        "groups": {"G21_cache_alignment": {"enabled": False}}
    })
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    assert result.messages == msgs
    assert len(result.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_faq_openai_reorder_and_cost_step():
    """Isolated: system msg moved to front; cost-discount step recorded."""
    msgs = _faq_messages()
    ctx = _make_ctx(msgs, model="gpt-4o")
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    # System message is now first
    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "You are an FAQ bot for Acme Corp. Be concise."

    # Cost-discount step recorded
    steps = result.savings.step_savings
    assert len(steps) == 1
    assert steps[0].group == "G21"
    assert "cost discount" in steps[0].description.lower()
    assert "50%" in steps[0].description  # OpenAI discount


@pytest.mark.asyncio
async def test_faq_anthropic_cache_markers():
    """Anthropic: system msg gets cache_control + tools marked."""
    msgs = [
        {"role": "user", "content": "FAQ Q1"},
        {"role": "system", "content": "You are an FAQ bot."},
        {"role": "user", "content": "FAQ Q2"},
    ]
    tools = [
        {"type": "function", "function": {"name": "search_faq", "parameters": {}}},
    ]
    from providers.anthropic_adapter import AnthropicAdapter
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", params={"tools": tools})
    ctx.provider_adapter = AnthropicAdapter()
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    assert result.messages[0]["cache_control"] == {"type": "ephemeral"}
    assert result.params["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    steps = result.savings.step_savings
    assert len(steps) == 1
    assert "90%" in steps[0].description  # Anthropic discount


@pytest.mark.asyncio
async def test_faq_quality_gate_output_identical():
    """Quality gate: reordering must not change message content (only order)."""
    msgs = _faq_messages()
    ctx_off = _make_ctx(msgs, config={
        "groups": {"G21_cache_alignment": {"enabled": False}}
    })
    ctx_on = _make_ctx(msgs)

    g21 = G21CacheAlignment()
    result_off = await g21.process_request(ctx_off)
    result_on = await g21.process_request(ctx_on)

    # Content identical; order differs
    off_contents = [(m["role"], m.get("content", "")) for m in result_off.messages]
    on_contents = [(m["role"], m.get("content", "")) for m in result_on.messages]
    assert sorted(off_contents) == sorted(on_contents)
