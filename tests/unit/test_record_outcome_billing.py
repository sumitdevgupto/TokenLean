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
