"""C5-T: Tests that G18 calls UsageMeter.record when billing is enabled."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import RequestContext
from middleware.g18_observability import G18Observability
from savings.models import SavingsRecord


def _make_ctx(billing_enabled=True):
    savings = SavingsRecord(
        request_id="req-g18",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=500,
        final_tokens_sent=300,
    )
    return RequestContext(
        request_id="req-g18",
        user_id="u1",
        original_messages=[{"role": "user", "content": "hi"}],
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={
            "groups": {
                "G18_observability": {
                    "enabled": True,
                    "prometheus_enabled": False,
                    "turn_efficiency_enabled": False,
                    "tool_governance_enabled": False,
                },
            },
            "billing": {"enabled": billing_enabled},
        },
        savings=savings,
    )


class TestG18BillingIntegration:
    @pytest.mark.asyncio
    async def test_usage_meter_called_when_billing_enabled(self):
        mock_meter = AsyncMock()
        g18 = G18Observability(usage_meter=mock_meter)
        ctx = _make_ctx(billing_enabled=True)
        response = {"usage": {"prompt_tokens": 300, "completion_tokens": 100}}

        with patch.object(g18, "_usage_meter", mock_meter), \
             patch("middleware.g18_observability._emit_trace", AsyncMock()), \
             patch.object(g18, "_check_turn_efficiency", AsyncMock()), \
             patch.object(g18, "_record_tool_calls", AsyncMock()):
            await g18.record(ctx, response)

        mock_meter.record.assert_called_once_with(ctx, response)

    @pytest.mark.asyncio
    async def test_usage_meter_not_called_when_billing_disabled(self):
        mock_meter = AsyncMock()
        g18 = G18Observability(usage_meter=mock_meter)
        ctx = _make_ctx(billing_enabled=False)
        response = {"usage": {"prompt_tokens": 300, "completion_tokens": 100}}

        with patch.object(g18, "_usage_meter", mock_meter), \
             patch("middleware.g18_observability._emit_trace", AsyncMock()), \
             patch.object(g18, "_check_turn_efficiency", AsyncMock()), \
             patch.object(g18, "_record_tool_calls", AsyncMock()):
            await g18.record(ctx, response)

        mock_meter.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_prometheus_metrics_still_recorded_with_billing(self):
        """Regression: Prometheus metrics must still record when billing is enabled."""
        mock_meter = AsyncMock()
        g18 = G18Observability(usage_meter=mock_meter)
        ctx = _make_ctx(billing_enabled=True)
        ctx.config["groups"]["G18_observability"]["prometheus_enabled"] = True
        response = {"usage": {"prompt_tokens": 300, "completion_tokens": 100}}

        with patch.object(g18, "_usage_meter", mock_meter), \
             patch("middleware.g18_observability._emit_trace", AsyncMock()), \
             patch.object(g18, "_check_turn_efficiency", AsyncMock()), \
             patch.object(g18, "_record_tool_calls", AsyncMock()), \
             patch("middleware.g18_observability.REQUESTS_TOTAL") as mock_counter:
            await g18.record(ctx, response)

        # G18 should still record Prometheus metrics
        mock_counter.labels.assert_called()

    @pytest.mark.asyncio
    async def test_billing_error_does_not_break_g18(self):
        """If UsageMeter.record raises, G18 must still complete normally."""
        error_meter = AsyncMock()
        error_meter.record.side_effect = RuntimeError("billing outage")
        g18 = G18Observability(usage_meter=error_meter)
        ctx = _make_ctx(billing_enabled=True)
        response = {"usage": {"prompt_tokens": 300, "completion_tokens": 100}}

        with patch.object(g18, "_usage_meter", error_meter), \
             patch("middleware.g18_observability._emit_trace", AsyncMock()), \
             patch.object(g18, "_check_turn_efficiency", AsyncMock()), \
             patch.object(g18, "_record_tool_calls", AsyncMock()):
            # Must NOT raise
            await g18.record(ctx, response)
