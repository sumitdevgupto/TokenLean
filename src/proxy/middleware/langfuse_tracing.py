"""
Langfuse tracing helpers for the G1-G18 middleware pipeline.

Provides clean abstractions so individual middleware can emit spans
without importing the Langfuse SDK directly.
"""
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
_LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

_client = None
_client_init_attempted = False  # sentinel: True once we've tried (keys absent or init failed)


def get_client() -> Optional[Any]:
    """Return a cached Langfuse client, or None if keys are missing."""
    global _client, _client_init_attempted
    if _client is not None:
        return _client
    if _client_init_attempted:
        return None  # already know it's unavailable — skip repeated checks
    _client_init_attempted = True
    try:
        from langfuse import Langfuse
        if not _LANGFUSE_PUBLIC_KEY or not _LANGFUSE_SECRET_KEY:
            logger.debug("Langfuse keys not configured, skipping tracing")
            return None
        _client = Langfuse(
            host=_LANGFUSE_HOST,
            public_key=_LANGFUSE_PUBLIC_KEY,
            secret_key=_LANGFUSE_SECRET_KEY,
        )
        # Verify SDK is compatible — langfuse>=2.x uses .trace(), v3 uses different API
        if not hasattr(_client, "trace"):
            logger.warning("Langfuse SDK does not support .trace() — tracing disabled")
            _client = None
            return None
        return _client
    except Exception as exc:
        logger.warning("Langfuse client init failed: %s", exc)
        return None


def _should_capture_content(ctx: "RequestContext") -> bool:
    """Per-tenant toggle for writing raw prompt/response content into traces (I1).

    All tenants share one Langfuse project, so raw ``input``/``output`` would be
    visible to anyone with project access. Tenants that require content isolation
    set ``capture_trace_content: false`` under their G18_observability config
    (per-tenant override wins); token counts and savings metadata are still
    recorded. Default True keeps the current behaviour for existing deployments.
    """
    try:
        base = ctx.config.get("groups", {}).get("G18_observability", {})
        tenant_cfg = (
            ctx.config.get("tenants", {})
            .get(getattr(ctx, "tenant_id", "default"), {})
            .get("groups", {})
            .get("G18_observability", {})
        )
        return bool(tenant_cfg.get("capture_trace_content", base.get("capture_trace_content", True)))
    except Exception:
        return True


def start_trace(ctx: "RequestContext") -> Optional[Any]:
    """Create a Langfuse trace at the beginning of a request and store it on ctx."""
    cfg = ctx.config.get("groups", {}).get("G18_observability", {})
    if not cfg.get("enabled", False):
        return None

    client = get_client()
    if not client:
        return None

    try:
        session_id = (
            ctx.params.get("workflow_id")
            or ctx.params.get("x_workflow_id")
            or ctx.params.get("session_id")
            or ctx.params.get("x_session_id")
        )
        trace = client.trace(
            id=ctx.request_id,
            name="llm-proxy-call",
            user_id=ctx.user_id,
            metadata={
                "model_requested": ctx.model,
                "baseline_tokens": ctx.savings.baseline_tokens,
                "user_id": ctx.user_id,
                "tenant_id": ctx.tenant_id,
            },
            tags=["token-optimisation"],
            session_id=session_id,
        )
        ctx.langfuse_trace = trace
        return trace
    except Exception as exc:
        logger.warning("Langfuse trace creation failed: %s", exc)
        return None


def add_span(
    ctx: "RequestContext",
    name: str,
    span_input: Any = None,
    output: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Add a child span to the current trace."""
    trace = getattr(ctx, "langfuse_trace", None)
    if not trace:
        return None
    # I1: redact raw payloads when this tenant opts out of content capture.
    if not _should_capture_content(ctx):
        span_input = None
        output = None
    try:
        return trace.span(
            name=name,
            input=span_input,
            output=output,
            metadata=metadata or {},
            usage=usage,
        )
    except Exception as exc:
        logger.warning("Langfuse span creation failed (%s): %s", name, exc)
        return None


def add_score(
    ctx: "RequestContext",
    name: str,
    value: float,
    comment: Optional[str] = None,
) -> None:
    """Add a score to the current trace."""
    trace = getattr(ctx, "langfuse_trace", None)
    if not trace:
        return
    try:
        trace.score(name=name, value=value, comment=comment)
    except Exception as exc:
        logger.warning("Langfuse score creation failed (%s): %s", name, exc)


def finish_trace(ctx: "RequestContext", response: Optional[Dict[str, Any]] = None) -> None:
    """Add the final LLM generation and flush the trace."""
    trace = getattr(ctx, "langfuse_trace", None)
    if not trace:
        return

    try:
        usage: Optional[Dict[str, Any]] = None
        output_text = ""
        if response:
            usage_data = response.get("usage", {})
            prompt_tokens = usage_data.get("prompt_tokens", ctx.savings.final_tokens_sent)
            output_tokens = usage_data.get("completion_tokens", 0)
            total_tokens = usage_data.get("total_tokens", prompt_tokens + output_tokens)
            usage = {
                "input": prompt_tokens,
                "output": output_tokens,
                "total": total_tokens,
                "unit": "TOKENS",
            }
            choices = response.get("choices", [{}])
            if choices:
                output_text = choices[0].get("message", {}).get("content", "")

        metadata = ctx.savings.to_langfuse_metadata()

        # I1: omit raw prompt/response for tenants that opt out of content capture
        # (shared Langfuse project → no per-tenant RBAC on stored content).
        if _should_capture_content(ctx):
            gen_input, gen_output = ctx.messages, output_text
        else:
            gen_input = gen_output = "[redacted: capture_trace_content=false]"

        trace.generation(
            name="llm-completion",
            model=ctx.routed_model,
            input=gen_input,
            output=gen_output,
            usage=usage,
            metadata=metadata,
        )

        # Write full savings metadata to the trace so Grafana SQL queries on
        # traces.metadata can find total_abs_saving, cost_saving_usd,
        # total_pct_saving, and step_savings. Also re-affirms identity fields
        # in case start_trace ran before tenant resolution.
        # Spread savings metadata first, then re-affirm live identity fields last
        # so ctx.user_id / ctx.tenant_id always win over any stale snapshot in
        # metadata (and in case start_trace ran before tenant resolution).
        trace_meta = {
            **metadata,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
        }
        # Record the RAG collection (set from the X-Rag-Collection header) so
        # dashboards can classify RAG / Doc-Q&A requests. Without this the
        # pitch "Medium — Doc Q&A" panel can never match a request.
        rag_collection = ctx.params.get("x_rag_collection") or ctx.params.get("rag_collection")
        if rag_collection:
            trace_meta["rag_collection"] = rag_collection
            trace_meta["x_rag_collection"] = rag_collection
        trace.update(metadata=trace_meta)

        _add_scores(ctx, trace)

        client = get_client()
        if client:
            client.flush()
    except Exception as exc:
        logger.warning("Langfuse finish_trace failed: %s", exc)


def _add_scores(ctx: "RequestContext", trace: Any) -> None:
    """Attach evaluation scores to the trace."""
    # Token savings percentage
    try:
        trace.score(name="savings_pct", value=float(ctx.savings.total_pct_saving))
    except Exception as exc:
        logger.debug("Langfuse score savings_pct failed: %s", exc)

    # Cache hit rate (binary per request)
    try:
        trace.score(name="cache_hit_rate", value=1.0 if ctx.savings.cache_hit else 0.0)
    except Exception as exc:
        logger.debug("Langfuse score cache_hit_rate failed: %s", exc)

    # Routing confidence (when RouteLLM or cascade is used)
    if ctx.savings.routellm_confidence is not None:
        try:
            trace.score(name="routing_confidence", value=float(ctx.savings.routellm_confidence))
        except Exception as exc:
            logger.debug("Langfuse score routing_confidence failed: %s", exc)

    # Turn efficiency (higher turn count = lower score)
    workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id")
    if workflow_id:
        try:
            turn_count = ctx.params.get("_token_budget", {}).get("workflow_turn", 0)
            if turn_count <= 3:
                efficiency = 1.0
            elif turn_count <= 6:
                efficiency = 0.5
            else:
                efficiency = 0.0
            trace.score(
                name="turn_efficiency",
                value=efficiency,
                comment=f"turn_count={turn_count}",
            )
        except Exception as exc:
            logger.debug("Langfuse score turn_efficiency failed: %s", exc)

    # Compression ratio (when G1 is active)
    g1_step = next((s for s in ctx.savings.step_savings if s.group == "G01"), None)
    if g1_step and g1_step.tokens_before > 0:
        try:
            ratio = 1.0 - (g1_step.tokens_after / g1_step.tokens_before)
            trace.score(name="compression_ratio", value=round(ratio, 2))
        except Exception as exc:
            logger.debug("Langfuse score compression_ratio failed: %s", exc)
