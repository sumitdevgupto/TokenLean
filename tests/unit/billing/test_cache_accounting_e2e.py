"""#34 — end-to-end cache accounting: response usage → G18 → savings → UsageEvent.

Each link in this chain has its own unit test, but they can all pass while the chain is
broken: G18 could record cache tokens that `_build_event` never reads, or the metering
INSERT could carry columns nothing populates. This test walks one response through the
whole seam and asserts the numbers arrive intact — including the None-vs-zero
distinction, which is the entire point of the feature and the easiest thing to lose in a
`or 0` somewhere along the way.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime
from unittest.mock import patch

import pytest

from billing.metering import UsageMeter
from middleware.g18_observability import G18Observability
from providers.openai_adapter import OpenAIAdapter
from savings.models import SavingsRecord

_PRICING = {"gpt-4o": {"input": 0.005, "output": 0.015},
            "default": {"input": 0.005, "output": 0.015}}


class _Ctx:
    """Minimal RequestContext stand-in carrying only what this seam touches."""

    def __init__(self):
        self.request_id = "req-e2e-cache"
        self.user_id = "u1"
        self.tenant_id = "acme"
        self.model = "gpt-4o"
        self.routed_model = "gpt-4o"
        self.params = {}
        self.provider_adapter = OpenAIAdapter()
        self.cache_hit = False
        self.cache_level = ""
        self.bypassed = False
        self.ingress_protocol = "openai"
        self.agent_id = ""
        self.redis_prefix = "t:acme:"
        self.config = {
            "groups": {
                "G18_observability": {"enabled": True, "prometheus_enabled": False},
                "G21_cache_alignment": {"providers": {"openai": {
                    "cache_read_multiplier": 0.5, "cache_write_multiplier": 1.0}}},
            },
            "pricing_tier": "enterprise",
        }
        self.savings = SavingsRecord(
            request_id=self.request_id, user_id=self.user_id, timestamp=datetime.now(),
            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=1200,
        )
        self.pricing_tier = "enterprise"
        self.otel_span = None
        self.langfuse_trace = None
        self.skip_groups = []


def _response(details):
    return {
        "id": "chatcmpl-e2e",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": details},
    }


async def _run(details):
    ctx = _Ctx()
    with patch("middleware.langfuse_tracing.finish_trace"), \
         patch("config_loader.get_pricing_table", return_value=_PRICING):
        await G18Observability().record(ctx, _response(details))
    event = UsageMeter(db_pool=None)._build_event(
        ctx, _response(details), status_code=200, billable=True,
        total_duration_ms=10, llm_duration_ms=5,
    )
    return ctx, event


@pytest.mark.asyncio
async def test_cache_counts_survive_the_whole_chain():
    ctx, event = await _run({"cached_tokens": 600, "cache_write_tokens": 250})
    assert ctx.savings.cache_read_tokens == 600
    assert ctx.savings.cache_write_tokens == 250
    # The billable row is what the dashboards and the invoice reconciliation read.
    assert event.cache_read_tokens == 600
    assert event.cache_write_tokens == 250
    assert event.cost_cache_write_usd is not None and event.cost_cache_write_usd > 0
    assert event.cost_cache_read_usd is not None


@pytest.mark.asyncio
async def test_unknown_stays_null_all_the_way_to_the_row():
    """A provider that reports nothing must persist as SQL NULL, never as 0.

    A defaulted zero would let the billing dashboard state 'this tenant did no cache
    writes' when the truth is 'we never found out' — the same class of half-truth the
    read-only reporting already was.
    """
    ctx, event = await _run({})
    assert ctx.savings.cache_write_tokens is None
    assert event.cache_write_tokens is None
    assert event.cost_cache_write_usd is None


@pytest.mark.asyncio
async def test_explicit_zero_is_not_confused_with_unknown():
    ctx, event = await _run({"cached_tokens": 0, "cache_write_tokens": 0})
    assert event.cache_write_tokens == 0
    assert event.cache_write_tokens is not None


@pytest.mark.asyncio
async def test_cost_split_never_exceeds_the_reported_total():
    _, event = await _run({"cached_tokens": 600, "cache_write_tokens": 250})
    assert (event.cost_cache_read_usd + event.cost_cache_write_usd) <= event.cost_actual_usd + 1e-9


@pytest.mark.asyncio
async def test_disclosure_and_persistence_agree():
    """What the caller is told in _token_opt must match what is billed/reported."""
    ctx, event = await _run({"cached_tokens": 600, "cache_write_tokens": 250})
    meta = ctx.savings.to_langfuse_metadata()
    assert meta["cache_read_tokens"] == event.cache_read_tokens
    assert meta["cache_write_tokens"] == event.cache_write_tokens
    assert meta["cost_cache_write_usd"] == pytest.approx(event.cost_cache_write_usd, rel=1e-6)
