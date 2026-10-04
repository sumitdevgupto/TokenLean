"""
OpenTelemetry tracing for the Token Optimisation proxy pipeline.

Every middleware stage in pipeline.py wraps its call with start_span / end_span
so the full G0→G32 execution graph is visible in Jaeger.  The Langfuse layer
(langfuse_tracing.py) handles LLM-specific spans; this layer covers the proxy
pipeline graph.

Exports via OTLP gRPC to Jaeger (or any OTLP collector), set up once at startup by
``configure`` from the ``tracing`` config block: ``enabled``, ``otlp_endpoint``,
``sample_rate`` and ``service_name``. The OTEL_EXPORTER_OTLP_ENDPOINT environment variable
wins for the endpoint, and with no ``enabled`` set it is what turns export on. Until then,
and whenever it is off, every span is a no-op. Each stage span is a child of the request's
pipeline span (``ctx.otel_span``), so a request is one trace, and ``TraceIdHeaderMiddleware``
names it on the response as X-Trace-ID (``propagate_trace_id_header``, on by default).

OSS stack: opentelemetry-sdk (Apache-2), opentelemetry-exporter-otlp (Apache-2).
Gracefully no-ops when opentelemetry is not installed so unit tests run without
the SDK dependency.
"""
import logging
import os
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# ── Optional OTel import (graceful no-op if SDK not installed) ────────────────
try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode
    _sdk_available = True
except Exception:  # ImportError
    trace = None
    _sdk_available = False

_DEFAULT_ENDPOINT = "http://jaeger:4317"
_otel_available = False          # True once configure() has set up an exporter
_tracer = None
_provider = None
_propagate_header = False        # tracing.propagate_trace_id_header, while spans are exported
TRACE_ID_HEADER = b"x-trace-id"


def _settings(config: Any, env: Mapping[str, str] = os.environ) -> Dict[str, Any]:
    """What ``configure`` sets up, from the ``tracing`` block and the environment.

    Spans are exported when ``tracing.enabled`` says so, or, with ``enabled`` unset, when
    OTEL_EXPORTER_OTLP_ENDPOINT names a collector (docker-compose sets it). A deploy with
    neither exports nothing: it used to send every span to jaeger:4317, which is not there,
    and retry. The variable also wins over ``otlp_endpoint``, as the template documents. TLS
    is used unless the endpoint is plain ``http://``."""
    block = config.get("tracing") if isinstance(config, dict) else None
    block = block if isinstance(block, dict) else {}
    env_endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    enabled = block.get("enabled")
    endpoint = env_endpoint or str(block.get("otlp_endpoint") or _DEFAULT_ENDPOINT)
    try:
        rate = min(1.0, max(0.0, float(block.get("sample_rate", 1.0))))
    except (TypeError, ValueError):
        rate = 1.0
    return {"enabled": bool(env_endpoint) if enabled is None else bool(enabled),
            "endpoint": endpoint, "insecure": endpoint.startswith("http://"),
            "sample_rate": rate,
            "service_name": str(block.get("service_name") or "token-opt-proxy"),
            "propagate_trace_id_header": bool(block.get("propagate_trace_id_header", True))}


def configure(config: Any, exporter: Any = None) -> bool:
    """Set up pipeline tracing from the proxy config (once, at startup; a change applies at
    the next start). ``exporter`` replaces the OTLP exporter (tests). Returns whether spans
    are now exported."""
    global _otel_available, _tracer, _provider, _propagate_header
    s = _settings(config)
    if not (_sdk_available and s["enabled"]):
        _otel_available, _tracer, _provider = False, None, None
        logger.info("OTel pipeline tracing is off%s", "" if _sdk_available else
                    " (opentelemetry is not installed)")
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            processor = BatchSpanProcessor(
                OTLPSpanExporter(endpoint=s["endpoint"], insecure=s["insecure"]))
        else:
            processor = SimpleSpanProcessor(exporter)
        provider = TracerProvider(
            resource=Resource.create({"service.name": s["service_name"]}),
            sampler=ParentBased(TraceIdRatioBased(s["sample_rate"])))
        provider.add_span_processor(processor)
        _provider = provider
        _tracer = provider.get_tracer("token-opt-proxy",
                                      schema_url="https://opentelemetry.io/schemas/1.11.0")
        _otel_available = True
        _propagate_header = s["propagate_trace_id_header"]
        logger.info("OTel pipeline tracing → %s (sample rate %s)", s["endpoint"],
                    s["sample_rate"])
        return True
    except Exception as exc:
        _otel_available, _tracer, _provider = False, None, None
        logger.warning("OTel pipeline tracing could not start, so no spans are exported: %s",
                       exc)
        return False


class _NoOpSpan:
    """Returned when OTel is not available so callers need no None checks."""
    trace_id: int = 0

    def set_attribute(self, *_a: Any, **_kw: Any) -> None:  # noqa: D401
        pass

    def set_status(self, *_a: Any, **_kw: Any) -> None:
        pass

    def record_exception(self, *_a: Any, **_kw: Any) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def start_span(name: str, ctx: Any) -> Any:
    """Start an OTel span for a pipeline stage, as a child of the request's pipeline span
    (``ctx.otel_span``) when it has one.

    Args:
        name: Human-readable stage name, e.g. ``"G01-compression"``.
        ctx:  ``RequestContext`` — span attributes ``request_id`` and
              ``tenant_id`` are read from it.

    Returns:
        The OTel span (or a no-op span if OTel is unavailable).
    """
    if not _otel_available or _tracer is None:
        return _NoOpSpan()

    try:
        parent = getattr(ctx, "otel_span", None)
        context = (trace.set_span_in_context(parent)
                   if parent is not None and not isinstance(parent, _NoOpSpan) else None)
        span = _tracer.start_span(name, context=context)
        span.set_attribute("proxy.request_id", getattr(ctx, "request_id", ""))
        span.set_attribute("proxy.tenant_id", getattr(ctx, "tenant_id", "default"))
        span.set_attribute("proxy.model", getattr(ctx, "model", ""))
        return span
    except Exception as exc:
        logger.debug("OTel start_span failed: %s", exc)
        return _NoOpSpan()


def end_span(span: Any, *, error: Optional[Exception] = None) -> None:
    """End an OTel span, optionally recording an error.

    Args:
        span:  Span returned by ``start_span``.
        error: If provided, records the exception and sets ERROR status.
    """
    if span is None:
        return
    try:
        if error is not None:
            span.record_exception(error)
            if _otel_available:
                span.set_status(StatusCode.ERROR, str(error))
        elif _otel_available:
            span.set_status(StatusCode.OK)
        span.end()
    except Exception as exc:
        logger.debug("OTel end_span failed: %s", exc)


def response_trace_id(ctx: Any) -> str:
    """The trace id a response names in X-Trace-ID: its request's pipeline span
    (``ctx.otel_span``), while spans are exported and ``tracing.propagate_trace_id_header`` is
    on (the default). Empty otherwise, and the header is left out."""
    if not (_otel_available and _propagate_header):
        return ""
    return get_trace_id(getattr(ctx, "otel_span", None))


class TraceIdHeaderMiddleware:
    """ASGI middleware: put the request's trace id on its response as X-Trace-ID. The request
    handler leaves its RequestContext on ``request.state.pipeline_ctx``; the id is read when
    the response starts, by when the pipeline span exists, so every response to a pipeline
    request carries it: served, streamed or refused, on every protocol route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})

        async def send_with_trace_id(message):
            if message.get("type") == "http.response.start":
                trace_id = response_trace_id(state.get("pipeline_ctx"))
                if trace_id:
                    headers = [(k, v) for k, v in message.get("headers") or []
                               if k.lower() != TRACE_ID_HEADER]
                    message = dict(message, headers=[*headers, (TRACE_ID_HEADER,
                                                                trace_id.encode("latin-1"))])
            await send(message)

        await self.app(scope, receive, send_with_trace_id)


def get_trace_id(span: Any) -> str:
    """Return the W3C trace-id hex string for a span (empty string if unavailable)."""
    if span is None or isinstance(span, _NoOpSpan):
        return ""
    try:
        ctx = span.get_span_context()
        tid = ctx.trace_id
        if tid == 0:
            return ""
        return format(tid, "032x")
    except Exception:
        return ""
