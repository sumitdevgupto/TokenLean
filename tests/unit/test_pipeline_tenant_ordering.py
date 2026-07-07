"""Verify Langfuse trace creation happens after tenant resolution, so
ctx.tenant_id is correct (not "default") by the time start_trace() runs and
writes it into the trace metadata that backs the Grafana Tenant dropdown."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from middleware import RequestContext
from savings.models import SavingsRecord
from tenancy.resolver import clear_registry


def _make_ctx():
    savings = SavingsRecord(
        request_id="req-001",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=50,
    )
    return RequestContext(
        request_id="req-001",
        user_id="u1",
        original_messages=[{"role": "user", "content": "hi"}],
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={"groups": {}},
        savings=savings,
    )


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


class TestStartTraceCalledAfterTenantInjection:
    def test_start_trace_called_after_tenant_injection(self):
        ctx = _make_ctx()
        # C1: tenant comes from the authenticated key (main.py stashes _auth_*).
        ctx.params["_auth_tenant_id"] = "corp"
        ctx.params["_auth_tier"] = "enterprise"
        observed_tenant_ids = []

        def _record_tenant(passed_ctx):
            observed_tenant_ids.append(passed_ctx.tenant_id)
            return None

        with patch("tenancy.config.TenantConfigLoader.load", new_callable=AsyncMock):
            from middleware.pipeline import OptimisationPipeline
            pipeline = OptimisationPipeline()

            with patch.object(pipeline.g00, "process_request", new_callable=AsyncMock) as mock_g00:
                with patch("middleware.pipeline.langfuse_tracing.start_trace", side_effect=_record_tenant):
                    mock_g00.return_value = ctx
                    ctx.bypassed = True  # stop right after G00 so we don't run the full pipeline

                    async def _run():
                        return await pipeline.process_request(ctx, request_headers={"X-Tenant-ID": "corp"})

                    asyncio.run(_run())

        assert observed_tenant_ids == ["corp"], (
            "start_trace() observed ctx.tenant_id=%r — tenant resolution must run before start_trace()"
            % observed_tenant_ids
        )
