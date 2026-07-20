"""Unit tests for the OSS outbound-event seam (events.py).

The seam is a no-op by default (OSS) and dispatches to a commercial-installed
function pointer when set. Barricade-safe: core imports only `events`, never a
commercial dispatcher.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio

import pytest

import events


@pytest.fixture(autouse=True)
def _restore_dispatcher():
    """Every test starts and ends with the no-op dispatcher (module global)."""
    events.set_webhook_dispatcher(None)
    yield
    events.set_webhook_dispatcher(None)


def test_default_is_noop():
    assert events.dispatcher_installed() is False
    # emit_event never raises with no dispatcher.
    asyncio.run(events.emit_event("t1", events.GUARDRAIL_BLOCK, {"x": 1}))


def test_set_dispatcher_marks_installed_and_dispatches():
    seen = []

    async def _disp(tenant_id, event, payload):
        seen.append((tenant_id, event, payload))

    events.set_webhook_dispatcher(_disp)
    assert events.dispatcher_installed() is True
    asyncio.run(events.emit_event("t1", events.PII_DETECTED, {"count": 2}))
    assert seen == [("t1", "pii.detected", {"count": 2})]


def test_emit_swallows_dispatcher_exceptions():
    async def _boom(tenant_id, event, payload):
        raise RuntimeError("delivery down")

    events.set_webhook_dispatcher(_boom)
    # Must not propagate — delivery is best-effort.
    asyncio.run(events.emit_event("t1", events.SPEND_CAP_REACHED, {}))


def test_emit_defaults_blank_tenant_and_copies_payload():
    seen = {}

    async def _disp(tenant_id, event, payload):
        seen["tenant"] = tenant_id
        seen["payload"] = payload

    events.set_webhook_dispatcher(_disp)
    src = {"a": 1}
    asyncio.run(events.emit_event("", events.BUDGET_THRESHOLD, src))
    assert seen["tenant"] == "default"          # blank tenant → "default"
    assert seen["payload"] == {"a": 1}
    assert seen["payload"] is not src           # payload copied, not aliased


def test_schedule_event_noop_without_dispatcher():
    async def _run():
        # No dispatcher installed → schedule_event does nothing and creates no task.
        before = len(asyncio.all_tasks())
        events.schedule_event("t1", events.GUARDRAIL_BLOCK, {})
        assert len(asyncio.all_tasks()) == before

    asyncio.run(_run())


def test_schedule_event_fires_when_installed():
    seen = []

    async def _disp(tenant_id, event, payload):
        seen.append(event)

    async def _run():
        events.set_webhook_dispatcher(_disp)
        events.schedule_event("t1", events.GUARDRAIL_BLOCK, {})
        # Let the scheduled task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert seen == [events.GUARDRAIL_BLOCK]


def test_schedule_event_no_running_loop_is_safe():
    async def _disp(tenant_id, event, payload):
        pass

    events.set_webhook_dispatcher(_disp)
    # Called outside any running loop → swallowed RuntimeError, no raise.
    events.schedule_event("t1", events.PII_DETECTED, {})


def test_all_event_types_are_stable_strings():
    assert events.ALL_EVENT_TYPES == (
        "spend_cap.reached", "budget.threshold", "guardrail.block", "pii.detected",
        "trial.threshold", "trial.expired",
    )
