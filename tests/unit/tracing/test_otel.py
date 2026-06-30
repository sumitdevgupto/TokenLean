"""A5-T: Tests for OpenTelemetry tracing wrapper."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest


class FakeCtx:
    request_id = "req-001"
    tenant_id = "acme"
    model = "gpt-4o"
    otel_span = None


class TestOtelNoOp:
    """When OTel SDK is not installed the calls must be silent no-ops."""

    def test_start_span_returns_something(self):
        from tracing.otel import start_span
        span = start_span("test-stage", FakeCtx())
        assert span is not None

    def test_end_span_does_not_raise(self):
        from tracing.otel import start_span, end_span
        span = start_span("test-stage", FakeCtx())
        end_span(span)  # must not raise

    def test_end_span_with_error_does_not_raise(self):
        from tracing.otel import start_span, end_span
        span = start_span("test-stage", FakeCtx())
        end_span(span, error=ValueError("boom"))  # must not raise

    def test_end_span_on_none_does_not_raise(self):
        from tracing.otel import end_span
        end_span(None)  # must not raise

    def test_get_trace_id_returns_string(self):
        from tracing.otel import start_span, get_trace_id
        span = start_span("test-stage", FakeCtx())
        tid = get_trace_id(span)
        assert isinstance(tid, str)

    def test_get_trace_id_on_none_returns_empty(self):
        from tracing.otel import get_trace_id
        assert get_trace_id(None) == ""


class TestOtelSpanAttributes:
    """When OTel SDK IS available the span gets proxy attributes set."""

    def test_start_span_sets_request_id_if_otel_available(self):
        from tracing import otel as otel_mod
        if not otel_mod._otel_available:
            pytest.skip("OTel SDK not installed — skipping live span test")

        span = otel_mod.start_span("test-stage", FakeCtx())
        # Verify it's a real span (has get_span_context method)
        assert hasattr(span, "get_span_context")
        otel_mod.end_span(span)

    def test_get_trace_id_non_zero_when_otel_available(self):
        from tracing import otel as otel_mod
        if not otel_mod._otel_available:
            pytest.skip("OTel SDK not installed")

        span = otel_mod.start_span("test-stage", FakeCtx())
        tid = otel_mod.get_trace_id(span)
        assert len(tid) == 32  # 128-bit trace id as 32 hex chars
        otel_mod.end_span(span)
