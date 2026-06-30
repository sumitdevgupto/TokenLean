"""C1 — _record_outcome bills every served 2xx request (one usage_events row).

Verifies the centralised billing hook: a served 2xx response (normal LLM, cache
hit, or bypass — all routed through _record_outcome with status "200" + the
response) schedules exactly one UsageMeter.record; non-2xx exits, batch-deferred
(202), and missing-response calls bill nothing.
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
    return SimpleNamespace(tenant_id="t1", request_id="r1")


@pytest.mark.asyncio
async def test_bills_on_2xx_with_response(monkeypatch):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    ctx = _ctx()

    main._record_outcome(ctx, time.time(), "200", {"id": "resp"})
    await asyncio.sleep(0)  # let the fire-and-forget task run

    meter.record.assert_awaited_once()
    assert meter.record.await_args.args[0] is ctx


@pytest.mark.asyncio
async def test_no_bill_on_non_2xx_or_missing_response(monkeypatch):
    meter = AsyncMock()
    monkeypatch.setattr(main, "_usage_meter", meter)
    ctx = _ctx()

    main._record_outcome(ctx, time.time(), "502", {"id": "resp"})   # provider error
    main._record_outcome(ctx, time.time(), "429", {"id": "resp"})   # rate limited
    main._record_outcome(ctx, time.time(), "202", {"id": "resp"})   # batch defer → C1b, not C1
    main._record_outcome(ctx, time.time(), "200", None)             # served but no response body
    main._record_outcome(None, time.time(), "200", {"id": "resp"})  # no ctx
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
