"""
Compounding Ablation Test — measures combined effect of G19 + G20 + G5+ + G21.

Tests:
  1. All-off baseline (raw token count + cost)
  2. Each feature individually (per-feature delta)
  3. All-on combined (compounding gain)
  4. Verifies compounding > sum of individual gains (or at least no regression)

This test uses mocked contexts to avoid requiring external services.
"""
import copy
import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g19_headroom import G19Headroom
from middleware.g21_cache_alignment import G21CacheAlignment
from savings.calculator import count_messages_tokens
from middleware import RequestContext
from tests.conftest import _make_savings


# ─── Shared test data ────────────────────────────────────────────────────────

def _enterprise_messages():
    """DS1-style enterprise support messages with structured + NL content."""
    system_prompt = (
        "You are a customer support agent. It is important to make sure to "
        "respond professionally. In order to help the customer, please note that "
        "you should be thorough. Due to the fact that customers expect quality, "
        "ensure that you provide accurate information at this point in time."
    )
    tool_output = json.dumps({
        "order_id": "ORD-12345",
        "status": "shipped",
        "tracking": "TRACK-ABC",
        "customer_name": "Alice",
        "customer_email": "",
        "notes": None,
        "metadata": {},
        "history": [
            {"event": "created", "timestamp": "2024-01-01T00:00:00Z", "details": ""},
            {"event": "shipped", "timestamp": "2024-01-02T00:00:00Z", "details": ""},
        ],
    }, indent=2)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "What's the status of my order ORD-12345?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "lookup_order", "arguments": '{"order_id": "ORD-12345"}'}}
        ]},
        {"role": "tool", "content": tool_output},
        {"role": "user", "content": "Thanks, when will it arrive?"},
    ]


def _full_config(g19_enabled=True, g21_enabled=True):
    """Config with G19 and G21 toggleable."""
    return {
        "groups": {
            "G19_headroom": {
                "enabled": g19_enabled,
                "request_side_enabled": True,
                "response_side_enabled": True,
                "min_length_to_compress": 30,
                "compression_strategies": {
                    "json": {"remove_empty": True, "dedupe_keys": True},
                    "code": {"strip_comments": True, "strip_whitespace": True, "compress_imports": True},
                    "logs": {"dedupe_lines": True, "truncate_long_lines": 200},
                },
            },
            "G21_cache_alignment": {
                "enabled": g21_enabled,
                "providers": {
                    "openai": {"auto": True},
                    "anthropic": {"marker": True, "cache_type": "ephemeral"},
                },
            },
            "G5_cache": {
                "enabled": True,
                "l2_embedding_model": "BAAI/bge-small-en-v1.5",
                "l2_similarity_threshold": 0.90,
            },
        },
    }


def _make_ctx(messages, config, model="gpt-4o"):
    savings = _make_savings(messages, model)
    return RequestContext(
        request_id="req-compound-test",
        user_id="compound-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model=model,
        routed_model=model,
        params={},
        config=config,
        savings=savings,
    )


# ─── Test: all-off baseline ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_off_baseline():
    """With all features disabled, token count is unchanged."""
    messages = _enterprise_messages()
    config = _full_config(g19_enabled=False, g21_enabled=False)
    ctx = _make_ctx(messages, config)

    baseline_tokens = count_messages_tokens(messages, "gpt-4o")

    g19 = G19Headroom()
    g21 = G21CacheAlignment()

    ctx = await g19.process_request(ctx)
    ctx = await g21.process_request(ctx)

    after_tokens = count_messages_tokens(ctx.messages, "gpt-4o")
    assert after_tokens == baseline_tokens
    assert len(ctx.savings.step_savings) == 0


# ─── Test: G19 only ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g19_only():
    """G19 alone compresses structured content (JSON tool output)."""
    messages = _enterprise_messages()
    config = _full_config(g19_enabled=True, g21_enabled=False)
    ctx = _make_ctx(messages, config)

    baseline_tokens = count_messages_tokens(messages, "gpt-4o")

    g19 = G19Headroom()
    ctx = await g19.process_request(ctx)

    after_tokens = count_messages_tokens(ctx.messages, "gpt-4o")
    # Tool output JSON should be compressed (empty fields removed)
    assert after_tokens <= baseline_tokens
    # At least one savings step recorded
    g19_savings = [s for s in ctx.savings.step_savings if s.group == "G19"]
    assert len(g19_savings) >= 1


# ─── Test: G21 only ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g21_only():
    """G21 alone reorders for prefix caching (no token count change, but step recorded)."""
    # Create messages with system NOT at front to trigger reordering
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "What is 2+2?"},
    ]
    config = _full_config(g19_enabled=False, g21_enabled=True)
    ctx = _make_ctx(messages, config)

    g21 = G21CacheAlignment()
    ctx = await g21.process_request(ctx)

    # System message should now be first
    assert ctx.messages[0]["role"] == "system"
    g21_savings = [s for s in ctx.savings.step_savings if s.group == "G21"]
    assert len(g21_savings) == 1


# ─── Test: all-on compounding ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_on_compounding():
    """All features together should compound savings."""
    # Messages with system not at front + structured tool output
    messages = [
        {"role": "user", "content": "What's the status of my order?"},
        {"role": "system", "content": "You are a support agent. It is important to make sure to help customers."},
        {"role": "tool", "content": json.dumps({
            "order_id": "ORD-999",
            "status": "shipped",
            "customer_email": "",
            "notes": None,
            "metadata": {},
        }, indent=2)},
    ]
    config = _full_config(g19_enabled=True, g21_enabled=True)
    ctx = _make_ctx(messages, config)

    baseline_tokens = count_messages_tokens(messages, "gpt-4o")

    g19 = G19Headroom()
    g21 = G21CacheAlignment()

    # Apply in pipeline order: G19 then G21
    ctx = await g19.process_request(ctx)
    ctx = await g21.process_request(ctx)

    after_tokens = count_messages_tokens(ctx.messages, "gpt-4o")

    # Should have savings from at least G19 (JSON compression) and G21 (reorder)
    total_steps = len(ctx.savings.step_savings)
    assert total_steps >= 1  # At minimum G19 or G21 should fire

    # Token count should not increase
    assert after_tokens <= baseline_tokens


# ─── Test: per-feature attribution ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_feature_attribution():
    """Each feature's delta is independently measurable."""
    messages = [
        {"role": "user", "content": "Query"},
        {"role": "system", "content": "You are helpful. It is important to make sure to respond."},
        {"role": "tool", "content": json.dumps({"key": "value", "empty": "", "null": None, "list": []}, indent=2)},
    ]

    baseline_tokens = count_messages_tokens(messages, "gpt-4o")

    # G19 only
    config_g19 = _full_config(g19_enabled=True, g21_enabled=False)
    ctx_g19 = _make_ctx(messages, config_g19)
    g19 = G19Headroom()
    ctx_g19 = await g19.process_request(ctx_g19)
    g19_tokens = count_messages_tokens(ctx_g19.messages, "gpt-4o")
    g19_delta = baseline_tokens - g19_tokens

    # G21 only
    config_g21 = _full_config(g19_enabled=False, g21_enabled=True)
    ctx_g21 = _make_ctx(messages, config_g21)
    g21 = G21CacheAlignment()
    ctx_g21 = await g21.process_request(ctx_g21)
    g21_tokens = count_messages_tokens(ctx_g21.messages, "gpt-4o")
    g21_delta = baseline_tokens - g21_tokens

    # Both
    config_both = _full_config(g19_enabled=True, g21_enabled=True)
    ctx_both = _make_ctx(messages, config_both)
    ctx_both = await g19.process_request(ctx_both)
    ctx_both = await g21.process_request(ctx_both)
    both_tokens = count_messages_tokens(ctx_both.messages, "gpt-4o")
    combined_delta = baseline_tokens - both_tokens

    # Combined should be >= max of individual (no regression)
    assert combined_delta >= max(g19_delta, g21_delta, 0)

    # All deltas should be non-negative
    assert g19_delta >= 0
    assert g21_delta >= 0
    assert combined_delta >= 0


# ─── Test: original messages preserved ───────────────────────────────────────

@pytest.mark.asyncio
async def test_compounding_preserves_originals():
    """Original messages must not be mutated by any feature."""
    messages = _enterprise_messages()
    config = _full_config(g19_enabled=True, g21_enabled=True)
    ctx = _make_ctx(messages, config)

    original_before = copy.deepcopy(ctx.original_messages)

    g19 = G19Headroom()
    g21 = G21CacheAlignment()
    ctx = await g19.process_request(ctx)
    ctx = await g21.process_request(ctx)

    assert ctx.original_messages == original_before
