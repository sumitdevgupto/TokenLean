"""The OpenAI endpoint copies only TokenLean's own X-* headers into ctx.params.

ctx.params is persisted (G13 writes it to the Redis batch stream), so infrastructure headers
and a caller's own routing hints must not ride along; the pipeline still receives every
header, and the routing rules see them through ctx.routing_headers.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class _Pipeline:
    """Records what the endpoint hands the pipeline, then serves a cache hit."""

    def __init__(self):
        self.params, self.headers = None, None

    async def process_request(self, ctx, request_headers=None):
        self.params, self.headers = dict(ctx.params), dict(request_headers or {})
        ctx.cache_hit = ctx.savings.cache_hit = True
        ctx.cache_level = ctx.savings.cache_level = "L1"
        ctx.cache_response = {
            "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        return ctx

    async def process_response(self, ctx, response):
        return ctx, response


async def _fake_auth(request):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "enterprise"}


@pytest.fixture
def pipeline(monkeypatch):
    fake = _Pipeline()
    monkeypatch.setattr(main, "_usage_meter", None)
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: {"groups": {}, "providers": []})
    monkeypatch.setattr(main, "_pipeline", fake)
    return fake


def test_only_tokenlean_headers_reach_ctx_params(pipeline):
    r = TestClient(main.app).post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer tok-x", "X-Template-ID": "t1",
                 "X-Cloud-Trace-Context": "abc/1;o=1", "X-Customer-Tier": "gold"})
    assert r.status_code == 200, r.text
    assert pipeline.params["x_template_id"] == "t1"
    assert "x_cloud_trace_context" not in pipeline.params
    assert "x_customer_tier" not in pipeline.params
    assert pipeline.headers["x-customer-tier"] == "gold"     # the rules still get it
