"""A3-T + A4-T: Tests for Redis namespace isolation and Qdrant collection routing."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from middleware.g05_cache import _cache_key, _step_cache_key


class TestRedisCacheKeyNamespacing:
    def test_default_prefix_uses_no_namespace(self):
        key = _cache_key("hello world", prefix="")
        assert key.startswith("tok_opt:l1:")

    def test_tenant_prefix_prepended_to_key(self):
        key = _cache_key("hello world", prefix="t:acme:")
        assert key.startswith("t:acme:tok_opt:l1:")

    def test_different_tenants_produce_different_keys(self):
        k1 = _cache_key("same query", prefix="t:tenant-a:")
        k2 = _cache_key("same query", prefix="t:tenant-b:")
        assert k1 != k2

    def test_same_tenant_same_query_produces_same_key(self):
        k1 = _cache_key("same query", prefix="t:acme:")
        k2 = _cache_key("same query", prefix="t:acme:")
        assert k1 == k2

    def test_step_cache_key_uses_prefix(self):
        k = _step_cache_key("step1", "hash123", "v1", prefix="t:corp:")
        assert k.startswith("t:corp:tok_opt:step:")

    def test_step_cache_key_default_no_prefix(self):
        k = _step_cache_key("step1", "hash123", "v1")
        assert k.startswith("tok_opt:step:")


class TestG10SessionKeyNamespacing:
    def test_session_prefix_uses_redis_prefix(self):
        from middleware.g10_memory import _session_prefix

        class FakeCtx:
            redis_prefix = "t:acme:"

        result = _session_prefix(FakeCtx())
        assert result == "t:acme:tok_opt:session:"

    def test_session_prefix_empty_for_default_tenant(self):
        from middleware.g10_memory import _session_prefix

        class FakeCtx:
            redis_prefix = ""

        result = _session_prefix(FakeCtx())
        assert result == "tok_opt:session:"

    def test_tenant_a_session_key_differs_from_tenant_b(self):
        from middleware.g10_memory import _session_prefix

        class CtxA:
            redis_prefix = "t:tenant-a:"

        class CtxB:
            redis_prefix = "t:tenant-b:"

        assert _session_prefix(CtxA()) != _session_prefix(CtxB())


class TestQdrantCollectionRouting:
    """G07 collection resolution (C2): client override allowed only for admin keys."""

    def _ctx(self, *, params=None, qdrant_collection="rag_acme", is_admin_key=False):
        class FakeCtx:
            pass
        c = FakeCtx()
        c.params = params or {}
        c.qdrant_collection = qdrant_collection
        c.is_admin_key = is_admin_key
        c.tenant_id = "acme"
        c.request_id = "req-test"
        return c

    def test_ctx_qdrant_collection_used_in_retrieval(self):
        from middleware.g07_retrieval import _resolve_collection
        assert _resolve_collection(self._ctx(), {}) == "rag_acme"

    def test_env_fallback_used_when_ctx_has_default(self):
        from middleware.g07_retrieval import _resolve_collection
        assert _resolve_collection(self._ctx(qdrant_collection="rag_docs"), {}) == "rag_docs"

    def test_non_admin_client_override_is_ignored(self):
        """C2: a non-admin x_rag_collection cannot escape the tenant collection."""
        from middleware.g07_retrieval import _resolve_collection
        ctx = self._ctx(params={"x_rag_collection": "rag_victim"}, is_admin_key=False)
        assert _resolve_collection(ctx, {}) == "rag_acme"

    def test_admin_client_override_wins(self):
        from middleware.g07_retrieval import _resolve_collection
        ctx = self._ctx(params={"x_rag_collection": "pitch_docs"}, is_admin_key=True)
        assert _resolve_collection(ctx, {}) == "pitch_docs"

    def test_operator_config_collection_used_when_no_override(self):
        from middleware.g07_retrieval import _resolve_collection
        assert _resolve_collection(self._ctx(), {"collection": "rag_cfg"}) == "rag_cfg"
