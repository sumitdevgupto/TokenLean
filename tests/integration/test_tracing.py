"""A6-T: Integration test — OTel span export to Jaeger.

Requires docker-compose --profile observability to be running with Jaeger.
Skipped automatically when JAEGER_URL is not set or Jaeger is unreachable.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest
import urllib.request
import urllib.error

_JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")


def _jaeger_reachable() -> bool:
    try:
        urllib.request.urlopen(f"{_JAEGER_URL}/", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.mark.skipif(not _jaeger_reachable(), reason="Jaeger not running (start with --profile observability)")
class TestJaegerIntegration:
    def test_jaeger_ui_responds(self):
        resp = urllib.request.urlopen(f"{_JAEGER_URL}/")
        assert resp.status == 200

    def test_jaeger_api_returns_services(self):
        resp = urllib.request.urlopen(f"{_JAEGER_URL}/api/services")
        assert resp.status == 200

    def test_otel_export_does_not_raise_when_jaeger_running(self):
        from tracing.otel import start_span, end_span

        class FakeCtx:
            request_id = "integration-test-001"
            tenant_id = "test-tenant"
            model = "gpt-4o"

        span = start_span("integration-test-stage", FakeCtx())
        end_span(span)  # Should push span to Jaeger without raising
