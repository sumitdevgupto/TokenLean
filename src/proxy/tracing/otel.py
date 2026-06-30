"""
OpenTelemetry tracing for the Token Optimisation proxy pipeline.

Every middleware stage in pipeline.py wraps its call with start_span / end_span
so the full G0→G22 execution graph is visible in Jaeger.  The Langfuse layer
(langfuse_tracing.py) handles LLM-specific spans; this layer covers the proxy
pipeline graph.

Exports via OTLP gRPC to Jaeger (or any OTLP collector) using the standard
OTEL_EXPORTER_OTLP_ENDPOINT environment variable (default: http://jaeger:4317).

OSS stack: opentelemetry-sdk (Apache-2), opentelemetry-exporter-otlp (Apache-2).
Gracefully no-ops when opentelemetry is not installed so unit tests run without
the SDK dependency.
"""
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Optional OTel import (graceful no-op if SDK not installed) ────────────────
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.trace import StatusCode

    _OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    _provider = TracerProvider()
    _exporter = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT, insecure=True)
    _provider.add_span_processor(BatchSpanProcessor(_exporter))
    trace.set_tracer_provider(_provider)
    _tracer = trace.get_tracer("token-opt-proxy", schema_url="https://opentelemetry.io/schemas/1.11.0")
    _otel_available = True
    logger.info("OTel tracing enabled → %s", _OTLP_ENDPOINT)
except Exception:  # ImportError or connection error at import time
    _otel_available = False
    _tracer = None
    logger.debug("OTel SDK not available — tracing is a no-op")


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
    """Start an OTel span for a pipeline stage and attach it to ``ctx``.

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
        span = _tracer.start_span(name)
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
                from opentelemetry.trace import StatusCode
                span.set_status(StatusCode.ERROR, str(error))
        elif _otel_available:
            from opentelemetry.trace import StatusCode
            span.set_status(StatusCode.OK)
        span.end()
    except Exception as exc:
        logger.debug("OTel end_span failed: %s", exc)


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
