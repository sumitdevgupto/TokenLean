"""Paths that return a response WITHOUT running the response pipeline.

G32's guarantee is positional — it runs before G14/G28/G15, the groups that auto-execute
tools. Any path that serves a response while skipping the response chain silently voids
that guarantee, so each one needs an explicit decision and a test pinning it:

  * **cache hit / bypass** -> hoisted (G32 called directly); covered in
    `test_g32_short_circuit_paths.py`.
  * **streaming** -> NOT gated; a documented limitation, pinned in
    `test_g32_tool_eligibility.py`.
  * **batch defer** -> the request is refused batching when it carries tools, so it
    falls through to the normal gated path. Pinned here.

Also here: the G31 audit gap. `context_trust_action` was absent from BOTH the audit
branch and main.py's scheduling guard, so a context-trust block wrote no audit row at
all — the only trust & safety verdict with no compliance trail.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from middleware import RequestContext
from middleware.g13_batch import G13Batch
from savings.models import SavingsRecord


def _ctx(params=None, tenant_id="acme"):
    return RequestContext(
        request_id="req-b", user_id="u", original_messages=[], messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params=params or {},
        config={"groups": {"G13_batch": {"enabled": True}}}, tenant_id=tenant_id,
        savings=SavingsRecord(request_id="req-b", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


_TOOLS = [{"type": "function", "function": {"name": "delete_everything", "parameters": {}}}]


class TestBatchDoesNotBypassTheGate:
    """A batched request is answered out-of-band by the flush worker and delivered by
    `/v1/batch/results/{id}`; neither path runs `process_response`, so G32 would never
    see its tool calls. G13 ships `enabled: true`, so this was reachable with nothing but
    a `batch_topic` on a tool-bearing request."""

    async def test_tool_bearing_request_is_not_deferred(self):
        ctx = _ctx({"batch_topic": "bulk", "tools": _TOOLS})
        with patch("middleware.g13_batch._accumulate", new=AsyncMock()) as acc:
            out = await G13Batch().process_request(ctx)
        assert out.batch_deferred is False, (
            "a tool-bearing request was batched — its result skips the response pipeline, "
            "so the G32 eligibility gate would never run on it"
        )
        acc.assert_not_awaited()

    async def test_deprecated_functions_field_counts_too(self):
        ctx = _ctx({"batch_topic": "bulk", "functions": [{"name": "wire_money"}]})
        with patch("middleware.g13_batch._accumulate", new=AsyncMock()):
            assert (await G13Batch().process_request(ctx)).batch_deferred is False

    async def test_plain_request_still_batches(self):
        """The fix must not disable batching for the bulk prose it exists to serve."""
        ctx = _ctx({"batch_topic": "bulk"})
        with patch("middleware.g13_batch._accumulate", new=AsyncMock()) as acc, \
             patch("middleware.g13_batch._record_batch_owner", new=AsyncMock()):
            out = await G13Batch().process_request(ctx)
        assert out.batch_deferred is True
        acc.assert_awaited_once()

    async def test_empty_tools_list_still_batches(self):
        ctx = _ctx({"batch_topic": "bulk", "tools": []})
        with patch("middleware.g13_batch._accumulate", new=AsyncMock()), \
             patch("middleware.g13_batch._record_batch_owner", new=AsyncMock()):
            assert (await G13Batch().process_request(ctx)).batch_deferred is True


class TestContextTrustAudit:
    """G31's injection verdict must reach `audit_events` like every sibling verdict."""

    @pytest.mark.parametrize("action,expected", [
        ("flag", "context_trust.flagged"),
        ("block", "context_trust.blocked"),
        ("strip", "context_trust.stripped"),
    ])
    async def test_verdict_writes_an_audit_row(self, action, expected):
        from audit.log import AuditLogger
        logger = AuditLogger(db_pool=MagicMock())
        logger.log_config_change = AsyncMock(return_value=True)

        ctx = _ctx()
        ctx.context_trust_action = action
        ctx.context_trust_categories = ["instruction_override"]
        await logger.log_security_events(ctx)

        logger.log_config_change.assert_awaited_once()
        kw = logger.log_config_change.await_args.kwargs
        assert kw["action"] == expected
        assert kw["details"]["categories"] == ["instruction_override"]
        # `source` is what lets a reviewer tell a poisoned corpus (G31) apart from a
        # hostile user (G30) — both are injection, only one is an internal incident.
        assert kw["details"]["source"] == "retrieved"

    async def test_allow_writes_nothing(self):
        from audit.log import AuditLogger
        logger = AuditLogger(db_pool=MagicMock())
        logger.log_config_change = AsyncMock(return_value=True)
        ctx = _ctx()
        ctx.context_trust_action = "allow"
        await logger.log_security_events(ctx)
        logger.log_config_change.assert_not_awaited()

    def test_scheduler_guard_includes_context_trust(self):
        """The audit branch is unreachable if main.py never schedules the task."""
        import inspect, main
        src = inspect.getsource(main._schedule_security_audit)
        assert '"context_trust_action"' in src, (
            "a pure G31 injection verdict schedules no audit task, so the audit branch "
            "never runs"
        )

    def test_portal_taxonomy_exposes_the_new_kind(self):
        """The Security tab reads `_SECURITY_ACTIONS_BY_KIND`; a row it does not list is
        invisible to the customer even though it is in the ledger."""
        try:
            from api.portal import _SECURITY_ACTIONS_BY_KIND
        except Exception:
            pytest.skip("commercial portal not present in this tree")
        from audit.log import _CONTEXT_TRUST_ACTIONS
        assert set(_CONTEXT_TRUST_ACTIONS.values()) <= set(
            _SECURITY_ACTIONS_BY_KIND["context_trust"])


class TestShortCircuitGateFailureIsLoud:
    """Staying fail-open on the cache/bypass hoist is deliberate — but a broken gate must
    not look identical to a clean one."""

    async def test_failure_emits_a_metric_and_logs_at_error(self, caplog):
        import logging, main
        from middleware.quality_metrics import TOOL_ELIGIBILITY_FAILURES_TOTAL

        def _count():
            for m in TOOL_ELIGIBILITY_FAILURES_TOTAL.collect():
                for s in m.samples:
                    if s.labels.get("tenant_id") == "acme" and s.name.endswith("_total"):
                        return s.value
            return 0.0

        before = _count()
        broken = MagicMock()
        broken.process_response = AsyncMock(side_effect=RuntimeError("policy engine down"))
        response = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

        with patch.object(main, "_pipeline", MagicMock(g32=broken)), \
             caplog.at_level(logging.ERROR):
            out = await main._apply_tool_eligibility_on_short_circuit(_ctx(), response)

        assert out is response, "a broken gate must not turn a served cache hit into an error"
        assert _count() == before + 1, "gate failure was not counted"
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "fail-open logged below ERROR — a permanently broken gate would be invisible"
        )
