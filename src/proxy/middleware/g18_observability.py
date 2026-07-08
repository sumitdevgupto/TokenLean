"""
G18 · Observability & Token FinOps
Stage: Across the Loop
Saving: 15-40% indirect — surfaces waste all other groups fix
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from prometheus_client import Counter, Gauge, Histogram

from middleware import RequestContext
from middleware import langfuse_tracing
from savings.calculator import (
    effective_token_cost,
    estimate_cost,
    estimate_cost_with_cache,
    get_cost_per_1k,
)
from cache.redis_pool import get_redis as _get_redis

logger = logging.getLogger(__name__)
GROUP = "G18"


async def _emit_trace(ctx: RequestContext, response: Dict[str, Any]) -> None:
    """Internal helper for emitting Langfuse traces — patched by tests."""
    langfuse_tracing.finish_trace(ctx, response)


PROMPT_TOKENS = Counter(
    "token_opt_prompt_tokens_total",
    "Total prompt tokens processed",
    ["model", "team", "feature", "tenant_id"],
)
COMPLETION_TOKENS = Counter(
    "token_opt_completion_tokens_total",
    "Total completion tokens processed",
    ["model", "team", "feature", "tenant_id"],
)
REASONING_TOKENS = Counter(
    "token_opt_reasoning_tokens_total",
    "Total reasoning tokens processed (for models with reasoning_effort/thinking)",
    ["model", "team", "feature", "tenant_id"],
)
EFFECTIVE_TOKENS = Counter(
    "token_opt_effective_tokens_total",
    "Effective token cost (ET metric)",
    ["model", "tenant_id"],
)
COST_USD = Counter(
    "token_opt_cost_usd_total",
    "Total estimated cost in USD",
    ["model", "team", "feature", "tenant_id"],
)
CACHE_HITS = Counter(
    "token_opt_cache_hits_total",
    "Cache hits by level",
    ["level", "tenant_id"],
)
SAVINGS_PCT = Histogram(
    "token_opt_savings_pct",
    "Token savings percentage",
    ["tenant_id"],
    buckets=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
)
WORKFLOW_TURNS = Gauge(
    "token_opt_workflow_turns",
    "Current turn count per workflow",
    ["workflow_id", "tenant_id"],
)
TURN_EFFICIENCY_ALERTS = Counter(
    "token_opt_turn_efficiency_alerts_total",
    "Turn efficiency threshold breaches",
)
REQUESTS_TOTAL = Counter(
    "token_opt_requests_total",
    "Total requests processed by the proxy",
    ["model", "team", "feature", "tenant_id"],
)
# Shared latency bucket ladder. Resolves the >10s tail that the old coarse
# buckets collapsed: with only [..., 10000, 30000, 60000] a p99 anywhere between
# 10s and 30s interpolated to the same ~12s midpoint regardless of the true
# value. The finer rungs above 10s make percentiles in the tail meaningful.
_LATENCY_BUCKETS_MS = [
    10, 25, 50, 100, 250, 500, 1000, 2000, 3000, 5000, 7500,
    10000, 15000, 20000, 30000, 45000, 60000, 120000,
]
# Per-stage ladder — one histogram per (stage, tenant), so it is slightly leaner
# than the full request ladder to keep series cardinality bounded (buckets ×
# ~30 stages × tenants), while still resolving sub-100ms stages and the
# multi-second sidecar/cascade tail that the old [50,250,1000,5000,30000]
# snapped into two-order-of-magnitude buckets.
_STAGE_BUCKETS_MS = [
    5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000, 30000, 60000,
]

REQUEST_DURATION_MS = Histogram(
    "token_opt_request_duration_ms",
    "End-to-end request latency in milliseconds (used by SLA dashboard)",
    ["tenant_id", "status"],
    buckets=_LATENCY_BUCKETS_MS,
)
LLM_DURATION_MS = Histogram(
    "token_opt_llm_duration_ms",
    "Provider LLM call latency in milliseconds; cache hits and bypasses never "
    "reach the provider and are not observed here",
    ["tenant_id"],
    buckets=_LATENCY_BUCKETS_MS,
)
PROXY_OVERHEAD_MS = Histogram(
    "token_opt_proxy_overhead_ms",
    "Proxy-induced latency in milliseconds: end-to-end duration minus total "
    "provider LLM call time (main call plus any provider calls made inside "
    "middleware — G06 cascade/judge, G10 summarisation, G09 schema). Cache hits "
    "and bypasses skip the provider, so their full duration counts as proxy "
    "time (used by SLA dashboard 'Proxy only' row)",
    ["tenant_id", "status"],
    buckets=_LATENCY_BUCKETS_MS,
)
STAGE_DURATION_MS = Histogram(
    "token_opt_stage_duration_ms",
    "Per-pipeline-stage wall-clock latency in milliseconds (G-group granularity); "
    "attributes proxy overhead to the stage that caused it (SLA 'Proxy time by "
    "stage' panel + the Latency Breakup dashboard). The pseudo-stage 'LLM-call' "
    "carries the main provider completion so a normal request's breakup sums to "
    "end-to-end; on G06-cascade requests the provider time lives inside the "
    "'G06-routing' stage instead (no separate main call is made)",
    ["stage", "tenant_id"],
    buckets=_STAGE_BUCKETS_MS,
)
HTTP_REQUESTS = Counter(
    "token_opt_http_requests_total",
    "HTTP requests by outcome status, counting every exit path including "
    "errors and short-circuits (used by SLA error-rate / uptime panels)",
    ["tenant_id", "status"],
)
GROUP_TOKENS_SAVED = Counter(
    "token_opt_group_tokens_saved_total",
    "Tokens saved per optimisation group (G0-G18), for real-time per-group dashboards",
    ["group", "tenant_id"],
)
USD_SAVED = Counter(
    "token_opt_usd_saved_total",
    "USD saved per optimisation group, derived from config pricing table",
    ["group", "model", "tenant_id"],
)
OUTPUT_HOLDOUT_COMPLETION_TOKENS = Histogram(
    "g11_output_holdout_completion_tokens",
    "Completion tokens by G11 output-shaping holdout cohort (treatment vs holdout); "
    "compare cohort means to measure the real output-token reduction. "
    "pct_reduction = 1 - mean(treatment)/mean(holdout)",
    ["cohort", "tenant_id"],
    buckets=[16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384],
)


class G18Observability:
    def __init__(self, usage_meter=None, audit_logger=None):
        self._usage_meter = usage_meter
        self._audit_logger = audit_logger

    async def record(self, ctx: RequestContext, response: Dict[str, Any]) -> None:
        cfg = ctx.config.get("groups", {}).get("G18_observability", {})
        if not cfg.get("enabled", False):
            return

        usage = response.get("usage", {})
        provider_prompt = usage.get("prompt_tokens")  # z — None when usage absent
        prompt_tokens = provider_prompt if provider_prompt is not None else ctx.savings.proxy_optimised_tokens
        completion_tokens = usage.get("completion_tokens", 0)

        # Provider-normalised cached + reasoning tokens (multi-provider): the adapter knows
        # each provider's usage field names (OpenAI prompt_tokens_details.cached_tokens,
        # Anthropic cache_read_input_tokens, Gemini cached_content_token_count, etc.). The
        # base/default reads the OpenAI shape, so this is a no-op for OpenAI and for the
        # legacy adapter-less path below.
        if ctx.provider_adapter is not None:
            try:
                _u = ctx.provider_adapter.extract_usage(response)
            except Exception as _exc:
                logger.debug("[%s] G18: extract_usage failed: %s", ctx.request_id, _exc)
                _u = {}
        else:
            _u = {
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                or usage.get("thinking_tokens", 0),
            }
        cache_read_tokens = _u.get("cached_tokens", 0) or 0
        reasoning_tokens = _u.get("reasoning_tokens", 0) or 0

        # Labels
        team = ctx.params.get("x_team", "default")
        feature = ctx.params.get("x_feature", "default")
        model = ctx.routed_model
        tenant_id = ctx.tenant_id

        # Update savings record.
        # G18 is the single source of truth for cost: baseline and actual MUST be
        # computed on the same basis (both include the same completion tokens) or the
        # comparison is meaningless. Baseline = un-optimised counterfactual (full prompt
        # at the originally-requested model); actual = optimised prompt at the routed
        # model — so this still credits G06 routing without the input-only asymmetry that
        # made "actual" appear ~200x "baseline". Do NOT let upstream stages (e.g. G06)
        # pre-seed these fields with an input-only estimate; G18 always recomputes both.
        ctx.savings.provider_prompt_tokens = provider_prompt  # z (None if usage absent)
        ctx.savings.final_tokens_sent = prompt_tokens          # z when present, else y estimate
        ctx.savings.response_tokens = completion_tokens
        # Actual cost credits the provider's cached-input discount on the
        # response-reported cached tokens (G21 P1). Provider-agnostic: the per-provider
        # multiplier comes from the adapter (Gate 3 — no provider strings here). With
        # cache_read_tokens == 0 this is identical to estimate_cost().
        cache_read_multiplier = 1.0
        if cache_read_tokens and ctx.provider_adapter is not None:
            try:
                cache_read_multiplier = ctx.provider_adapter.cache_read_cost_multiplier(ctx.config)
            except Exception as _exc:
                logger.debug("[%s] G18: cache_read_cost_multiplier failed: %s", ctx.request_id, _exc)
                cache_read_multiplier = 1.0
        # B3: discount-aware price book (reporting-only) — optional reasoning surcharge
        # and a provider-native batch discount, both config-gated (default 1.0 = no-op,
        # so cost_actual is unchanged unless a deployment opts in). The batch discount
        # only applies when the request was served via the native batch lane.
        reasoning_rate_multiplier = cfg.get("reasoning_rate_multiplier", 1.0)
        batch_discount = (
            cfg.get("batch_discount_multiplier", 1.0)
            if ctx.params.get("_native_batch")
            else 1.0
        )
        ctx.savings.cost_actual_usd = estimate_cost_with_cache(
            prompt_tokens, cache_read_tokens, completion_tokens, model, cache_read_multiplier,
            batch_discount=batch_discount,
            reasoning_tokens=reasoning_tokens,
            reasoning_rate_multiplier=reasoning_rate_multiplier,
        )
        ctx.savings.cost_baseline_usd = estimate_cost(
            ctx.savings.baseline_tokens, completion_tokens, ctx.savings.model_requested
        )

        # ET metric
        et = effective_token_cost(prompt_tokens, cache_read_tokens, completion_tokens)
        ctx.savings.effective_token_et = round(et, 2)
        metadata = ctx.savings.to_langfuse_metadata()
        metadata["warnings"] = ctx.params.get("_token_opt_warnings", [])
        metadata["reasoning_tokens"] = reasoning_tokens

        # ── Prometheus metrics ──
        if cfg.get("prometheus_enabled", True):
            REQUESTS_TOTAL.labels(model=model, team=team, feature=feature, tenant_id=tenant_id).inc()
            PROMPT_TOKENS.labels(model=model, team=team, feature=feature, tenant_id=tenant_id).inc(prompt_tokens)
            COMPLETION_TOKENS.labels(model=model, team=team, feature=feature, tenant_id=tenant_id).inc(completion_tokens)
            if reasoning_tokens > 0:
                REASONING_TOKENS.labels(model=model, team=team, feature=feature, tenant_id=tenant_id).inc(reasoning_tokens)
            EFFECTIVE_TOKENS.labels(model=model, tenant_id=tenant_id).inc(et)
            COST_USD.labels(model=model, team=team, feature=feature, tenant_id=tenant_id).inc(ctx.savings.cost_actual_usd)
            if ctx.savings.cache_hit:
                level = ctx.savings.cache_level or "UNKNOWN"
                CACHE_HITS.labels(level=level, tenant_id=tenant_id).inc()
            SAVINGS_PCT.labels(tenant_id=tenant_id).observe(ctx.savings.total_pct_saving)

            inp_cost, _ = get_cost_per_1k(model)
            for step in ctx.savings.step_savings:
                if step.absolute_saving > 0:
                    GROUP_TOKENS_SAVED.labels(group=step.group, tenant_id=tenant_id).inc(step.absolute_saving)
                    group_usd = round(step.absolute_saving / 1000.0 * inp_cost, 8)
                    USD_SAVED.labels(group=step.group, model=model, tenant_id=tenant_id).inc(group_usd)

            workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id")
            if workflow_id:
                turn_count = ctx.params.get("_token_budget", {}).get("workflow_turn", 0)
                WORKFLOW_TURNS.labels(workflow_id=workflow_id, tenant_id=tenant_id).set(turn_count)

        # ── Turn Efficiency KPI ──
        if cfg.get("turn_efficiency_enabled", True):
            await self._check_turn_efficiency(ctx, cfg)

        # ── Tool governance ──
        if cfg.get("tool_governance_enabled", True):
            await self._record_tool_calls(ctx, response)

        # ── JSONL export ──
        await self._export_jsonl(ctx, metadata)

        # ── Langfuse trace ──
        await _emit_trace(ctx, response)

        # ── Usage metering (billing) ──
        billing_cfg = ctx.config.get("billing", {})
        if billing_cfg.get("enabled", False) and getattr(self, "_usage_meter", None):
            try:
                await self._usage_meter.record(ctx, response)
            except Exception as _exc:
                logger.warning("G18: billing record failed: %s", _exc)

        # ── Audit logging (F3) ──
        audit_cfg = ctx.config.get("audit", {})
        if audit_cfg.get("enabled", False) and getattr(self, "_audit_logger", None):
            try:
                await self._audit_logger.log(ctx, response)
            except Exception as _exc:
                logger.warning("G18: audit log failed: %s", _exc)

        logger.info(
            "[%s] G18 saved=%dt (%.1f%%) cost_saving=$%.6f reasoning_tokens=%d",
            ctx.request_id,
            ctx.savings.total_absolute_saving,
            ctx.savings.total_pct_saving,
            ctx.savings.cost_saving_usd,
            reasoning_tokens,
        )

    async def _check_turn_efficiency(self, ctx: RequestContext, cfg: Dict) -> None:
        workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id")
        if not workflow_id:
            return
        turn_count = ctx.params.get("_token_budget", {}).get("workflow_turn", 0)
        if not turn_count:
            return

        try:
            redis = _get_redis()
            # I4: tenant-scope so two tenants' workflows can't collide on the same id.
            baseline_key = f"{getattr(ctx, 'redis_prefix', '')}tok_opt:turn_baseline:{workflow_id}"
            baseline_raw = await redis.get(baseline_key)
            multiplier = cfg.get("turn_efficiency_baseline_multiplier", 2.0)

            if baseline_raw is None:
                await redis.set(baseline_key, str(turn_count), ex=86400)
                return

            baseline = int(baseline_raw)
            threshold = int(baseline * multiplier)

            if turn_count > threshold:
                TURN_EFFICIENCY_ALERTS.inc()
                warning = (
                    f"Turn efficiency alert: {turn_count} > {threshold} turns "
                    f"(baseline {baseline} x {multiplier})"
                )
                ctx.params.setdefault("_token_opt_warnings", []).append(warning)
                logger.warning("[%s] G18 %s", ctx.request_id, warning)
        except Exception as exc:
            logger.warning("G18 turn efficiency check failed: %s", exc)

    async def _record_tool_calls(self, ctx: RequestContext, response: Dict) -> None:
        choices = response.get("choices", [])
        tool_names = set()
        for choice in choices:
            msg = choice.get("message") or {}
            for tc in msg.get("tool_calls") or []:
                name = tc.get("function", {}).get("name", "")
                if name:
                    tool_names.add(name)
        if not tool_names:
            return

        try:
            redis = _get_redis()
            now = time.time()
            for name in tool_names:
                await redis.zadd("tok_opt:tool_calls", {name: now})
        except Exception as exc:
            logger.warning("G18 tool governance recording failed: %s", exc)

    async def _export_jsonl(self, ctx: RequestContext, metadata: Dict) -> None:
        cfg = ctx.config.get("groups", {}).get("G18_observability", {})
        bucket = cfg.get("jsonl_gcs_bucket", "")
        prefix = cfg.get("jsonl_gcs_prefix", "token-usage-logs")

        from storage import get_storage_backend
        backend = get_storage_backend(bucket=bucket)
        if backend is None:
            return

        workflow_id = (
            ctx.params.get("workflow_id")
            or ctx.params.get("x_workflow_id")
            or ctx.request_id
        )
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        # I7: partition exported usage logs by tenant so per-tenant data isn't
        # co-mingled under one GCS prefix.
        tenant_seg = getattr(ctx, "tenant_id", "default") or "default"
        path = f"{prefix}/{tenant_seg}/{workflow_id}/{ts}.json"

        try:
            backend.write(path, json.dumps(metadata) + "\n")
            logger.debug("[%s] G18 JSONL exported to %s", ctx.request_id, path)
        except Exception as exc:
            logger.warning("G18 JSONL export failed: %s", exc)


