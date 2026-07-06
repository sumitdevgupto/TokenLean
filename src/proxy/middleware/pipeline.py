import logging
import os
import time
from typing import Any, Awaitable, Dict, Optional, Tuple

from middleware import RequestContext
from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded
from providers import get_adapter
from tenancy.resolver import resolve_tenant
from tenancy.config import TenantConfigLoader
from tracing import otel
from middleware.g01_compression import G01Compression
from middleware.g02_template_registry import G02TemplateRegistry
from middleware.g04_bypass import G04Bypass
from middleware.g05_cache import G05Cache
from middleware.g06_routing import G06Routing
from middleware.g07_retrieval import G07Retrieval
from middleware.g08_tool_loading import G08ToolLoading
from middleware.g09_context_schema import G09ContextSchema
from middleware.g10_memory import G10Memory
from middleware.g11_output_format import G11OutputFormat
from middleware.g12_reasoning_budget import G12ReasoningBudget
from middleware.g13_batch import G13Batch
from middleware.g14_tool_output import G14ToolOutput
from middleware.g15_server_compute import G15ServerCompute
from middleware.g16_agent_arch import G16AgentArch
from middleware.g17_loop_control import G17LoopControl
from middleware.g18_observability import G18Observability, STAGE_DURATION_MS
from middleware.g19_headroom import G19Headroom
from middleware.g20_prompt_optimizer import G20PromptOptimizer
from middleware.g21_cache_alignment import G21CacheAlignment
from middleware.g22_deduplication import G22Deduplication
from middleware.g23_streaming_compression import G23StreamingCompression
from middleware.g24_adaptive_bypass import G24AdaptiveBypass
from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
from middleware.g27_multimodal_optimizer import G27MultimodalOptimizer
from middleware.g28_ccr import G28CCR
from middleware import langfuse_tracing

logger = logging.getLogger(__name__)

# Per-stage timing: any stage slower than this (ms) is logged at WARNING so a
# hanging/slow stage is obvious in `docker logs`. Tunable via env without rebuild.
_SLOW_STAGE_MS = float(os.getenv("PIPELINE_SLOW_STAGE_MS", "1000"))


class OptimisationPipeline:
    """
    Orchestrates the full G0–G28 token optimisation pipeline.

    Request path:  G0 → G24 → G4 → G5 → G6 → G1 → G27 → G2 → G20 → G7 → G8 → G28 → G19 → G9 → G10 → G22 → G16 → G11 → G25 → G12 → G13 → G17 → G21
    Response path: G14 → G23 → G19 → G15 → G11(feedback) → G18
    """

    def __init__(self, db_pool=None) -> None:
        self._tenant_config_loader = TenantConfigLoader(db_pool=db_pool)
        self.g00 = G00RateLimit()
        self.g01 = G01Compression()
        self.g02 = G02TemplateRegistry()
        self.g04 = G04Bypass()
        self.g05 = G05Cache()
        self.g06 = G06Routing()
        self.g07 = G07Retrieval()
        self.g08 = G08ToolLoading()
        self.g09 = G09ContextSchema()
        self.g10 = G10Memory()
        self.g11 = G11OutputFormat()
        self.g12 = G12ReasoningBudget()
        self.g13 = G13Batch()
        self.g14 = G14ToolOutput()
        self.g15 = G15ServerCompute()
        self.g16 = G16AgentArch()
        self.g17 = G17LoopControl()
        self.g18 = G18Observability()
        self.g19 = G19Headroom()
        self.g20 = G20PromptOptimizer()
        self.g21 = G21CacheAlignment()
        self.g22 = G22Deduplication()
        self.g23 = G23StreamingCompression()
        self.g24 = G24AdaptiveBypass()
        self.g25 = G25AdaptiveReasoning()
        self.g27 = G27MultimodalOptimizer()
        self.g28 = G28CCR()

    async def _run_timed(self, name: str, ctx: RequestContext, coro: Awaitable[Any]) -> Any:
        """Await `coro` inside an OTel span while measuring wall-clock time.

        Logs every stage's duration at DEBUG, and at WARNING when it exceeds
        `_SLOW_STAGE_MS` — this is what surfaces a hanging stage (e.g. a stalled
        sidecar / vector-DB call) in `docker logs token-opt-proxy`.
        """
        _s = otel.start_span(name, ctx)
        _t0 = time.perf_counter()
        try:
            return await coro
        finally:
            _dt = (time.perf_counter() - _t0) * 1000.0
            rid = getattr(ctx, "request_id", "?")
            if _dt > _SLOW_STAGE_MS:
                logger.warning("[%s] STAGE %s SLOW: %.0fms", rid, name, _dt)
            else:
                logger.debug("[%s] stage %s %.0fms", rid, name, _dt)
            try:
                STAGE_DURATION_MS.labels(
                    stage=name, tenant_id=getattr(ctx, "tenant_id", "default")
                ).observe(_dt)
            except Exception:  # never let metrics break the pipeline
                pass
            otel.end_span(_s)

    async def process_request(self, ctx: RequestContext, request_headers: Optional[Dict[str, str]] = None) -> RequestContext:
        """Run request through the pre-LLM optimisation pipeline."""
        # ── Tenant injection (before any middleware) ────────────────────────
        # The authenticated key is authoritative: main.py stashes the key's
        # tenant/tier/admin scope into ctx.params as _auth_*. The X-Tenant-ID
        # header is honoured only for admin keys (resolve_tenant enforces this).
        headers = {k.lower(): v for k, v in (request_headers or {}).items()}
        key_tenant_id = ctx.params.get("_auth_tenant_id")
        key_tier = ctx.params.get("_auth_tier", "basic")
        key_is_admin = bool(ctx.params.get("_auth_admin", False))
        tenant = resolve_tenant(
            headers,
            key_tenant_id=key_tenant_id,
            key_tier=key_tier,
            key_is_admin=key_is_admin,
            api_key_hash=ctx.params.get("_api_key_hash"),
        )
        ctx.tenant_id = tenant.tenant_id
        ctx.redis_prefix = tenant.redis_prefix
        ctx.qdrant_collection = tenant.qdrant_collection
        ctx.pricing_tier = tenant.pricing_tier
        ctx.is_admin_key = key_is_admin
        # I6: record actor→target when an admin key impersonates another tenant
        header_tenant = headers.get("x-tenant-id", "").strip()
        if key_is_admin and header_tenant and header_tenant != (key_tenant_id or ""):
            ctx.impersonator_tenant_id = key_tenant_id or "unknown"
            # Always log impersonation as a security event (independent of the
            # audit-config toggle); the audit row (I6) adds the durable record.
            logger.warning(
                "[%s] IMPERSONATION: admin key (tenant=%s) acting as tenant=%s",
                ctx.request_id, ctx.impersonator_tenant_id, ctx.tenant_id,
            )
        # Merge per-tenant config overrides (shallow merge at groups level)
        if tenant.config_overrides:
            import copy
            merged = copy.deepcopy(ctx.config)
            for k, v in tenant.config_overrides.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
            ctx.config = merged

        # Load per-tenant config overrides from Postgres (E3)
        await self._tenant_config_loader.load(ctx)

        # ── Provider adapter (resolves once per request based on routed model) ─
        ctx.provider_adapter = get_adapter(
            ctx.routed_model,
            ctx.config.get("providers", []),
        )

        # ── Start pipeline-level OTel span ──────────────────────────────────
        pipeline_span = otel.start_span("proxy-pipeline", ctx)
        ctx.otel_span = pipeline_span

        # Start Langfuse trace before any middleware runs
        langfuse_tracing.start_trace(ctx)

        # Stage 1 — At the Gate (Rate Limiting)
        ctx = await self._run_timed("G00-rate-limit", ctx, self.g00.process_request(ctx))

        # Stage 1b — Adaptive Bypass (loads skip_groups from rules)
        ctx = await self._run_timed("G24-adaptive-bypass", ctx, self.g24.process_request(ctx))

        # Stage 2 — At the Gate
        for _name, _mid in [("G04-bypass", self.g04), ("G05-cache", self.g05)]:
            ctx = await self._run_timed(_name, ctx, _mid.process_request(ctx))
            if ctx.bypassed:
                logger.debug("[%s] G04 bypassed LLM call", ctx.request_id)
                otel.end_span(pipeline_span)
                return ctx
            if ctx.cache_hit:
                logger.debug("[%s] G05 cache hit (%s)", ctx.request_id, ctx.cache_level)
                otel.end_span(pipeline_span)
                return ctx

        ctx = await self._run_timed("G06-routing", ctx, self.g06.process_request(ctx))

        # Stage 3 — Into the LLM
        for _name, _mid, _group in [
            ("G01-compression", self.g01, "G01"),
            ("G27-multimodal-optimizer", self.g27, "G27"),
            ("G02-template-registry", self.g02, "G02"),
            ("G20-prompt-optimizer", self.g20, "G20"),
            ("G07-retrieval", self.g07, "G07"),
            ("G08-tool-loading", self.g08, "G08"),
            ("G28-ccr", self.g28, "G28"),
            ("G19-headroom", self.g19, "G19"),
            ("G09-context-schema", self.g09, "G09"),
            ("G10-memory", self.g10, "G10"),
            ("G22-deduplication", self.g22, "G22"),
        ]:
            if _group in ctx.skip_groups:
                logger.debug("[%s] G24 skipping %s", ctx.request_id, _group)
                continue
            ctx = await self._run_timed(_name, ctx, _mid.process_request(ctx))

        # Stage 4 — Inside the LLM (architecture advisory + parameter injection)
        for _name, _mid, _group in [
            ("G16-agent-arch", self.g16, "G16"),
            ("G11-output-format", self.g11, "G11"),
            ("G25-adaptive-reasoning", self.g25, "G25"),
            ("G12-reasoning-budget", self.g12, "G12"),
            ("G13-batch", self.g13, "G13"),
            ("G17-loop-control", self.g17, "G17"),
        ]:
            if _group in ctx.skip_groups:
                logger.debug("[%s] G24 skipping %s", ctx.request_id, _group)
                continue
            ctx = await self._run_timed(_name, ctx, _mid.process_request(ctx))

        # Stage 5 — Final alignment for provider prompt caching
        ctx = await self._run_timed("G21-cache-alignment", ctx, self.g21.process_request(ctx))

        # B1: record the proxy's optimised token estimate (y) before the LLM call,
        # tools-symmetric with the baseline (x). G18 later overwrites final_tokens_sent
        # with the provider's actual prompt_tokens (z); until then the estimate stands.
        ctx.savings.proxy_optimised_tokens = ctx.current_request_token_count
        ctx.savings.final_tokens_sent = ctx.savings.proxy_optimised_tokens
        otel.end_span(pipeline_span)
        return ctx

    async def process_response(
        self,
        ctx: RequestContext,
        response: Dict[str, Any],
    ) -> Tuple[RequestContext, Dict[str, Any]]:
        """Run response through the post-LLM optimisation pipeline."""
        # Stage 5 — After the Response
        for _name, _fn in [
            ("G14-tool-output", lambda r: self.g14.process_response(ctx, r)),
            ("G28-ccr-resp", lambda r: self.g28.process_response(ctx, r)),
            ("G23-streaming-compression", lambda r: self.g23.process_response(ctx, r)),
            ("G19-headroom-resp", lambda r: self.g19.process_response(ctx, r)),
            ("G15-server-compute", lambda r: self.g15.process_response(ctx, r)),
        ]:
            response = await self._run_timed(_name, ctx, _fn(response))

        # Stage 5b — G11 max_tokens feedback loop (must run after response received)
        ctx, response = await self._run_timed(
            "G11-output-format-resp", ctx, self.g11.process_response(ctx, response)
        )

        # Stage 6 — Across the Loop (observability)
        await self._run_timed("G18-observability", ctx, self.g18.record(ctx, response))

        # Store result in G5 cache
        await self._run_timed("G05-store-response", ctx, self.g05.store_response(ctx, response))

        return ctx, response
