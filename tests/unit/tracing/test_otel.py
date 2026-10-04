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


# ── Configured from the `tracing` block at startup; stages under the pipeline span ──
# Until 2026-10-02 the exporter was built at import from the environment alone (always on,
# jaeger:4317 by default, insecure even for https), `tracing.*` was read by nothing, and
# every stage span was a trace of its own.

@pytest.fixture
def otel_state():
    from tracing import otel as otel_mod
    saved = (otel_mod._otel_available, otel_mod._tracer, otel_mod._provider,
             getattr(otel_mod, "_propagate_header", False))
    yield otel_mod
    (otel_mod._otel_available, otel_mod._tracer, otel_mod._provider,
     otel_mod._propagate_header) = saved


@pytest.mark.parametrize("config,env,enabled", [
    ({}, {}, False),                                                  # nothing names a collector
    ({}, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4317"}, True),  # docker-compose sets it
    ({"tracing": {"enabled": True}}, {}, True),
    ({"tracing": {"enabled": False}}, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://j:4317"}, False),
])
def test_when_spans_are_exported(config, env, enabled):
    from tracing.otel import _settings
    assert _settings(config, env)["enabled"] is enabled


def test_the_endpoint_env_var_wins_and_https_gets_tls():
    from tracing.otel import _settings
    block = {"tracing": {"enabled": True, "otlp_endpoint": "http://collector:4317"}}
    s = _settings(block, {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example:4317"})
    assert (s["endpoint"], s["insecure"]) == ("https://otlp.example:4317", False)
    s = _settings(block, {})
    assert (s["endpoint"], s["insecure"]) == ("http://collector:4317", True)


@pytest.mark.parametrize("rate,expected", [(0.25, 0.25), (3, 1.0), (-1, 0.0), ("often", 1.0)])
def test_the_sample_rate_is_read_and_bounded(rate, expected):
    from tracing.otel import _settings
    assert _settings({"tracing": {"sample_rate": rate}}, {})["sample_rate"] == expected


def test_switched_off_every_span_is_a_no_op(otel_state):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    # An exporter that would work, so only the switch can turn tracing off.
    assert otel_state.configure({"tracing": {"enabled": False}},
                                exporter=InMemorySpanExporter()) is False
    assert isinstance(otel_state.start_span("G01", FakeCtx()), otel_state._NoOpSpan)


def _memory_tracing(otel_mod, **block):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    memory = InMemorySpanExporter()
    assert otel_mod.configure({"tracing": {"enabled": True, **block}}, exporter=memory) is True
    return memory


def test_stage_spans_belong_to_the_pipeline_trace(otel_state):
    pytest.importorskip("opentelemetry.sdk.trace")
    memory = _memory_tracing(otel_state)
    ctx = FakeCtx()
    pipeline = otel_state.start_span("proxy-pipeline", ctx)
    ctx.otel_span = pipeline
    try:
        stage = otel_state.start_span("G01-compression", ctx)
        otel_state.end_span(stage)
        otel_state.end_span(pipeline)
    finally:
        ctx.otel_span = None
    spans = {s.name: s for s in memory.get_finished_spans()}
    assert spans["G01-compression"].parent is not None, "the stage span is a trace of its own"
    assert spans["G01-compression"].parent.span_id == spans["proxy-pipeline"].context.span_id
    assert spans["G01-compression"].context.trace_id == spans["proxy-pipeline"].context.trace_id
    assert otel_state.get_trace_id(pipeline) == format(spans["G01-compression"].context.trace_id,
                                                       "032x")
    assert spans["proxy-pipeline"].resource.attributes["service.name"] == "token-opt-proxy"


def test_a_zero_sample_rate_exports_nothing(otel_state):
    pytest.importorskip("opentelemetry.sdk.trace")
    memory = _memory_tracing(otel_state, sample_rate=0, service_name="proxy-eu")
    otel_state.end_span(otel_state.start_span("proxy-pipeline", FakeCtx()))
    assert memory.get_finished_spans() == ()


@pytest.mark.parametrize("block,propagate", [
    ({}, True),                                        # the template's value, and the default
    ({"propagate_trace_id_header": True}, True),
    ({"propagate_trace_id_header": False}, False),
])
def test_the_trace_id_header_switch_is_read(block, propagate):
    from tracing.otel import _settings
    assert _settings({"tracing": block}, {})["propagate_trace_id_header"] is propagate


def test_a_response_names_the_requests_trace_only_while_spans_are_exported(otel_state):
    pytest.importorskip("opentelemetry.sdk.trace")
    ctx = FakeCtx()
    _memory_tracing(otel_state)
    ctx.otel_span = otel_state.start_span("proxy-pipeline", ctx)
    try:
        assert otel_state.response_trace_id(ctx) == otel_state.get_trace_id(ctx.otel_span) != ""
        otel_state.configure({"tracing": {"enabled": False}})          # export off, switch on
        assert otel_state.response_trace_id(ctx) == ""
        _memory_tracing(otel_state, propagate_trace_id_header=False)   # export on, switch off
        assert otel_state.response_trace_id(ctx) == ""
    finally:
        ctx.otel_span = None
    assert otel_state.response_trace_id(None) == ""    # a request that never reached the pipeline


def test_the_proxy_configures_tracing_at_startup():
    import ast
    import inspect
    import textwrap
    import main
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.lifespan)))
    calls = {ast.unparse(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    assert any(c.endswith("otel.configure") for c in calls)
