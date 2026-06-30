"""I8 — Consolidated cross-tenant isolation invariants.

A single, explicit gate for the multi-tenancy guarantees so a regression in any
one of them fails loudly under a named CI step (not buried in a per-group file).
Covers: key-authoritative tenant resolution (C1), admin-only header impersonation
(C1), RAG-collection constraint (C2), admin-scope detection (H1), tenant_id
sanitisation (I5), and pgvector collection-name validation (I3).

All assertions are pure / unit-level — no Redis, Postgres, Qdrant, or LLM calls.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest

from tenancy.resolver import resolve_tenant
from tenancy.context import TenantContext, sanitise_tenant_id
from auth.api_key_manager import is_admin_key


# ── C1: the authenticated key is authoritative for tenant identity ───────────
class TestKeyAuthoritativeResolution:
    def test_no_header_uses_key_tenant(self):
        tc = resolve_tenant({}, key_tenant_id="nova-med", key_tier="pro")
        assert tc.tenant_id == "nova-med"
        assert tc.redis_prefix == "t:nova-med:"

    def test_no_key_no_header_is_default(self):
        assert resolve_tenant({}).tenant_id == "default"

    def test_bare_header_is_not_trusted(self):
        # The spoof that used to work: header with no authenticated binding.
        assert resolve_tenant({"x-tenant-id": "victim"}).tenant_id == "default"

    def test_non_admin_cannot_cross_tenants_via_header(self):
        tc = resolve_tenant(
            {"x-tenant-id": "victim"}, key_tenant_id="attacker", key_tier="basic"
        )
        assert tc.tenant_id == "attacker"  # header ignored, key wins

    def test_admin_key_may_impersonate(self):
        tc = resolve_tenant(
            {"x-tenant-id": "target"}, key_tenant_id="ops", key_tier="pro", key_is_admin=True
        )
        assert tc.tenant_id == "target"


# ── C1: distinct tenants get distinct namespaces ─────────────────────────────
class TestNamespaceScoping:
    def test_distinct_tenants_distinct_prefixes(self):
        a = TenantContext.for_tenant("tenant-a")
        b = TenantContext.for_tenant("tenant-b")
        assert a.redis_prefix != b.redis_prefix
        assert a.qdrant_collection != b.qdrant_collection

    def test_default_has_no_prefix(self):
        assert TenantContext.default().redis_prefix == ""


# ── H1: admin-scope detection ────────────────────────────────────────────────
class TestAdminScope:
    def test_admin_dict_is_admin(self):
        assert is_admin_key({"tenant_id": "ops", "admin": True}) is True

    def test_plain_tenant_key_is_not_admin(self):
        assert is_admin_key({"tenant_id": "nova-med", "tier": "pro"}) is False

    def test_legacy_string_key_is_not_admin(self):
        assert is_admin_key(None) is False


# ── C2: RAG collection constraint ────────────────────────────────────────────
class TestRagCollectionConstraint:
    def _ctx(self, params, is_admin):
        class C:
            pass
        c = C()
        c.params = params
        c.qdrant_collection = "rag_acme"
        c.is_admin_key = is_admin
        c.tenant_id = "acme"
        c.request_id = "r"
        return c

    def test_non_admin_override_ignored(self):
        from middleware.g07_retrieval import _resolve_collection
        ctx = self._ctx({"x_rag_collection": "rag_victim"}, is_admin=False)
        assert _resolve_collection(ctx, {}) == "rag_acme"

    def test_admin_override_allowed(self):
        from middleware.g07_retrieval import _resolve_collection
        ctx = self._ctx({"x_rag_collection": "pitch_docs"}, is_admin=True)
        assert _resolve_collection(ctx, {}) == "pitch_docs"


# ── I3: pgvector collection-name validation ──────────────────────────────────
class TestCollectionNameValidation:
    @pytest.mark.parametrize("name", ["rag_acme", "rag_docs", "pitch_docs"])
    def test_valid_names_accepted(self, name):
        from middleware.g07_retrieval import _is_valid_collection_name
        assert _is_valid_collection_name(name)

    @pytest.mark.parametrize("name", [
        "rag_acme; DROP TABLE cache_l2",
        "rag acme",
        "rag-acme",          # hyphen not allowed in a SQL identifier here
        "1rag",              # must start with a letter
        "",
        "RAG_ACME",          # uppercase not in the allowlist
    ])
    def test_injection_or_invalid_names_rejected(self, name):
        from middleware.g07_retrieval import _is_valid_collection_name
        assert not _is_valid_collection_name(name)


# ── I5: tenant_id sanitisation ───────────────────────────────────────────────
class TestTenantIdSanitisation:
    def test_separators_replaced(self):
        assert ":" not in sanitise_tenant_id("a:b/c")
        assert "/" not in sanitise_tenant_id("a:b/c")

    def test_empty_becomes_default(self):
        assert sanitise_tenant_id("") == "default"
        assert sanitise_tenant_id("   ") == "default"

    def test_length_capped(self):
        assert len(sanitise_tenant_id("x" * 200)) <= 64

    def test_leading_non_alnum_is_prefixed(self):
        out = sanitise_tenant_id("_evil")
        assert out[0].isalnum()
