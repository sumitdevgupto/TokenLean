"""A1-T: Tests for extended RequestContext tenant fields."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone
from middleware import RequestContext
from savings.models import SavingsRecord


def _make_savings(model="gpt-4o"):
    return SavingsRecord(
        request_id="req-test",
        user_id="user-test",
        timestamp=datetime.now(timezone.utc),
        model_requested=model,
        routed_model=model,
        baseline_tokens=100,
    )


def _base_ctx(**overrides):
    defaults = dict(
        request_id="req-001",
        user_id="user-001",
        original_messages=[{"role": "user", "content": "hi"}],
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={},
        savings=_make_savings(),
    )
    defaults.update(overrides)
    return RequestContext(**defaults)


class TestRequestContextTenantDefaults:
    def test_tenant_id_defaults_to_default(self):
        ctx = _base_ctx()
        assert ctx.tenant_id == "default"

    def test_redis_prefix_defaults_empty(self):
        ctx = _base_ctx()
        assert ctx.redis_prefix == ""

    def test_qdrant_collection_defaults_to_rag_docs(self):
        ctx = _base_ctx()
        assert ctx.qdrant_collection == "rag_docs"

    def test_pricing_tier_defaults_to_basic(self):
        ctx = _base_ctx()
        assert ctx.pricing_tier == "basic"

    def test_otel_span_defaults_to_none(self):
        ctx = _base_ctx()
        assert ctx.otel_span is None


class TestRequestContextTenantOverrides:
    def test_tenant_id_set_explicitly(self):
        ctx = _base_ctx(tenant_id="acme")
        assert ctx.tenant_id == "acme"

    def test_redis_prefix_set_explicitly(self):
        ctx = _base_ctx(redis_prefix="t:acme:")
        assert ctx.redis_prefix == "t:acme:"

    def test_qdrant_collection_set_explicitly(self):
        ctx = _base_ctx(qdrant_collection="rag_acme")
        assert ctx.qdrant_collection == "rag_acme"

    def test_pricing_tier_enterprise(self):
        ctx = _base_ctx(pricing_tier="enterprise")
        assert ctx.pricing_tier == "enterprise"

    def test_otel_span_set_explicitly(self):
        sentinel = object()
        ctx = _base_ctx(otel_span=sentinel)
        assert ctx.otel_span is sentinel


class TestRequestContextCreate:
    def test_create_without_tenant_args_uses_defaults(self):
        ctx = RequestContext.create(
            request_id="req-001",
            user_id="u1",
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            params={},
            config={},
        )
        assert ctx.tenant_id == "default"
        assert ctx.redis_prefix == ""
        assert ctx.qdrant_collection == "rag_docs"
        assert ctx.pricing_tier == "basic"

    def test_create_with_tenant_args(self):
        ctx = RequestContext.create(
            request_id="req-001",
            user_id="u1",
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
            params={},
            config={},
            tenant_id="acme",
            redis_prefix="t:acme:",
            qdrant_collection="rag_acme",
            pricing_tier="pro",
        )
        assert ctx.tenant_id == "acme"
        assert ctx.redis_prefix == "t:acme:"
        assert ctx.qdrant_collection == "rag_acme"
        assert ctx.pricing_tier == "pro"

    def test_create_deep_copies_messages(self):
        msgs = [{"role": "user", "content": "hello"}]
        ctx = RequestContext.create("r", "u", msgs, "gpt-4o", {}, {})
        msgs[0]["content"] = "mutated"
        assert ctx.messages[0]["content"] == "hello"
        assert ctx.original_messages[0]["content"] == "hello"


class TestCurrentRequestTokenCount:
    """B1 — y (current_request_token_count) must be tools-inclusive, i.e. the same
    basis as baseline_tokens (x), so the proxy-savings comparison is apples-to-apples."""

    def test_includes_tool_tokens(self):
        from savings.calculator import count_messages_tokens, count_request_tokens
        tools = [{
            "type": "function",
            "function": {
                "name": "f", "description": "do a thing",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        }]
        ctx = _base_ctx(
            messages=[{"role": "user", "content": "hello there friend"}],
            original_messages=[{"role": "user", "content": "hello there friend"}],
            params={"tools": tools},
        )
        msgs_only = count_messages_tokens(ctx.messages, ctx.model)
        with_tools = count_request_tokens(ctx.messages, ctx.model, tools)
        assert ctx.current_request_token_count == with_tools
        assert ctx.current_request_token_count > msgs_only

    def test_equals_baseline_for_unoptimised_request(self):
        # Before any optimisation, y (current) must equal x (baseline) — both via
        # count_request_tokens on the same messages + tools.
        ctx = RequestContext.create(
            request_id="r", user_id="u",
            messages=[{"role": "user", "content": "what is the capital of france?"}],
            model="gpt-4o", params={}, config={},
        )
        assert ctx.current_request_token_count == ctx.savings.baseline_tokens
