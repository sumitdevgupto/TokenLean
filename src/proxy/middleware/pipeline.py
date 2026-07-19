import logging
import os
import time
from typing import Any, Awaitable, Dict, Optional, Tuple

from middleware import RequestContext
from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded
from providers import get_adapter
from tenancy.resolver import resolve_tenant
from tenancy.config import TenantConfigLoader, deep_merge
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
from middleware.intent_orchestration import IntentOrchestration
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
from middleware.g29_pii_redaction import G29PiiRedaction
from middleware.g30_guardrails import G30Guardrails
from middleware.g31_context_trust import G31ContextTrust
from middleware import langfuse_tracing

logger = logging.getLogger(__name__)

# Per-stage timing: any stage slower than this (ms) is logged at WARNING so a
# hanging/slow stage is obvious in `docker logs`. Tunable via env without rebuild.
_SLOW_STAGE_MS = float(os.getenv("PIPELINE_SLOW_STAGE_MS", "1000"))


class OptimisationPipeline:
    """
    Orchestrates the full G0–G28 token optimisation pipeline.

    Request path:  G0 → G24 → G30 → G29 → G4 → G5 → G6 → G1 → G27 → G2 → G20 → G7 → G8 → G28 → G19 → G9 → G10 → G22 → G31 → G16 → G11 → G25 → G12 → G13 → G17 → G21
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
        self.f2 = IntentOrchestration()  # F2 intent-based downstream-agent dispatch
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
        self.g29 = G29PiiRedaction()
        self.g30 = G30Guardrails()
        self.g31 = G31ContextTrust()

    def set_db_pool(self, pool) -> None:
        """Inject the asyncpg pool into the tenant-config loader after startup.

        D1 fix: this module-level pipeline is constructed with db_pool=None, so without
        this call TenantConfigLoader.load() no-ops and per-tenant config_overrides
        (model prefs, G-group knobs) are persisted but NEVER applied at runtime.
        """
        self._tenant_config_loader.set_db_pool(pool)

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
        key_tier = ctx.params.get("_auth_tier", "free")
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
        # Deprecated Mechanism C: TenantContext.config_overrides is never populated by
        # resolve_tenant in production (always empty). Kept as a harmless no-op merge for
        # back-compat; the live per-tenant path is TenantConfigLoader (Postgres) below.
        if tenant.config_overrides:
            import copy
            ctx.config = deep_merge(copy.deepcopy(ctx.config), tenant.config_overrides)

        # Load per-tenant config overrides from Postgres (E3) — the LIVE mechanism (D1).
        await self._tenant_config_loader.load(ctx)

        # Per-tenant default model: when the request omitted `model`, honour the tenant's
        # proxy.default_model / fallback_request_model from the now-merged ctx.config. This
        # must run BEFORE the adapter + G06 so routing/caching/savings all see the tenant
        # model (main.py's global fallback was only a placeholder).
        if ctx.params.get("_model_defaulted"):
            _pcfg = ctx.config.get("proxy", {}) or {}
            _m = _pcfg.get("fallback_request_model") or _pcfg.get("default_model")
            if _m and _m != ctx.model:
                ctx.model = _m
                ctx.routed_model = _m

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

        # Stage 1c — Trust & Safety (G30 injection guardrails → G29 PII redaction).
        # Deliberately placed BEFORE the G04/G05 skip-loops and NOT gated by
        # ctx.skip_groups, so: (1) G24 adaptive-bypass can never disable them,
        # (2) bypass / cache-hit traffic is still guarded, and (3) redaction precedes
        # every content-persisting stage (G05 key/embedding, G07 RAG, G10 memory,
        # G28 CCR). A block short-circuits the whole pipeline like a bypass.
        ctx = await self._run_timed("G30-guardrails", ctx, self.g30.process_request(ctx))
        if ctx.security_blocked:
            logger.info("[%s] G30 blocked request (injection guardrail)", ctx.request_id)
            otel.end_span(pipeline_span)
            return ctx
        ctx = await self._run_timed("G29-pii-redaction", ctx, self.g29.process_request(ctx))
        if ctx.security_blocked:
            logger.info("[%s] G29 blocked request (PII policy)", ctx.request_id)
            otel.end_span(pipeline_span)
            return ctx

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

        # F2 — Intent Orchestration. Classify the request's intent; if it matches a
        # registered downstream agent, dispatch there (its OpenAI-compatible endpoint)
        # INSTEAD of the normal LLM and short-circuit — mirrors the cache/cascade
        # short-circuits. Runs after G06 (tenant/config/adapter resolved) and BEFORE the
        # Stage 3 prompt optimisations, which are tuned for the main LLM, not a black-box
        # agent service. Default off / no-op when no agents are registered (byte-identical).
        ctx = await self._run_timed("F2-intent-orchestration", ctx, self.f2.process_request(ctx))
        if ctx.agent_dispatched:
            logger.info("[%s] F2 dispatched to downstream agent '%s'", ctx.request_id, ctx.agent_id)
            otel.end_span(pipeline_span)
            return ctx

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

        # Stage 3b — Context-Trust (G31 indirect-injection scan). Runs AFTER G07/G10/G28
        # have injected retrieved documents / memories into the prompt, and is NOT gated
        # by ctx.skip_groups — G30's user-prompt scan cannot see this appended context, so
        # a poisoned RAG doc / stored memory would otherwise reach the model un-inspected.
        # A block short-circuits the pipeline like G30 (content-filter 200).
        ctx = await self._run_timed("G31-context-trust", ctx, self.g31.process_request(ctx))
        if ctx.security_blocked:
            logger.info("[%s] G31 blocked request (context-injection guardrail)", ctx.request_id)
            otel.end_span(pipeline_span)
            return ctx

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
        # G29 runs first so response-side PII masking / placeholder restore happens
        # before observability (G18 trace/audit), format shaping, and the G05 cache
        # store — every downstream response consumer sees the redacted content.
        for _name, _fn in [
            ("G29-pii-redaction-resp", lambda r: self.g29.process_response(ctx, r)),
            ("G30-guardrails-resp", lambda r: self.g30.process_response(ctx, r)),
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

        # Stage 5c — application-quality: grounding coverage (Task 11). Needs both the
        # retrieved chunks (G07 stashed them) and the answer (now available). No-op for
        # non-RAG / tool-call answers; never raises.
        try:
            from middleware.quality_metrics import emit_grounding
            emit_grounding(ctx, response)
        except Exception:
            pass

        # Stage 6 — Across the Loop (observability)
        await self._run_timed("G18-observability", ctx, self.g18.record(ctx, response))

        # Store result in G5 cache
        await self._run_timed("G05-store-response", ctx, self.g05.store_response(ctx, response))

        return ctx, response
