"""Work a request leaves to run after its response — the billing row, the security audit
and the quota, trial and spend counters — is held until it finishes, its failures are
logged, and shutdown waits for it (bounded) before the Redis and database pools close.

These tasks were created and the handle discarded: the event loop keeps only a weak
reference to a task, a failure was never observed, and on scale-in or a deploy the
shutdown closed the pools under them and the runner cancelled the rest, so the last
moments' invoice rows and counter bumps were lost.
"""
import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import main


@pytest.fixture(autouse=True)
def _empty_registry():
    main._AFTER_RESPONSE.clear()
    yield
    main._AFTER_RESPONSE.clear()


async def test_a_task_is_held_until_it_finishes():
    release = asyncio.Event()

    async def work():
        await release.wait()

    main._after_response(work(), "unit-work")
    assert len(main._AFTER_RESPONSE) == 1
    (task,) = main._AFTER_RESPONSE
    assert not task.done()
    release.set()
    await task
    await asyncio.sleep(0)
    assert main._AFTER_RESPONSE == set()


async def test_a_failure_is_logged_by_name(caplog):
    async def work():
        raise RuntimeError("redis went away")

    with caplog.at_level(logging.WARNING, logger="main"):
        main._after_response(work(), "spend-counter")
        await asyncio.sleep(0.01)
    assert any(r.name == "main" and "spend-counter" in r.getMessage()
               and "redis went away" in r.getMessage() for r in caplog.records)
    assert main._AFTER_RESPONSE == set()


async def test_shutdown_waits_for_pending_work():
    finished = []

    async def work():
        await asyncio.sleep(0.05)
        finished.append(True)

    main._after_response(work(), "billing-row")
    await main._drain_after_response(limit_s=2.0)
    assert finished == [True]


async def test_the_wait_is_bounded(caplog):
    async def stuck():
        await asyncio.Event().wait()

    main._after_response(stuck(), "stuck-row")
    with caplog.at_level(logging.WARNING, logger="main"):
        await main._drain_after_response(limit_s=0.05)
    assert any("1 " in r.getMessage() and "still running" in r.getMessage()
               for r in caplog.records)
    for task in list(main._AFTER_RESPONSE):
        task.cancel()


def test_no_running_loop_still_raises_and_leaves_nothing_unawaited():
    async def work():
        return None

    coro = work()
    with pytest.raises(RuntimeError):
        main._after_response(coro, "no-loop")
    assert coro.cr_frame is None          # closed, so no "never awaited" warning


def test_shutdown_drains_before_the_pools_close():
    src = inspect.getsource(main.lifespan)
    assert "await _drain_after_response(" in src and "await close_pool()" in src
    assert src.index("await _drain_after_response(") < src.index("await close_pool()")


def _ctx(**over):
    ctx = SimpleNamespace(
        request_id="r1", tenant_id="acme", redis_prefix="t:acme:", user_id="u",
        config={"trial": {"status": "active"}},
        savings=SimpleNamespace(cost_actual_usd=0.25),
        pii_action="flag", guardrail_action=None, context_trust_action=None,
        context_trust_pii_action=None, tool_eligibility_action=None,
        tool_dispatch_blocked=None, empty_completion=None)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


@pytest.mark.parametrize("schedule", [
    lambda ctx: main._bump_spend_counter(ctx),
    lambda ctx: main._bump_quota_counter(ctx),
    lambda ctx: main._bump_trial_counter(ctx),
], ids=["spend", "quota", "trial"])
async def test_the_counters_go_through_the_registry(schedule):
    with patch("main.add_to_spend", new=AsyncMock()), \
            patch("cache.redis_pool.get_redis", return_value=AsyncMock()):
        schedule(_ctx())
        assert len(main._AFTER_RESPONSE) == 1
        await main._drain_after_response(limit_s=2.0)


async def test_a_webhook_event_is_held_and_drained():
    import events
    delivered, release = [], asyncio.Event()

    async def dispatch(tenant_id, event, payload):
        await release.wait()
        delivered.append(event)

    events.set_webhook_dispatcher(dispatch)
    try:
        events.schedule_event("acme", "pii.detected", {})
        assert len(events.pending_tasks()) == 1
        release.set()
        await main._drain_after_response(limit_s=2.0)
        assert delivered == ["pii.detected"]
        await asyncio.sleep(0)
        assert events.pending_tasks() == set()
    finally:
        events.set_webhook_dispatcher(None)


async def test_the_billing_row_and_the_audit_go_through_the_registry():
    meter, auditor = AsyncMock(), AsyncMock()
    with patch.object(main, "_usage_meter", meter), patch.object(main, "_audit_logger", auditor):
        main._schedule_billing(_ctx(), {"choices": []})
        main._schedule_security_audit(_ctx())
        assert len(main._AFTER_RESPONSE) == 2
        await main._drain_after_response(limit_s=2.0)
    meter.record.assert_awaited_once()
    auditor.log_security_events.assert_awaited_once()
