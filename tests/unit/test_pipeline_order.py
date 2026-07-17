"""B3-T: Tests verifying G20 and G22 stage ordering in the pipeline."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from middleware import RequestContext
from savings.models import SavingsRecord


def _make_ctx():
    savings = SavingsRecord(
        request_id="req-order",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=50,
    )
    return RequestContext(
        request_id="req-order",
        user_id="u1",
        original_messages=[{"role": "user", "content": "hi"}],
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={"groups": {}},
        savings=savings,
    )


class TestPipelineStageOrdering:
    """Verify G20 runs after G02 and G22 runs after G10 using call-order tracking."""

    def _tracked_mock(self, call_log: list, name: str) -> AsyncMock:
        m = AsyncMock()
        async def _side(ctx):
            call_log.append(name)
            return ctx
        m.process_request.side_effect = _side
        return m

    @pytest.mark.asyncio
    async def test_g20_runs_after_g02_and_before_g07(self):
        """G20 must appear in call order between G02 and G07."""
        from middleware.pipeline import OptimisationPipeline

        call_log = []
        pipeline = OptimisationPipeline.__new__(OptimisationPipeline)

        def _make(name):
            return self._tracked_mock(call_log, name)

        pipeline._tenant_config_loader = AsyncMock()
        pipeline._tenant_config_loader.load = AsyncMock()
        pipeline.g00 = _make("G00")
        pipeline.g01 = _make("G01")
        pipeline.g02 = _make("G02")
        pipeline.g04 = _make("G04")
        pipeline.g05 = _make("G05")
        pipeline.g06 = _make("G06")
        pipeline.g07 = _make("G07")
        pipeline.g08 = _make("G08")
        pipeline.g09 = _make("G09")
        pipeline.g10 = _make("G10")
        pipeline.g11 = _make("G11")
        pipeline.g12 = _make("G12")
        pipeline.g13 = _make("G13")
        pipeline.g16 = _make("G16")
        pipeline.g17 = _make("G17")
        pipeline.g19 = _make("G19")
        pipeline.g20 = _make("G20")
        pipeline.g21 = _make("G21")
        pipeline.g22 = _make("G22")
        pipeline.g24 = _make("G24")
        pipeline.g25 = _make("G25")
        pipeline.g27 = _make("G27")
        pipeline.g28 = _make("G28")
        pipeline.g29 = _make("G29")
        pipeline.g30 = _make("G30")
        pipeline.g31 = _make("G31")
        # G18 is only in process_response; stub it for completeness
        pipeline.g18 = MagicMock()
        pipeline.g15 = _make("G15")

        # Mock langfuse_tracing so it doesn't try to connect
        with patch("middleware.pipeline.langfuse_tracing") as mock_lf, \
             patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            mock_lf.start_trace.return_value = None

            ctx = _make_ctx()
            await pipeline.process_request(ctx)

        assert "G20" in call_log
        assert "G02" in call_log
        assert "G07" in call_log
        g02_pos = call_log.index("G02")
        g20_pos = call_log.index("G20")
        g07_pos = call_log.index("G07")
        assert g02_pos < g20_pos < g07_pos, (
            f"Expected G02({g02_pos}) < G20({g20_pos}) < G07({g07_pos}), got: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_g22_runs_after_g10_and_before_g16(self):
        """G22 must appear in call order between G10 and G16."""
        from middleware.pipeline import OptimisationPipeline

        call_log = []
        pipeline = OptimisationPipeline.__new__(OptimisationPipeline)

        def _make(name):
            return self._tracked_mock(call_log, name)

        pipeline._tenant_config_loader = AsyncMock()
        pipeline._tenant_config_loader.load = AsyncMock()
        pipeline.g00 = _make("G00")
        pipeline.g01 = _make("G01")
        pipeline.g02 = _make("G02")
        pipeline.g04 = _make("G04")
        pipeline.g05 = _make("G05")
        pipeline.g06 = _make("G06")
        pipeline.g07 = _make("G07")
        pipeline.g08 = _make("G08")
        pipeline.g09 = _make("G09")
        pipeline.g10 = _make("G10")
        pipeline.g11 = _make("G11")
        pipeline.g12 = _make("G12")
        pipeline.g13 = _make("G13")
        pipeline.g16 = _make("G16")
        pipeline.g17 = _make("G17")
        pipeline.g19 = _make("G19")
        pipeline.g20 = _make("G20")
        pipeline.g21 = _make("G21")
        pipeline.g22 = _make("G22")
        pipeline.g24 = _make("G24")
        pipeline.g25 = _make("G25")
        pipeline.g27 = _make("G27")
        pipeline.g28 = _make("G28")
        pipeline.g29 = _make("G29")
        pipeline.g30 = _make("G30")
        pipeline.g31 = _make("G31")
        pipeline.g18 = MagicMock()
        pipeline.g15 = _make("G15")

        with patch("middleware.pipeline.langfuse_tracing") as mock_lf, \
             patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            mock_lf.start_trace.return_value = None

            ctx = _make_ctx()
            await pipeline.process_request(ctx)

        assert "G22" in call_log
        assert "G10" in call_log
        assert "G16" in call_log
        g10_pos = call_log.index("G10")
        g22_pos = call_log.index("G22")
        g16_pos = call_log.index("G16")
        assert g10_pos < g22_pos < g16_pos, (
            f"Expected G10({g10_pos}) < G22({g22_pos}) < G16({g16_pos}), got: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_g23_runs_after_g14_in_response_path(self):
        """G23 must appear in response call order after G14."""
        from middleware.pipeline import OptimisationPipeline
        from datetime import datetime, timezone

        resp_log = []

        def _make_resp_mock(name):
            m = MagicMock()
            async def _side(ctx, response):
                resp_log.append(name)
                return response
            m.process_response.side_effect = _side
            return m

        pipeline = OptimisationPipeline.__new__(OptimisationPipeline)

        call_log = []
        def _make(name):
            return self._tracked_mock(call_log, name)

        # Request-path stubs (needed by process_request call below isn't invoked,
        # so we only need process_response stubs for the response path)
        pipeline.g14 = _make_resp_mock("G14")
        pipeline.g29 = _make_resp_mock("G29")
        pipeline.g28 = _make_resp_mock("G28")
        pipeline.g23 = _make_resp_mock("G23")
        pipeline.g19 = _make_resp_mock("G19")
        pipeline.g15 = _make_resp_mock("G15")
        pipeline.g11 = MagicMock()
        pipeline.g11.process_response = AsyncMock(side_effect=lambda ctx, r: (ctx, r))
        pipeline.g18 = MagicMock()
        pipeline.g18.record = AsyncMock()
        pipeline.g05 = MagicMock()
        pipeline.g05.store_response = AsyncMock()

        ctx = _make_ctx()

        with patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            response = await pipeline.process_response(ctx, {"choices": []})

        assert "G14" in resp_log
        assert "G23" in resp_log
        g14_pos = resp_log.index("G14")
        g23_pos = resp_log.index("G23")
        assert g14_pos < g23_pos, (
            f"Expected G14({g14_pos}) < G23({g23_pos}) in response path, got: {resp_log}"
        )


def _build_stubbed_pipeline(call_log):
    """Fully-stubbed request-path pipeline (every gXX tracks its own name)."""
    from middleware.pipeline import OptimisationPipeline

    p = OptimisationPipeline.__new__(OptimisationPipeline)
    p._tenant_config_loader = AsyncMock()
    p._tenant_config_loader.load = AsyncMock()

    def _mk(name):
        m = AsyncMock()

        async def _side(ctx):
            call_log.append(name)
            return ctx

        m.process_request.side_effect = _side
        return m

    for n in ["g00", "g01", "g02", "g04", "g05", "g06", "g07", "g08", "g09", "g10",
              "g11", "g12", "g13", "g15", "g16", "g17", "g19", "g20", "g21", "g22",
              "g24", "g25", "g27", "g28", "g29", "g30", "g31"]:
        setattr(p, n, _mk(n.upper()))
    p.g18 = MagicMock()
    return p


class TestTrustSafetyStageOrdering:
    """G30 (guardrails) → G29 (PII) run after G24 and before the G04/G05 gate, and a
    block short-circuits the pipeline before any optimisation/cache stage."""

    @pytest.mark.asyncio
    async def test_g30_then_g29_after_g24_before_g04_g05(self):
        call_log = []
        pipeline = _build_stubbed_pipeline(call_log)
        with patch("middleware.pipeline.langfuse_tracing") as mock_lf, \
             patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            mock_lf.start_trace.return_value = None
            await pipeline.process_request(_make_ctx())

        for g in ["G24", "G30", "G29", "G04", "G05"]:
            assert g in call_log, f"{g} did not run: {call_log}"
        assert (call_log.index("G24") < call_log.index("G30")
                < call_log.index("G29") < call_log.index("G04")
                < call_log.index("G05")), call_log

    @pytest.mark.asyncio
    async def test_g31_context_trust_runs_after_g22_before_g16(self):
        """G31 must scan the ASSEMBLED context — after G07/G10/G22 have injected/mutated
        it — and before the model-facing Stage-4 groups (G16…)."""
        call_log = []
        pipeline = _build_stubbed_pipeline(call_log)
        with patch("middleware.pipeline.langfuse_tracing") as mock_lf, \
             patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            mock_lf.start_trace.return_value = None
            await pipeline.process_request(_make_ctx())

        for g in ["G22", "G31", "G16"]:
            assert g in call_log, f"{g} did not run: {call_log}"
        assert call_log.index("G22") < call_log.index("G31") < call_log.index("G16"), call_log

    @pytest.mark.asyncio
    async def test_g31_block_short_circuits_before_g16(self):
        """A G31 context-injection block returns immediately — Stage-4 (G16) must not run."""
        call_log = []
        pipeline = _build_stubbed_pipeline(call_log)

        async def _blocking(ctx):
            call_log.append("G31")
            ctx.security_blocked = True
            ctx.security_block_response = {"choices": []}
            return ctx

        pipeline.g31.process_request.side_effect = _blocking
        with patch("middleware.pipeline.langfuse_tracing") as mock_lf, \
             patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            mock_lf.start_trace.return_value = None
            ctx = await pipeline.process_request(_make_ctx())

        assert ctx.security_blocked
        assert "G31" in call_log
        assert "G16" not in call_log

    @pytest.mark.asyncio
    async def test_guardrail_block_short_circuits_before_g29_and_g04(self):
        call_log = []
        pipeline = _build_stubbed_pipeline(call_log)

        async def _blocking(ctx):
            call_log.append("G30")
            ctx.security_blocked = True
            ctx.security_block_response = {"choices": []}
            return ctx

        pipeline.g30.process_request.side_effect = _blocking
        with patch("middleware.pipeline.langfuse_tracing") as mock_lf, \
             patch("middleware.pipeline.otel") as mock_otel:
            mock_otel.start_span.return_value = MagicMock()
            mock_lf.start_trace.return_value = None
            ctx = await pipeline.process_request(_make_ctx())

        assert ctx.security_blocked
        assert "G30" in call_log
        # A block returns immediately — G29 and the G04/G05 gate must not run.
        assert "G29" not in call_log
        assert "G04" not in call_log and "G05" not in call_log
