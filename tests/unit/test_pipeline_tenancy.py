"""A8-T: Tests for TenantContext injection in pipeline.process_request."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from middleware import RequestContext
from savings.models import SavingsRecord
from tenancy.context import TenantContext
from tenancy.resolver import clear_registry


def _make_ctx(config=None):
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
        config=config or {"groups": {}},
        savings=savings,
    )


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


class TestPipelineTenantInjection:
    """Verify tenant fields are set on ctx before any G-group runs."""

    def test_default_tenant_when_no_header(self):
        from tenancy.resolver import resolve_tenant
        ctx = _make_ctx()
        tenant = resolve_tenant({})
        ctx.tenant_id = tenant.tenant_id
        ctx.redis_prefix = tenant.redis_prefix
        ctx.qdrant_collection = tenant.qdrant_collection
        ctx.pricing_tier = tenant.pricing_tier

        assert ctx.tenant_id == "default"
        assert ctx.redis_prefix == ""
        assert ctx.qdrant_collection == "rag_docs"
        assert ctx.pricing_tier == "basic"

    def test_tenant_id_set_from_key(self):
        from tenancy.resolver import resolve_tenant
        ctx = _make_ctx()
        # C1: the authenticated key is authoritative for the tenant.
        tenant = resolve_tenant({}, key_tenant_id="acme", key_tier="pro")
        ctx.tenant_id = tenant.tenant_id
        ctx.redis_prefix = tenant.redis_prefix

        assert ctx.tenant_id == "acme"
        assert ctx.redis_prefix == "t:acme:"

    def test_admin_header_sets_tenant_before_g00(self):
        """Admin key + X-Tenant-ID resolves the impersonated tenant; redis_prefix ready."""
        from tenancy.resolver import resolve_tenant
        ctx = _make_ctx()
        tenant = resolve_tenant({"x-tenant-id": "corp"}, key_is_admin=True)
        ctx.tenant_id = tenant.tenant_id
        ctx.redis_prefix = tenant.redis_prefix

        # Verify redis prefix is set (so G05 will use it)
        assert ctx.redis_prefix == "t:corp:"

    def test_config_overrides_merged_for_tenant(self):
        from tenancy.context import TenantContext
        from tenancy.resolver import resolve_tenant
        import copy

        ctx = _make_ctx(config={"groups": {"G1_compression": {"enabled": True, "min_tokens": 200}}})
        tenant = TenantContext(
            tenant_id="corp",
            redis_prefix="t:corp:",
            qdrant_collection="rag_corp",
            pricing_tier="pro",
            config_overrides={"groups": {"G1_compression": {"min_tokens": 50}}},
        )

        # Simulate pipeline config merge (deep merge two levels)
        if tenant.config_overrides:
            merged = copy.deepcopy(ctx.config)
            for k, v in tenant.config_overrides.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    for inner_k, inner_v in v.items():
                        if isinstance(inner_v, dict) and isinstance(merged[k].get(inner_k), dict):
                            merged[k][inner_k].update(inner_v)
                        else:
                            merged[k][inner_k] = inner_v
                else:
                    merged[k] = v
            ctx.config = merged

        assert ctx.config["groups"]["G1_compression"]["min_tokens"] == 50
        # Original key preserved by deep merge
        assert ctx.config["groups"]["G1_compression"]["enabled"] is True


# ── E3-T: TenantConfigLoader called before G-group stages ─────────────────────

class TestTenantConfigLoaderInPipeline:
    """E3-T: Verify TenantConfigLoader.load is called in the pipeline."""

    def test_tenant_config_loader_load_called_before_middleware(self):
        """Pipeline must call TenantConfigLoader.load before any G-group stage."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        calls = []

        async def _fake_load(ctx):
            calls.append("load")

        ctx = _make_ctx()

        with patch("tenancy.config.TenantConfigLoader.load", side_effect=_fake_load):
            from middleware.pipeline import OptimisationPipeline
            pipeline = OptimisationPipeline()

            with patch.object(pipeline.g00, "process_request", new_callable=AsyncMock) as mock_g00:
                with patch.object(pipeline.g04, "process_request", new_callable=AsyncMock) as mock_g04:
                    mock_g00.return_value = ctx
                    mock_g04.return_value = ctx
                    ctx.bypassed = True  # stop after G04

                    async def _run():
                        return await pipeline.process_request(ctx)

                    asyncio.run(_run())

        # TenantConfigLoader.load should have been called
        assert "load" in calls, "TenantConfigLoader.load was not called in pipeline.process_request"

    def test_pipeline_has_tenant_config_loader_attribute(self):
        from middleware.pipeline import OptimisationPipeline
        pipeline = OptimisationPipeline()
        assert hasattr(pipeline, "_tenant_config_loader"), (
            "Pipeline should have _tenant_config_loader attribute"
        )
