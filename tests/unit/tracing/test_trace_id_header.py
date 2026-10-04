"""Every response to a pipeline request names its trace in an X-Trace-ID header.

tracing.propagate_trace_id_header (config/params/tracing.yaml.template, true there) was read by
nothing, so no response said which trace it was: a caller reporting a slow or failed request had
nothing to point at. The header is set when the response starts, from the request's pipeline span,
so a served answer, a stream and a refusal all carry it, on every protocol route. It is left out
when spans are not exported or the switch is off.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

import main  # noqa: E402
from middleware.g00_rate_limit import RateLimitExceeded  # noqa: E402
from middleware.g32_tool_eligibility import G32ToolEligibility  # noqa: E402
from tracing import otel  # noqa: E402

_CFG = {"groups": {}, "providers": []}


class _Pipeline:
    """Stands in for OptimisationPipeline: opens the pipeline span as it does, then does what
    `request_side(ctx)` says the request side did."""

    def __init__(self, request_side):
        self._request_side = request_side
        self.span = None
        self.g32 = G32ToolEligibility()

    async def process_request(self, ctx, request_headers=None):
        ctx.otel_span = self.span = otel.start_span("proxy-pipeline", ctx)
        self._request_side(ctx)
        return ctx

    async def process_response(self, ctx, response):
        return ctx, response


def _cache_hit(ctx):
    ctx.cache_hit = ctx.savings.cache_hit = True       # as G05 records an L1 hit
    ctx.cache_level = ctx.savings.cache_level = "L1"
    ctx.cache_response = {
        "id": "chatcmpl-cached", "object": "chat.completion", "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


def _rate_limited(ctx):
    raise RateLimitExceeded(retry_after=30, limit_type="rps", scope="acme")


async def _fake_auth(request, *_protocol):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "enterprise"}


@pytest.fixture
def tracing():
    """Spans exported to memory, with the `tracing` block's other keys as given."""
    saved = (otel._otel_available, otel._tracer, otel._provider,
             getattr(otel, "_propagate_header", False))

    def configure(**block):
        assert otel.configure({"tracing": {"enabled": True, **block}},
                              exporter=InMemorySpanExporter()) is True
    yield configure
    otel._otel_available, otel._tracer, otel._provider, otel._propagate_header = saved


def _post(monkeypatch, pipeline, path="/v1/chat/completions", **body):
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: _CFG)
    monkeypatch.setattr(main, "_pipeline", pipeline)
    monkeypatch.setattr(main, "_usage_meter", None)
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], **body}
    return TestClient(main.app).post(path, json=payload, headers={"Authorization": "Bearer tok-x"})


@pytest.mark.parametrize("request_side,body,status", [
    (_cache_hit, {}, 200),
    (_cache_hit, {"stream": True}, 200),
    (_rate_limited, {}, 429),
], ids=["served", "streamed", "refused"])
def test_the_response_names_the_requests_trace(monkeypatch, tracing, request_side, body, status):
    tracing()
    pipeline = _Pipeline(request_side)
    resp = _post(monkeypatch, pipeline, **body)
    assert resp.status_code == status
    assert len(resp.headers.get("x-trace-id", "")) == 32
    assert resp.headers["x-trace-id"] == otel.get_trace_id(pipeline.span)


def test_the_anthropic_route_names_it_too(monkeypatch, tracing):
    tracing()
    pipeline = _Pipeline(_cache_hit)
    resp = _post(monkeypatch, pipeline, path="/v1/messages", max_tokens=16)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-trace-id") == otel.get_trace_id(pipeline.span) != ""


def test_switched_off_the_header_is_left_out(monkeypatch, tracing):
    tracing(propagate_trace_id_header=False)
    resp = _post(monkeypatch, _Pipeline(_cache_hit))
    assert resp.status_code == 200
    assert "x-trace-id" not in resp.headers


def test_without_exported_spans_there_is_no_trace_to_name(monkeypatch, tracing):
    tracing()
    otel.configure({"tracing": {"enabled": False}})
    resp = _post(monkeypatch, _Pipeline(_cache_hit))
    assert resp.status_code == 200
    assert "x-trace-id" not in resp.headers


def test_a_request_outside_the_pipeline_names_none(tracing):
    tracing()
    resp = TestClient(main.app).get("/health")
    assert "x-trace-id" not in resp.headers


def test_a_trace_id_from_upstream_is_replaced_not_doubled(tracing):
    # A response that already carries an X-Trace-ID (an upstream's, passed through) ends up
    # with exactly one: this request's.
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    tracing()
    ctx = SimpleNamespace(request_id="r1", tenant_id="acme", model="m", otel_span=None)
    ctx.otel_span = otel.start_span("proxy-pipeline", ctx)
    app = FastAPI()

    @app.get("/x")
    async def passthrough(request: Request):
        request.state.pipeline_ctx = ctx
        return JSONResponse({}, headers={"X-Trace-ID": "0" * 32})

    app.add_middleware(otel.TraceIdHeaderMiddleware)
    resp = TestClient(app).get("/x")
    assert resp.headers.get_list("x-trace-id") == [otel.get_trace_id(ctx.otel_span)]
