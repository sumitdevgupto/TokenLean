"""C1/C2 — _record_outcome persists one usage_events row per outcome.

C1: a served 2xx response (normal LLM, cache hit, or bypass) schedules exactly one
BILLABLE UsageMeter.record. C2: non-2xx exits now also schedule a record — but
observability-only (billable=False), so the reliability/latency panels have error
data while the request-count invoice stays 2xx-only. Batch-defer (202) is billed on
its own path, so _record_outcome must NOT double-write it.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


def _ctx():
    # redis_prefix lets the 2xx quota/spend bumps resolve without a real Redis; they are
    # fire-and-forget and swallow their own errors, so they never affect these assertions.
    return SimpleNamespace(tenant_id="t1", request_id="r1", redis_prefix="t:t1:")


@pytest.mark.asyncio
async def test_bills_on_2xx_with_response(monkeypatch):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    ctx = _ctx()

    main._record_outcome(ctx, time.time(), "200", {"id": "resp"})
    await asyncio.sleep(0)  # let the fire-and-forget task run

    meter.record.assert_awaited_once()
    assert meter.record.await_args.args[0] is ctx
    assert meter.record.await_args.kwargs["billable"] is True
    assert meter.record.await_args.kwargs["status_code"] == 200


@pytest.mark.asyncio
async def test_non_2xx_persists_observability_only_row(monkeypatch):
    """C2: a non-2xx outcome writes a NON-billable row (error/latency analytics)."""
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    monkeypatch.setattr(main, "_persist_all_outcomes", lambda: True)
    ctx = _ctx()

    main._record_outcome(ctx, time.time(), "502", {"id": "resp"})   # provider error
    await asyncio.sleep(0)

    meter.record.assert_awaited_once()
    assert meter.record.await_args.kwargs["billable"] is False
    assert meter.record.await_args.kwargs["status_code"] == 502


@pytest.mark.asyncio
async def test_202_defer_not_double_written_by_record_outcome(monkeypatch):
    """The 202 batch-defer row is billed on its own path; _record_outcome skips it."""
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    monkeypatch.setattr(main, "_persist_all_outcomes", lambda: True)
    ctx = _ctx()

    main._record_outcome(ctx, time.time(), "202", {"id": "resp"})
    main._record_outcome(None, time.time(), "502", {"id": "resp"})  # no ctx → skip
    await asyncio.sleep(0)

    meter.record.assert_not_called()


@pytest.mark.asyncio
async def test_persist_all_outcomes_off_restores_2xx_only(monkeypatch):
    """With the C2 gate off, non-2xx exits write nothing (pre-C2 behaviour)."""
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    monkeypatch.setattr(main, "_persist_all_outcomes", lambda: False)
    ctx = _ctx()

    main._record_outcome(ctx, time.time(), "502", {"id": "resp"})
    main._record_outcome(ctx, time.time(), "429", {"id": "resp"})
    await asyncio.sleep(0)

    meter.record.assert_not_called()


@pytest.mark.asyncio
async def test_meter_none_is_safe(monkeypatch):
    monkeypatch.setattr(main, "_usage_meter", None)
    # Must not raise when billing is not wired (e.g. no DB pool).
    main._record_outcome(_ctx(), time.time(), "200", {"id": "resp"})
    await asyncio.sleep(0)


# ─── least_latency EWMA success-gating (2026-07-20 code review) ─────────────────
# Regression: record_model_latency must only fire on a genuine SUCCESS (status=="200").
# Before the fix it fired whenever llm_ms > 0 regardless of outcome, so a model that
# fails FAST looked "fast" to the EWMA and got preferentially routed to by
# strategy=least_latency — directly undermining the sibling per-model-lockout feature.

def _latency_ctx(routed_model="gpt-4o-mini", llm_ms=250.0, config=None):
    return SimpleNamespace(
        tenant_id="t1", request_id="r1", redis_prefix="t:t1:",
        routed_model=routed_model, model=routed_model,
        llm_elapsed_ms=llm_ms, config=config or {},
    )


@pytest.fixture(autouse=True)
def _reset_ewma():
    from middleware.g06_routing import _MODEL_LATENCY_EWMA
    _MODEL_LATENCY_EWMA.clear()
    yield
    _MODEL_LATENCY_EWMA.clear()


def test_success_feeds_the_ewma(monkeypatch):
    from middleware.g06_routing import _MODEL_LATENCY_EWMA
    monkeypatch.setattr(main, "_usage_meter", None)
    main._record_outcome(_latency_ctx(llm_ms=250.0), time.time(), "200", {"id": "resp"})
    assert _MODEL_LATENCY_EWMA.get("gpt-4o-mini") == 250.0


@pytest.mark.parametrize("status", ["401", "402", "429", "502", "503"])
def test_failure_never_feeds_the_ewma(monkeypatch, status):
    """The exact bug: a model that fails fast (immediate 4xx/5xx) must NOT look 'fast'
    to least_latency — only a real, successful call may inform the estimate."""
    from middleware.g06_routing import _MODEL_LATENCY_EWMA
    monkeypatch.setattr(main, "_usage_meter", None)
    main._record_outcome(_latency_ctx(llm_ms=5.0), time.time(), status, None)
    assert "gpt-4o-mini" not in _MODEL_LATENCY_EWMA


def test_zero_llm_ms_never_feeds_the_ewma_even_on_success(monkeypatch):
    # Cache hit / bypass: llm_ms==0, no LLM call happened — nothing to attribute.
    from middleware.g06_routing import _MODEL_LATENCY_EWMA
    monkeypatch.setattr(main, "_usage_meter", None)
    main._record_outcome(_latency_ctx(llm_ms=0.0), time.time(), "200", {"id": "resp"})
    assert _MODEL_LATENCY_EWMA == {}


def test_alpha_read_from_g6_routing_config(monkeypatch):
    """The EWMA smoothing factor is config-driven (groups.G6_routing.least_latency_alpha),
    not a hardcoded constant — a tenant can hot-reload-tune it without a redeploy."""
    from middleware.g06_routing import _MODEL_LATENCY_EWMA
    monkeypatch.setattr(main, "_usage_meter", None)
    cfg = {"groups": {"G6_routing": {"least_latency_alpha": 1.0}}}  # alpha=1 → no smoothing
    main._record_outcome(_latency_ctx(llm_ms=100.0, config=cfg), time.time(), "200", None)
    main._record_outcome(_latency_ctx(llm_ms=300.0, config=cfg), time.time(), "200", None)
    # alpha=1.0 means each new observation FULLY replaces the running EWMA.
    assert _MODEL_LATENCY_EWMA["gpt-4o-mini"] == 300.0


def test_alpha_defaults_to_point_three_when_unset(monkeypatch):
    from middleware.g06_routing import _MODEL_LATENCY_EWMA
    monkeypatch.setattr(main, "_usage_meter", None)
    main._record_outcome(_latency_ctx(llm_ms=100.0), time.time(), "200", None)
    main._record_outcome(_latency_ctx(llm_ms=200.0), time.time(), "200", None)
    # default alpha=0.3: 0.3*200 + 0.7*100 = 130
    assert _MODEL_LATENCY_EWMA["gpt-4o-mini"] == pytest.approx(130.0)


# ─── C1b — _schedule_billing primitive (shared by 2xx hook + batch defer) ────────

@pytest.mark.asyncio
async def test_schedule_billing_bills_with_response(monkeypatch):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    ctx = _ctx()
    main._schedule_billing(ctx, {"id": "resp"})
    await asyncio.sleep(0)
    meter.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_billing_bills_batch_empty_response(monkeypatch):
    # Batch-defer path bills with an empty dict (no LLM body yet) — still one row.
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    ctx = _ctx()
    main._schedule_billing(ctx, {})
    await asyncio.sleep(0)
    meter.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_billing_noop_on_none(monkeypatch):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    main._schedule_billing(None, {"id": "resp"})   # no ctx
    main._schedule_billing(_ctx(), None)           # no response
    await asyncio.sleep(0)
    meter.record.assert_not_called()


# ─── An admin key acting as another tenant is not that tenant's traffic ─────────
# An admin key with X-Tenant-ID (operator support, benchmarks) was billed as the tenant
# it named: a billable usage row, and a bump of its quota, spend and trial counters. Its
# row now names the impersonator and is never billable, and it counts against none of them.
def _impersonated_ctx():
    ctx = _ctx()
    ctx.impersonator_tenant_id = "ops"
    return ctx


@pytest.fixture
def counters(monkeypatch):
    bumped = []
    for name in ("_bump_quota_counter", "_bump_spend_counter", "_bump_trial_counter"):
        monkeypatch.setattr(main, name, lambda ctx, name=name: bumped.append(name))
    return bumped


@pytest.mark.asyncio
async def test_an_impersonated_answer_is_recorded_but_never_billed(monkeypatch, counters):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    monkeypatch.setattr(main, "_persist_all_outcomes", lambda: False)   # not an error row
    main._record_outcome(_impersonated_ctx(), time.time(), "200", {"id": "resp"})
    await asyncio.sleep(0)
    meter.record.assert_awaited_once()
    assert meter.record.await_args.kwargs["billable"] is False
    assert meter.record.await_args.kwargs["status_code"] == 200
    assert counters == []


@pytest.mark.asyncio
async def test_the_tenant_s_own_answer_still_counts(monkeypatch, counters):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    main._record_outcome(_ctx(), time.time(), "200", {"id": "resp"})
    await asyncio.sleep(0)
    assert meter.record.await_args.kwargs["billable"] is True
    assert counters == ["_bump_quota_counter", "_bump_spend_counter", "_bump_trial_counter"]


@pytest.mark.asyncio
@pytest.mark.parametrize("impersonated, billable", [(True, False), (False, True)],
                         ids=["impersonated", "the tenant's own"])
async def test_a_deferred_batch_request_is_billed_only_when_it_is_the_tenant_s(
        monkeypatch, impersonated, billable):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    ctx = _impersonated_ctx() if impersonated else _ctx()
    ctx.cache_response = None
    main._bill_batch_deferred(ctx)
    await asyncio.sleep(0)
    meter.record.assert_awaited_once()
    assert meter.record.await_args.kwargs["billable"] is billable
    assert meter.record.await_args.kwargs["status_code"] == 202


# ─── A batched request counts against the tenant's limits ───────────────────────
# It was billed when queued but bumped no counter, so a trial or capped tenant could send
# everything as a batch and never reach a limit. Its cost is known only when its answer
# arrives; G13 adds that to the spend counter then.
@pytest.mark.asyncio
@pytest.mark.parametrize("impersonated, counted", [
    (True, []), (False, ["_bump_quota_counter", "_bump_trial_counter"])],
    ids=["impersonated", "the tenant's own"])
async def test_a_deferred_batch_request_counts_against_the_quota_and_trial(
        monkeypatch, counters, impersonated, counted):
    monkeypatch.setattr(main, "_usage_meter", AsyncMock())
    ctx = _impersonated_ctx() if impersonated else _ctx()
    ctx.cache_response = None
    main._bill_batch_deferred(ctx)
    await asyncio.sleep(0)
    assert counters == counted


@pytest.mark.asyncio
async def test_a_served_request_s_cost_goes_to_the_spend_counter_g13_adds_to(monkeypatch):
    from middleware import g00_rate_limit
    added = []

    class _Redis:
        async def incrbyfloat(self, key, amount):
            added.append((key, amount))
            return amount

        async def expire(self, key, ttl):
            added.append((key, ttl))

    monkeypatch.setattr(g00_rate_limit, "_get_redis", lambda: _Redis())
    ctx = _ctx()
    ctx.savings = SimpleNamespace(cost_actual_usd=0.25)
    main._bump_spend_counter(ctx)
    await asyncio.sleep(0)
    key = g00_rate_limit.G00RateLimit.spend_key("t:t1:")
    assert added == [(key, 0.25), (key, 40 * 86400)]
