"""C2-T: Tests for UsageMeter recording."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from billing.metering import UsageMeter
from billing.models import UsageEvent
from savings.models import SavingsRecord, StepSaving
from middleware import RequestContext


def _make_ctx():
    savings = SavingsRecord(
        request_id="req-meter",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o-mini",
        baseline_tokens=500,
        final_tokens_sent=300,
    )
    savings.step_savings.append(StepSaving("G01", "compressed", 500, 300))
    return RequestContext(
        request_id="req-meter",
        user_id="u1",
        original_messages=[{"role": "user", "content": "hi"}],
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        routed_model="gpt-4o-mini",
        params={},
        config={"groups": {}},
        savings=savings,
        tenant_id="acme",
        pricing_tier="enterprise",
    )


class TestUsageMeterBuildsEvent:
    def test_build_event_has_correct_tenant(self):
        meter = UsageMeter()
        ctx = _make_ctx()
        event = meter._build_event(ctx, {})
        assert event.tenant_id == "acme"

    def test_build_event_has_correct_tokens(self):
        meter = UsageMeter()
        ctx = _make_ctx()
        event = meter._build_event(ctx, {})
        assert event.baseline_tokens == 500
        assert event.optimised_tokens == 300
        assert event.tokens_saved == 200

    def test_build_event_groups_applied(self):
        meter = UsageMeter()
        ctx = _make_ctx()
        event = meter._build_event(ctx, {})
        assert "G01" in event.groups_applied

    def test_build_event_pricing_tier(self):
        meter = UsageMeter()
        ctx = _make_ctx()
        event = meter._build_event(ctx, {})
        assert event.pricing_tier == "enterprise"

    def test_build_event_carries_xyz(self):
        # C2: x = baseline_tokens, y = proxy_optimised_tokens, z = provider_prompt_tokens.
        meter = UsageMeter()
        ctx = _make_ctx()
        ctx.savings.proxy_optimised_tokens = 320
        ctx.savings.provider_prompt_tokens = 300
        event = meter._build_event(ctx, {})
        assert event.baseline_tokens == 500          # x
        assert event.proxy_optimised_tokens == 320   # y
        assert event.provider_prompt_tokens == 300   # z

    def test_build_event_carries_response_tokens(self):
        # Real output tokens (observability): mapped from savings.response_tokens.
        meter = UsageMeter()
        ctx = _make_ctx()
        ctx.savings.response_tokens = 145
        event = meter._build_event(ctx, {})
        assert event.response_tokens == 145
        # Defaults to 0 when absent (defer / no-usage paths).
        ctx2 = _make_ctx()
        assert meter._build_event(ctx2, {}).response_tokens == 0

    def test_build_event_carries_ingress_protocol(self):
        # #4: the protocol column mirrors ctx.ingress_protocol (defaults to openai).
        meter = UsageMeter()
        ctx = _make_ctx()
        ctx.ingress_protocol = "anthropic"
        assert meter._build_event(ctx, {}).protocol == "anthropic"
        assert meter._build_event(_make_ctx(), {}).protocol == "openai"

    def test_build_event_carries_user_and_flags(self):
        # Requests Explorer filter columns mirror the request-context flags;
        # complexity_tier comes from params.x_complexity_tier (X-Complexity-Tier header).
        meter = UsageMeter()
        ctx = _make_ctx()
        ctx.user_id = "u-77"
        ctx.cache_hit = True
        ctx.cache_level = "L2"
        ctx.bypassed = True
        ctx.params = {"x_complexity_tier": "complex"}
        event = meter._build_event(ctx, {})
        assert event.user_id == "u-77"
        assert event.cache_hit is True
        assert event.cache_level == "L2"
        assert event.complexity_tier == "complex"
        assert event.bypassed is True

    def test_build_event_filter_fields_default_safely(self):
        # Empty params / falsy ctx flags → safe, non-null defaults.
        meter = UsageMeter()
        ctx = _make_ctx()  # params={}, cache_level=None, cache_hit/bypassed=False
        event = meter._build_event(ctx, {})
        assert event.cache_level == ""
        assert event.complexity_tier == ""
        assert event.cache_hit is False
        assert event.bypassed is False

    def test_build_event_cache_hit_shows_estimated_cost_saved(self):
        # C2: cache-hit rows skip G18 (cost_saving_usd == 0) — derive the avoided
        # input cost so the confidence story isn't $0 on cached traffic.
        meter = UsageMeter()
        ctx = _make_ctx()
        ctx.savings.cache_hit = True
        ctx.savings.final_tokens_sent = 0
        event = meter._build_event(ctx, {})
        assert event.tokens_saved == 500   # full baseline saved (100%)
        assert event.cost_saved_usd > 0    # estimated avoided input cost, not 0


class TestUsageMeterRecord:
    @pytest.mark.asyncio
    async def test_postgres_insert_called_with_correct_values(self):
        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        meter = UsageMeter(db_pool=mock_pool)
        ctx = _make_ctx()
        await meter.record(ctx, {})

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args[0]
        # First arg is the SQL, remaining are positional params
        assert "acme" in call_args  # tenant_id
        assert "req-meter" in call_args  # request_id

    @pytest.mark.asyncio
    async def test_openmeter_post_called_with_correct_payload(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="")

        mock_http = MagicMock()
        mock_http.post.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_http.post.return_value.__aexit__ = AsyncMock(return_value=False)

        meter = UsageMeter(
            http_session=mock_http,
            openmeter_url="http://openmeter:8888",
        )
        ctx = _make_ctx()
        await meter.record(ctx, {})

        mock_http.post.assert_called_once()
        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["subject"] == "acme"
        assert payload["data"]["tokens_saved"] == 200

    @pytest.mark.asyncio
    async def test_timeout_does_not_block_caller(self):
        """A TimeoutError from OpenMeter must be swallowed, not re-raised."""
        import asyncio

        mock_http = MagicMock()
        # Simulate aiohttp raising TimeoutError during the post context
        mock_http.post.return_value.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_http.post.return_value.__aexit__ = AsyncMock(return_value=False)

        meter = UsageMeter(
            http_session=mock_http,
            openmeter_url="http://openmeter:8888",
        )
        ctx = _make_ctx()
        # Must not raise — timeout is caught inside record()
        await meter.record(ctx, {})

    @pytest.mark.asyncio
    async def test_no_db_no_http_does_not_raise(self):
        meter = UsageMeter()
        ctx = _make_ctx()
        await meter.record(ctx, {})  # must not raise
