"""A2-T: Tests for TenantContext resolver."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from tenancy.context import TenantContext
from tenancy.resolver import resolve_tenant, clear_registry


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


class TestTenantContextForTenant:
    def test_for_tenant_sets_redis_prefix(self):
        tc = TenantContext.for_tenant("acme")
        assert tc.redis_prefix == "t:acme:"

    def test_for_tenant_sets_qdrant_collection(self):
        tc = TenantContext.for_tenant("acme")
        assert tc.qdrant_collection == "rag_acme"

    def test_for_tenant_default_has_no_prefix(self):
        tc = TenantContext.for_tenant("default")
        assert tc.redis_prefix == ""
        assert tc.qdrant_collection == "rag_docs"

    def test_for_tenant_sanitises_colon_in_id(self):
        tc = TenantContext.for_tenant("tenant:with:colons")
        assert ":" not in tc.qdrant_collection
        assert tc.tenant_id == "tenant_with_colons"

    def test_for_tenant_inherits_pricing_tier(self):
        tc = TenantContext.for_tenant("acme", pricing_tier="enterprise")
        assert tc.pricing_tier == "enterprise"


class TestTenantContextDefault:
    def test_default_has_empty_prefix(self):
        tc = TenantContext.default()
        assert tc.redis_prefix == ""

    def test_default_uses_rag_docs_collection(self):
        tc = TenantContext.default()
        assert tc.qdrant_collection == "rag_docs"

    def test_default_tenant_id(self):
        tc = TenantContext.default()
        assert tc.tenant_id == "default"

    def test_default_tier_is_basic(self):
        tc = TenantContext.default()
        assert tc.pricing_tier == "basic"


class TestResolveByHeader:
    """X-Tenant-ID is honoured ONLY for admin keys (C1). A bare header — with no
    authenticated tenant binding — is never trusted (closes the spoof)."""

    def test_header_alone_is_ignored_falls_back_to_default(self):
        # No key context → header must NOT resolve to the claimed tenant.
        tc = resolve_tenant({"x-tenant-id": "acme"})
        assert tc.tenant_id == "default"

    def test_admin_key_header_impersonates(self):
        tc = resolve_tenant({"x-tenant-id": "acme"}, key_is_admin=True, key_tier="pro")
        assert tc.tenant_id == "acme"
        assert tc.redis_prefix == "t:acme:"
        assert tc.pricing_tier == "pro"

    def test_empty_header_falls_back_to_default(self):
        tc = resolve_tenant({"x-tenant-id": ""})
        assert tc.tenant_id == "default"

    def test_missing_header_falls_back_to_default(self):
        tc = resolve_tenant({})
        assert tc.tenant_id == "default"


class TestResolveByApiKey:
    """The authenticated key's tenant is authoritative (C1)."""

    def test_key_tenant_resolves(self):
        tc = resolve_tenant({}, key_tenant_id="corp", key_tier="pro")
        assert tc.tenant_id == "corp"
        assert tc.pricing_tier == "pro"

    def test_no_signal_falls_back_to_default(self):
        tc = resolve_tenant({})
        assert tc.tenant_id == "default"

    def test_non_admin_header_cannot_override_key_tenant(self):
        # A non-admin key sending X-Tenant-ID for ANOTHER tenant stays on its own.
        tc = resolve_tenant(
            {"x-tenant-id": "victim"},
            key_tenant_id="corp",
            key_tier="enterprise",
        )
        assert tc.tenant_id == "corp"
        assert tc.pricing_tier == "enterprise"

    def test_admin_header_overrides_key_tenant(self):
        tc = resolve_tenant(
            {"x-tenant-id": "other"},
            key_tenant_id="corp",
            key_tier="enterprise",
            key_is_admin=True,
        )
        assert tc.tenant_id == "other"

    def test_legacy_registry_fallback_still_works(self):
        registry = {"hash-abc": TenantContext.for_tenant("corp", pricing_tier="pro")}
        tc = resolve_tenant({}, api_key_hash="hash-abc", tenant_registry=registry)
        assert tc.tenant_id == "corp"
        assert tc.pricing_tier == "pro"
