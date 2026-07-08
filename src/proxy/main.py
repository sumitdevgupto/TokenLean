"""
Token Optimisation Proxy — main entry point.

Exposes an OpenAI-compatible /v1/chat/completions endpoint.
Developers swap only their base_url; all optimisations (G0-G28, G26 reserved) are transparent.

Authentication: Bearer <proxy-key>  (issued per developer/team, stored in Secret Manager)
                Developers NEVER receive LLM provider keys.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import hashlib
import hmac

import litellm

# Global safety net: let litellm drop request params a routed model doesn't support
# (multi-provider) instead of 400-ing. The adapter's unsupported_params() is the explicit
# belt for OpenAI-compatible custom providers litellm can't introspect.
litellm.drop_params = True

from auth.api_key_manager import get_llm_provider_key, validate_proxy_key, is_admin_key, is_suspended
from config_loader import get_config, get_fallback_request_model, load_config, start_hot_reload
from providers import get_adapter, apply_context_management, get_provider_entry
from middleware.g18_observability import (
    REQUEST_DURATION_MS,
    HTTP_REQUESTS,
    LLM_DURATION_MS,
    PROXY_OVERHEAD_MS,
    STAGE_DURATION_MS,
)
from middleware import RequestContext
from middleware.g00_rate_limit import RateLimitExceeded
from middleware.g03_doc_pipeline import trigger_doc_ingestion
from middleware.g13_batch import start_batch_consumer, start_batch_poller
from middleware.pipeline import OptimisationPipeline
from middleware import langfuse_tracing

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# H2: bearer token guarding the /metrics scrape endpoint. When unset the endpoint
# is open (local dev); set it in production so per-tenant metrics aren't public.
_METRICS_SCRAPE_TOKEN = os.getenv("METRICS_SCRAPE_TOKEN", "")

app = FastAPI(
    title="Token Optimisation Proxy",
    description="LLM proxy implementing G0-G28 token optimisations (G26 reserved).",
    version="1.0.0",
)
# CORS: Restrict to specific origins in production via CORS_ORIGINS env var
# Format: comma-separated URLs, e.g., "https://myapp.com,https://myapp-staging.com"
# For local development, set to "http://localhost:3000,http://localhost:8080"
cors_origins = os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins else ["*"],  # Fallback to wildcard only if not configured
    allow_methods=["*"],
    allow_headers=["*"],
)

_pipeline = OptimisationPipeline()


@app.on_event("startup")
async def startup_event():
    load_config()
    start_hot_reload()
    # Initialise shared Redis connection pool (eliminates per-request churn)
    from cache.redis_pool import init_pool, get_redis
    init_pool()
    redis = get_redis()
    # Inject Redis client into G20 so it can load pre-computed DSPy templates
    _pipeline.g20._redis = redis
    cfg = get_config()
    # Start G13 Redis Streams batch consumer background task
    asyncio.create_task(start_batch_consumer(cfg))
    # Start G13 provider-native batch poller (no-op unless provider_native is on)
    asyncio.create_task(start_batch_poller(cfg))
    # Initialise OpenLLMetry (OTLP auto-instrumentation for LLM SDKs)
    _init_openllmetry(cfg)
    # Ensure billing table exists and wire UsageMeter (idempotent DDL)
    global _usage_meter
    try:
        from cache.pg_pool import get_pg_pool
        from billing.models import USAGE_EVENTS_DDL
        from billing.metering import UsageMeter
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set")
        pg = await get_pg_pool(db_url)
        async with pg.acquire() as conn:
            await conn.execute(USAGE_EVENTS_DDL)
        _usage_meter = UsageMeter(db_pool=pg)
        logger.info("Billing: usage_events table ready")
    except Exception as exc:
        logger.warning("Billing: could not initialise usage_events: %s", exc)
    if not _METRICS_SCRAPE_TOKEN:
        logger.warning(
            "METRICS_SCRAPE_TOKEN is not set — /metrics is unauthenticated. Set it in "
            "production so per-tenant token/cost metrics are not world-readable."
        )
    logger.info("Token Optimisation Proxy started")


@app.on_event("shutdown")
async def shutdown_event():
    from cache.redis_pool import close_pool
    await close_pool()
    logger.info("Token Optimisation Proxy shut down")


def _init_openllmetry(cfg: Dict[str, Any]) -> None:
    try:
        from traceloop.sdk import Traceloop
        g18 = cfg.get("groups", {}).get("G18_observability", {})
        if g18.get("openllmetry_enabled", False):
            endpoint = g18.get("openllmetry_endpoint", "")
            kwargs = {"app_name": "token-optimisation-proxy"}
            if endpoint:
                kwargs["api_endpoint"] = endpoint
            Traceloop.init(**kwargs)
            logger.info("OpenLLMetry initialised")
    except Exception as exc:
        logger.warning("OpenLLMetry init failed: %s", exc)


# ---------------------------------------------------------------------------
# Health / info endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/metrics")
async def metrics(request: Request):
    """Prometheus scrape endpoint — exposes all token-optimisation metrics.

    Gated by ``METRICS_SCRAPE_TOKEN`` (Bearer) when that env var is set. The
    metrics carry per-tenant token/cost labels, so on a public Cloud Run service
    they must not be world-readable. When the token is unset the endpoint stays
    open for local dev (a one-time startup warning is logged in startup_event).
    """
    if _METRICS_SCRAPE_TOKEN:
        auth = request.headers.get("Authorization", "")
        provided = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(provided, _METRICS_SCRAPE_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid or missing metrics scrape token")
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/admin/tool-governance")
async def tool_governance(request: Request):
    """Return tools with no calls in the last N days (configurable)."""
    user_id, _api_key, tenant_metadata = await _authenticate(request)
    _require_admin(tenant_metadata, "cross-tenant tool governance")
    cfg = get_config().get("groups", {}).get("G18_observability", {})
    days = cfg.get("tool_governance_days", 30)

    try:
        from cache.redis_pool import get_redis
        redis = get_redis()
        now = time.time()
        cutoff = now - (days * 86400)

        # Tools with recent calls
        recent = await redis.zrangebyscore("tok_opt:tool_calls", cutoff, "+inf")
        # All tools ever recorded
        all_tools = await redis.zrange("tok_opt:tool_calls", 0, -1)
        stale = sorted(set(all_tools) - set(recent))

        return {
            "stale_tools": stale,
            "days_threshold": days,
            "recent_count": len(set(recent)),
            "stale_count": len(stale),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Tool governance query failed: {exc}")


@app.post("/admin/alert-webhook")
async def alert_webhook(request: Request):
    """Receive Alertmanager webhook payloads for budget overrun alerts.

    Payload format (Alertmanager v1):
    {
        "version": "4",
        "groupKey": "...",
        "status": "firing|resolved",
        "receiver": "...",
        "alerts": [
            {
                "labels": {"team": "team-a", "feature": "feature-x", ...},
                "annotations": {"summary": "...", "description": "..."},
                "startsAt": "2026-06-07T12:00:00Z",
                ...
            }
        ],
        ...
    }
    """
    user_id, _api_key, tenant_metadata = await _authenticate(request)
    _require_admin(tenant_metadata, "alert webhook ingestion")
    try:
        payload = await request.json()
        from cache.redis_pool import get_redis
        redis = get_redis()

        # Store alert in Redis for audit trail (TTL 30 days)
        alert_id = str(uuid.uuid4())
        alert_record = {
            "received_at": time.time(),
            "payload": payload,
            "processed_by": user_id,
        }
        await redis.setex(f"tok_opt:alert:{alert_id}", 30 * 86400, json.dumps(alert_record))

        # Log alert for immediate visibility
        alerts = payload.get("alerts", [])
        for alert in alerts:
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            logger.warning(
                "[ALERT] %s: %s - %s (team=%s, feature=%s)",
                alert.get("status", "unknown"),
                labels.get("alertname", "unknown"),
                annotations.get("summary", "no summary"),
                labels.get("team", "unknown"),
                labels.get("feature", "unknown"),
            )

        return {"received": True, "alerts_count": len(alerts), "alert_id": alert_id}
    except Exception as exc:
        logger.error("Alert webhook processing failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Alert processing failed: {exc}")


@app.get("/admin/budget-status")
async def budget_status(request: Request):
    """Return current budget consumption per team/feature from Prometheus/Redis.

    Returns aggregated token usage and remaining budget for all configured
    teams and features. Used by operators and chargeback systems.
    """
    user_id, _api_key, tenant_metadata = await _authenticate(request)
    _require_admin(tenant_metadata, "cross-tenant budget status")
    try:
        from cache.redis_pool import get_redis
        redis = get_redis()
        cfg = get_config().get("groups", {}).get("G18_observability", {})

        # Get budget configuration
        team_budgets = cfg.get("team_daily_budgets", {})
        feature_budgets = cfg.get("feature_daily_budgets", {})

        # Query Redis for today's usage (keys: tok_opt:usage:{team}:{feature}:{date})
        today = time.strftime("%Y-%m-%d")
        result = {
            "queried_at": time.time(),
            "date": today,
            "teams": {},
            "features": {},
        }

        # Aggregate team usage
        for team, budget in team_budgets.items():
            # Sum all features for this team
            pattern = f"tok_opt:usage:{team}:*:{today}"
            keys = await redis.keys(pattern)
            total_tokens = 0
            for key in keys:
                try:
                    total_tokens += int(await redis.get(key) or 0)
                except (ValueError, TypeError):
                    continue
            result["teams"][team] = {
                "budget": budget,
                "consumed": total_tokens,
                "remaining": max(0, budget - total_tokens),
                "percent_used": round((total_tokens / budget * 100), 2) if budget > 0 else 0,
            }

        # Aggregate feature usage
        for feature, budget in feature_budgets.items():
            # Sum all teams for this feature
            pattern = f"tok_opt:usage:*:{feature}:{today}"
            keys = await redis.keys(pattern)
            total_tokens = 0
            for key in keys:
                try:
                    total_tokens += int(await redis.get(key) or 0)
                except (ValueError, TypeError):
                    continue
            result["features"][feature] = {
                "budget": budget,
                "consumed": total_tokens,
                "remaining": max(0, budget - total_tokens),
                "percent_used": round((total_tokens / budget * 100), 2) if budget > 0 else 0,
            }

        return result
    except Exception as exc:
        logger.error("Budget status query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Budget status query failed: {exc}")


@app.post("/admin/usage-export")
async def usage_export(request: Request):
    """Export token-usage records as JSONL from the Postgres ``usage_events`` table.

    Request body (all fields optional):
    {
        "start_date": "2024-06-01",   # inclusive, UTC
        "end_date": "2024-06-08",     # inclusive, UTC
        "tenant_id": "nova-med",      # optional filter
    }

    Returns: application/x-ndjson stream of usage records.

    Fair-disclosure: ``cost_saved_usd`` is a config-priced *estimate* (token counts ×
    the static ``pricing:`` table), NOT provider-reconciled billing — no discounts,
    cache/batch credits, or reasoning surcharges are modelled. Treat it as directional,
    not invoice-grade.
    """
    user_id, _api_key, tenant_metadata = await _authenticate(request)
    try:
        import datetime
        from cache.pg_pool import get_pg_pool

        body = await request.json()
        start_date = body.get("start_date", "")
        end_date = body.get("end_date", "")
        tenant_filter = body.get("tenant_id")
        # H1: non-admin keys may only export their OWN tenant — ignore any
        # client-supplied tenant_id (and never return the all-tenant set).
        if not is_admin_key(tenant_metadata):
            tenant_filter = _caller_tenant_id(tenant_metadata)

        def _parse_date(d: str) -> datetime.datetime:
            return datetime.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)

        start_dt = _parse_date(start_date) if start_date else datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        end_dt = _parse_date(end_date) if end_date else datetime.datetime.now(datetime.timezone.utc)

        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            raise HTTPException(status_code=503, detail="Database unavailable (DATABASE_URL not set)")
        pool = await get_pg_pool(db_url)

        sql = (
            "SELECT tenant_id, request_id, timestamp, baseline_tokens, optimised_tokens, "
            "proxy_optimised_tokens, provider_prompt_tokens, "
            "tokens_saved, cost_saved_usd, groups_applied, pricing_tier, model, routed_model "
            "FROM usage_events WHERE timestamp >= $1 AND timestamp <= $2"
        )
        args = [start_dt, end_dt]
        if tenant_filter:
            sql += " AND tenant_id = $3"
            args.append(tenant_filter)
        sql += " ORDER BY timestamp DESC"

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        # Stream JSONL, coercing non-JSON-native types (TIMESTAMPTZ → ISO, NUMERIC → float)
        lines = []
        for r in rows:
            record = dict(r)
            ts = record.get("timestamp")
            if ts is not None and hasattr(ts, "isoformat"):
                record["timestamp"] = ts.isoformat()
            cost = record.get("cost_saved_usd")
            if cost is not None:
                record["cost_saved_usd"] = float(cost)
            lines.append(json.dumps(record, separators=(",", ":")))

        if not lines:
            lines = [json.dumps({"message": "no_records", "start_date": start_date, "end_date": end_date}, separators=(",", ":"))]

        content = "\n".join(lines) + "\n"
        return StreamingResponse(
            iter([content]),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="usage-export.jsonl"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Usage export failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Usage export failed: {exc}")


@app.get("/v1/batch/results/{request_id}")
async def batch_results(request_id: str, request: Request):
    """Poll for a deferred batch response by request_id (returned in the 202 body)."""
    user_id, _api_key, tenant_metadata = await _authenticate(request)
    try:
        from cache.redis_pool import get_redis
        from middleware.g13_batch import get_batch_result_owner
        r = get_redis()
        # H1: only the owning tenant (or an admin key) may poll this result.
        # Return 404 (not 403) to a non-owner so we don't confirm the id exists.
        owner = await get_batch_result_owner(request_id)
        if owner and not is_admin_key(tenant_metadata) and owner != _caller_tenant_id(tenant_metadata):
            return JSONResponse(status_code=404, content={"status": "not_found", "request_id": request_id})
        key = f"tok_opt:batch_result:{request_id}"
        raw = await r.get(key)
        if raw is None:
            return JSONResponse(status_code=202, content={"status": "pending", "request_id": request_id})
        return JSONResponse(content=json.loads(raw))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Batch result lookup failed: {exc}")


@app.get("/v1/models")
async def list_models(request: Request):
    user_id, _api_key, _tenant_metadata = await _authenticate(request)
    cfg = get_config()
    providers = cfg.get("providers", [])
    models = []
    for p in providers:
        for m in p.get("models", []):
            models.append({"id": m, "object": "model", "owned_by": p.get("name", "")})
    return {"object": "list", "data": models}


# ---------------------------------------------------------------------------
# Document ingestion webhook (G03)
# ---------------------------------------------------------------------------

@app.post("/ingest-doc")
async def ingest_doc(request: Request):
    """GCS pub/sub push notification → triggers G03 doc pipeline job."""
    payload = await request.json()
    bucket = payload.get("bucket", "")
    obj = payload.get("name", "")
    if bucket and obj:
        success = await trigger_doc_ingestion(bucket, obj)
        return {"triggered": success, "object": obj}
    raise HTTPException(status_code=400, detail="Missing bucket or name in payload")


# ---------------------------------------------------------------------------
# Main proxy endpoint — OpenAI-compatible
# ---------------------------------------------------------------------------

# Body params consumed by middleware for routing/retrieval/loop-control —
# not recognized by the provider's completion API and must not be forwarded.
_INTERNAL_PARAM_KEYS = {"template_id", "workflow_id", "rag_query", "batch_id", "burst_block"}
_usage_meter = None  # initialised in startup_event once pg pool is ready


def _schedule_billing(ctx, response) -> None:
    """Fire-and-forget exactly one ``usage_events`` row for a billable served
    request (C1 normal/cache/bypass, C1b batch-deferred). Idempotent at the DB
    (request_id UNIQUE + ON CONFLICT DO NOTHING). No-op when billing isn't wired
    (no UsageMeter / no DB pool) or there is no running event loop."""
    if ctx is None or response is None or _usage_meter is None:
        return
    try:
        asyncio.create_task(_usage_meter.record(ctx, response))
    except RuntimeError:  # no running loop (not the request path) — skip billing
        logger.debug("[%s] billing skipped: no running event loop", getattr(ctx, "request_id", "?"))


def _record_outcome(ctx, start_ts: float, status: str, response=None) -> None:
    """Record SLA metrics (latency histogram + status-labelled counter) for one
    request, at every exit path — success, short-circuit, and error. ``ctx`` may
    be None for failures that occur before the context is built.

    C1: also writes the billable ``usage_events`` row for every **served 2xx**
    request — normal LLM call, cache hit, or bypass — as one fire-and-forget row.
    ``usage_events.request_id`` is UNIQUE + ON CONFLICT DO NOTHING, so this is
    idempotent. Non-2xx exits pass a non-"200" status (and/or no ``response``) and
    are never billed; batch-deferred (202) is billed separately at defer (C1b)."""
    tenant_id = getattr(ctx, "tenant_id", "default") if ctx is not None else "default"
    elapsed_ms = (time.time() - start_ts) * 1000
    llm_ms = getattr(ctx, "llm_elapsed_ms", 0.0) if ctx is not None else 0.0
    try:
        REQUEST_DURATION_MS.labels(tenant_id=tenant_id, status=status).observe(elapsed_ms)
        HTTP_REQUESTS.labels(tenant_id=tenant_id, status=status).inc()
        # Proxy-vs-LLM latency split: overhead = end-to-end minus provider call
        # time. Cache hits/bypasses have llm_ms=0 → full duration is proxy time.
        PROXY_OVERHEAD_MS.labels(tenant_id=tenant_id, status=status).observe(
            max(0.0, elapsed_ms - llm_ms)
        )
        if llm_ms > 0:
            LLM_DURATION_MS.labels(tenant_id=tenant_id).observe(llm_ms)
    except Exception as exc:  # never let metrics break the response
        logger.debug("SLA metric record failed: %s", exc)
    # C1: one billable row per served 2xx request (best-effort, never blocks).
    if status == "200":
        _schedule_billing(ctx, response)


def _stream_response(ctx, call_model, call_kwargs, outgoing_params, request_id, request_start):
    """Pass-through SSE streaming: relay the provider's chunks unchanged.

    Request-side optimisations (G0–G13) are already applied before this is called. The
    response-side pipeline (G14/G18/G23) is intentionally skipped for streamed calls;
    usage is captured best-effort from the final chunk (needs provider stream-usage
    support) and billing fires once on completion (billing is per-request).
    """
    import json as _json

    params = dict(outgoing_params)
    # Ask for a usage chunk where the provider supports it; litellm.drop_params removes it
    # for providers that don't.
    params.setdefault("stream_options", {"include_usage": True})

    async def event_gen():
        last_usage = {}
        parts = []  # accumulated assistant text for chunk-aware G23 (measurement only)
        _llm_start = time.time()
        try:
            stream = await litellm.acompletion(
                model=call_model, messages=ctx.messages, **call_kwargs, **params
            )
            async for chunk in stream:
                cd = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                if cd.get("usage"):
                    last_usage = cd["usage"]
                try:
                    delta = (cd.get("choices") or [{}])[0].get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        parts.append(delta["content"])
                except Exception:
                    pass
                yield f"data: {_json.dumps(cd, separators=(',', ':'))}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("[%s] streaming LLM call failed: %s", request_id, exc)
            yield f"data: {_json.dumps({'error': str(exc)})}\n\n"
        finally:
            # Chunk-aware G23: run output compression on the reassembled stream to record the
            # output-side savings the (skipped) response pipeline would have. The live chunks
            # are emitted unchanged — rewriting them mid-stream would corrupt the SSE output.
            _apply_stream_g23(ctx, "".join(parts))
            # The response pipeline (incl. G18) is skipped for streamed calls, so wire the
            # provider's real usage from the final chunk into savings here — otherwise the
            # billed row records real input/output tokens (z / response_tokens) as 0.
            if last_usage:
                try:
                    ctx.savings.provider_prompt_tokens = last_usage.get("prompt_tokens")
                    ctx.savings.response_tokens = last_usage.get("completion_tokens", 0) or 0
                except Exception:
                    pass
            try:
                # For streamed calls the provider owns the whole stream lifetime,
                # so LLM time = acompletion start → last chunk consumed. += to
                # preserve any provider time already booked by request-side
                # middleware (e.g. G06 judge).
                ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
                _record_outcome(
                    ctx, request_start, "200", {"usage": last_usage} if last_usage else None
                )
            except Exception as exc:
                logger.debug("[%s] streaming _record_outcome failed: %s", request_id, exc)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _apply_stream_g23(ctx, content: str) -> None:
    """Record G23 output-side savings for a streamed response (measurement only)."""
    if not content:
        return
    cfg = ctx.config.get("groups", {}).get("G23_streaming_compression", {})
    if not cfg.get("enabled", False):
        return
    try:
        from middleware.g23_streaming_compression import _compress_text, _estimate_tokens_from_chars
        compressed, chars_saved = _compress_text(
            content, cfg.get("min_repeat", 3), cfg.get("ngram_size", 5)
        )
        if chars_saved > 0:
            orig_t = _estimate_tokens_from_chars(len(content))
            saved_t = _estimate_tokens_from_chars(chars_saved)
            ctx.savings.add_step(
                "G23",
                f"G23 (stream): output compressed {chars_saved} chars → ~{saved_t} tokens saved",
                orig_t, orig_t - saved_t,
            )
    except Exception as exc:
        logger.debug("[%s] stream G23 failed: %s", ctx.request_id, exc)


def _served_response(ctx, response_dict: Dict, request_start: float) -> JSONResponse:
    """Finalise a served 2xx response: attach savings metadata + headers, record
    SLA/billing (`_record_outcome`), and return the JSONResponse.

    Shared by the normal LLM path and the G06 cascade short-circuit so both bill
    and surface headers identically.
    """
    response_dict.setdefault("_token_opt", {}).update(ctx.savings.to_langfuse_metadata())

    headers = {"x-savings-usd": f"{ctx.savings.cost_saving_usd:.6f}"}
    # G17: expose InterAgentState via x-token-opt-state header for downstream agents
    token_budget_state = ctx.params.get("_token_budget")
    if token_budget_state:
        import base64
        import json

        headers["x-token-opt-state"] = base64.b64encode(
            json.dumps(token_budget_state, separators=(",", ":")).encode("utf-8")
        ).decode("utf-8")

    # C1: SLA metrics + the billable usage_events row, centralised so every
    # 2xx-served path bills exactly once.
    _record_outcome(ctx, request_start, "200", response_dict)
    return JSONResponse(content=response_dict, headers=headers)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _request_start = time.time()
    request_id = str(uuid.uuid4())
    user_id, api_key, tenant_metadata = await _authenticate(request)
    body = await request.json()

    messages = body.get("messages", [])
    model = body.get("model") or get_fallback_request_model()
    params = {k: v for k, v in body.items() if k not in ("messages", "model")}

    # Map X-* proxy headers into ctx.params as x_ keys so middleware can read them
    # e.g. X-Template-ID → x_template_id, X-Rag-Collection → x_rag_collection
    for header_name, header_value in request.headers.items():
        lower = header_name.lower()
        if lower.startswith("x-") and lower not in ("x-user-id", "x-scenario-tag"):
            param_key = lower.replace("-", "_")  # x-rag-collection → x_rag_collection
            params[param_key] = header_value

    # G7 RAG: derive rag_query from the last user message when x_rag_collection is set
    if "x_rag_collection" in params and "rag_query" not in params:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                params["rag_query"] = msg.get("content", "")
                break

    # Store API key hash for tenant resolution in pipeline
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    params["_api_key_hash"] = api_key_hash

    # C1: the validated key is the authoritative tenant identity. New-format keys
    # carry {tenant_id, tier, admin}; legacy string keys have no metadata and fall
    # through to the "default" tenant. The pipeline reads these _auth_* params and
    # only honours an X-Tenant-ID header for admin-scoped keys. All _-prefixed
    # params are stripped before the LLM call.
    if isinstance(tenant_metadata, dict):
        params["_auth_tenant_id"] = tenant_metadata.get("tenant_id")
        params["_auth_tier"] = tenant_metadata.get("tier", "free")
        params["_auth_admin"] = bool(tenant_metadata.get("admin", False))

    cfg = get_config()

    # Build request context
    ctx = RequestContext.create(
        request_id=request_id,
        user_id=user_id,
        messages=messages,
        model=model,
        params=params,
        config=cfg,
    )

    # Run G0-G17 request pipeline
    try:
        ctx = await _pipeline.process_request(ctx, request_headers=dict(request.headers))
    except RateLimitExceeded as exc:
        logger.warning(
            "[%s] Rate limit exceeded: %s for %s (retry-after=%d)",
            request_id,
            exc.limit_type,
            exc.scope,
            exc.retry_after,
        )
        langfuse_tracing.finish_trace(ctx, None)
        _record_outcome(ctx, _request_start, "429")
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": f"Rate limit exceeded: {exc.limit_type} for {exc.scope}",
                    "type": "rate_limit_exceeded",
                    "code": "rate_limit_exceeded",
                }
            },
            headers={"Retry-After": str(exc.retry_after)},
        )

    # Short-circuit: bypass or cache hit
    if ctx.bypassed or ctx.cache_hit:
        response = ctx.cache_response or {}
        response.setdefault("_token_opt", {}).update(ctx.savings.to_langfuse_metadata())
        langfuse_tracing.finish_trace(ctx, response)
        _record_outcome(ctx, _request_start, "200", response)  # C1: bill cache hit / bypass
        return JSONResponse(content=response)

    # Batch deferred — response delivered async
    if ctx.batch_deferred:
        langfuse_tracing.finish_trace(ctx, None)
        # C1b: a batched request is billable at defer — it was accepted and will be
        # served async. ctx/savings exist here; the result-serve endpoint has no ctx.
        # request_id UNIQUE keeps it single even if the client polls the result.
        _schedule_billing(ctx, ctx.cache_response or {})
        _record_outcome(ctx, _request_start, "202")
        return JSONResponse(
            status_code=202,
            content={"status": "queued", "request_id": request_id},
        )

    # G06 cascade-execution already produced the final answer by running the tier
    # cascade inline (its provider time is already accumulated in
    # ctx.llm_elapsed_ms). Return it directly — calling the LLM again here would
    # be a duplicate provider round-trip. Fix: ctx.cascade_response was set by
    # G06 "for direct return in main.py" but was never consumed, so cascade
    # requests paid for the cascade AND a second main call.
    if ctx.cascade_response is not None:
        logger.info(
            "[%s] Using G06 cascade result (model=%s); skipping duplicate main LLM call",
            request_id, ctx.routed_model,
        )
        ctx, response_dict = await _pipeline.process_response(ctx, ctx.cascade_response)
        return _served_response(ctx, response_dict, _request_start)

    # Resolve provider key for the routed model
    provider = _resolve_provider(ctx.routed_model, cfg)
    provider_key = get_llm_provider_key(provider)
    routed_adapter = get_adapter(ctx.routed_model, cfg.get("providers", []))
    # Providers using ambient / multi-field credentials (AWS SigV4 for Bedrock, Vertex ADC)
    # need no single bearer key — skip the guard for them (requires_api_key() == False).
    if not provider_key and routed_adapter.requires_api_key():
        _record_outcome(ctx, _request_start, "503")
        raise HTTPException(status_code=503, detail=f"Provider key unavailable for {provider}")

    # Build outgoing params, then strip reasoning-only params when the routed
    # model doesn't support them. The developer may have attached reasoning_effort
    # (valid for o4-mini), but a downgrade — G06 routing, or its disabled/no-tiers
    # fallback to default_model — can leave those params stranded on a non-reasoning
    # model (gpt-4o-mini), which LiteLLM rejects with UnsupportedParamsError → 502.
    # Provider-agnostic via the adapter (Standard-3: no hardcoded provider names).
    outgoing_params = {
        k: v for k, v in ctx.params.items()
        if not k.startswith("_") and not k.startswith("x_") and k not in _INTERNAL_PARAM_KEYS
    }
    if not routed_adapter.supports_reasoning(ctx.routed_model):
        for rk in routed_adapter.reasoning_param_keys():
            if outgoing_params.pop(rk, None) is not None:
                logger.debug(
                    "[%s] Stripped reasoning param '%s' — model %s does not support reasoning",
                    request_id, rk, ctx.routed_model,
                )

    # Flex / priority pricing: keep `service_tier` only for providers that accept it
    # (OpenAI). Anthropic/Gemini reject it → strip to avoid a 400. Provider-agnostic
    # via the adapter (Standard-3: no hardcoded provider names).
    if "service_tier" in outgoing_params and not routed_adapter.supports_service_tier():
        outgoing_params.pop("service_tier", None)
        logger.debug(
            "[%s] Stripped service_tier — %s does not support it", request_id, ctx.routed_model,
        )

    # Provider param hygiene: strip params this provider rejects (e.g. parallel_tool_calls /
    # logprobs on non-OpenAI, `thinking` on non-Anthropic). Explicit belt alongside the
    # global litellm.drop_params safety net. Provider-agnostic via the adapter.
    for _uk in routed_adapter.unsupported_params():
        if outgoing_params.pop(_uk, None) is not None:
            logger.debug(
                "[%s] Stripped unsupported param '%s' for %s", request_id, _uk, ctx.routed_model,
            )

    # Keep an injected reasoning/thinking budget below max_tokens — Anthropic 400s when
    # max_tokens <= thinking.budget_tokens (no-op for OpenAI's string reasoning_effort).
    outgoing_params = routed_adapter.cap_reasoning_params(
        outgoing_params, outgoing_params.get("max_tokens")
    )

    # Provider-native context editing (per-tenant opt-in; no-op for non-Anthropic
    # adapters, so this is safe to call unconditionally — Standard-3 compliant).
    outgoing_params = apply_context_management(
        outgoing_params, routed_adapter, cfg, ctx.tenant_id
    )

    # Resolve provider routing (model string + api_base / api_version / custom_llm_provider)
    # via the adapter so Azure, Bedrock, and OpenAI-compatible custom base URLs are
    # reachable — litellm's model-name heuristics alone can't reach them.
    _call_model, _call_kwargs = routed_adapter.build_call(
        ctx.routed_model,
        get_provider_entry(ctx.routed_model, cfg.get("providers", [])) or {},
        provider_key,
    )

    # Streaming pass-through: request-side optimisations are already applied; relay the
    # provider's SSE chunks unchanged and skip the response-side pipeline (G14/G18/G23).
    # Previously stream=true 502-crashed (.model_dump() on an async iterator).
    if outgoing_params.get("stream"):
        return _stream_response(
            ctx, _call_model, _call_kwargs, outgoing_params, request_id, _request_start
        )

    # Call LLM via LiteLLM
    _llm_start = time.time()
    try:
        logger.info("[%s] LLM call → %s starting", request_id, ctx.routed_model)
        llm_response = await litellm.acompletion(
            model=_call_model,
            messages=ctx.messages,
            **_call_kwargs,
            **outgoing_params,
        )
        _llm_ms = (time.time() - _llm_start) * 1000
        # += (not =) so any provider time already accumulated by middleware (G06
        # judge/cascade fallback, G10 summary, G09 schema) is preserved alongside
        # the main call — the SLA split needs the TOTAL provider time.
        ctx.llm_elapsed_ms += _llm_ms
        # Book the main provider completion as the 'LLM-call' pseudo-stage so the
        # Latency Breakup dashboard shows it alongside the G-group stages and a
        # normal request's per-step bars sum to end-to-end. G06-cascade requests
        # short-circuit the main call (their provider time is inside G06-routing),
        # so they simply record no LLM-call stage — no double counting.
        try:
            STAGE_DURATION_MS.labels(
                stage="LLM-call",
                tenant_id=getattr(ctx, "tenant_id", "default"),
            ).observe(_llm_ms)
        except Exception:  # never let metrics break the response
            pass
        (logger.warning if _llm_ms > 10000 else logger.info)(
            "[%s] LLM call %s completed in %.0fms", request_id, ctx.routed_model, _llm_ms
        )
        response_dict = llm_response.model_dump() if hasattr(llm_response, "model_dump") else dict(llm_response)
    except litellm.exceptions.AuthenticationError as exc:
        logger.error("LLM auth error: %s", exc)
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _record_outcome(ctx, _request_start, "401")
        raise HTTPException(status_code=401, detail="LLM provider authentication failed")
    except litellm.exceptions.RateLimitError as exc:
        logger.warning("LLM rate limit: %s", exc)
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _record_outcome(ctx, _request_start, "429")
        raise HTTPException(status_code=429, detail="LLM provider rate limit reached")
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _record_outcome(ctx, _request_start, "502")
        raise HTTPException(status_code=502, detail=f"LLM provider error: {str(exc)}")

    # Run G14-G18 response pipeline, then finalise (savings metadata, headers,
    # SLA metrics + billing) via the shared helper.
    ctx, response_dict = await _pipeline.process_response(ctx, response_dict)
    return _served_response(ctx, response_dict, _request_start)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _authenticate(request: Request) -> tuple[str, Optional[str], Optional[dict]]:
    """
    Validate proxy API key, return (user_id, api_key, tenant_metadata). Raises 401 on failure.

    Optional: Accept X-User-ID header to override user_id for testing/tracking.
    Header is only accepted if the value matches the allowlist in config.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Use: Bearer <proxy-key>",
        )
    api_key = auth_header.removeprefix("Bearer ").strip()
    is_valid, user_id, tenant_metadata = validate_proxy_key(api_key)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid proxy API key. Contact your platform team for a key.",
        )

    # Suspended keys authenticate to a known tenant but are rejected here (403).
    # The suspended flag is set out-of-band by the key-store lifecycle; the proxy
    # only enforces it. Checked before any X-User-ID override so a suspended key
    # can never slip through the header path.
    if is_suspended(tenant_metadata):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key suspended. Contact your platform team.",
        )

    # Check for X-User-ID header override (for testing/tracking)
    cfg = get_config()
    header_override_enabled = cfg.get("proxy", {}).get("allow_user_id_header_override", False)
    if header_override_enabled:
        header_user_id = request.headers.get("X-User-ID") or request.headers.get("x-user-id")
        if header_user_id:
            # Validate against allowlist (supports wildcards, e.g., "pitch-*")
            allowed_patterns = cfg.get("proxy", {}).get("allowed_user_id_headers", [])
            if allowed_patterns:
                from fnmatch import fnmatch
                if any(fnmatch(header_user_id, pattern) for pattern in allowed_patterns):
                    logger.debug("User ID overridden by X-User-ID header: %s", header_user_id)
                    return header_user_id, api_key, tenant_metadata
                else:
                    logger.warning("X-User-ID header value '%s' not in allowlist, ignoring", header_user_id)
            else:
                # If no allowlist configured, accept any header value (use with caution)
                logger.debug("User ID overridden by X-User-ID header (no allowlist): %s", header_user_id)
                return header_user_id, api_key, tenant_metadata

    return user_id, api_key, tenant_metadata


def _caller_tenant_id(tenant_metadata: Optional[dict]) -> str:
    """Caller's own tenant from the validated key (default for legacy keys)."""
    if isinstance(tenant_metadata, dict):
        return tenant_metadata.get("tenant_id", "default")
    return "default"


def _require_admin(tenant_metadata: Optional[dict], action: str) -> None:
    """Raise 403 unless the caller's key carries the admin/impersonation scope (H1)."""
    if not is_admin_key(tenant_metadata):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin scope required for {action}",
        )


def _resolve_provider(model: str, cfg: Dict[str, Any]) -> str:
    """Map a model name to its provider via providers[].model_prefixes.

    Uses the SAME prefix (startswith) resolution as providers.get_provider_entry /
    get_adapter, so the key we fetch matches the adapter that will route the call.
    A substring match here is wrong: 'openrouter/openai/gpt-oss-120b:free' contains
    the 'openai'/'gpt' fragment and would mis-resolve to OpenAI (→ wrong/absent key).
    """
    entry = get_provider_entry(model, cfg.get("providers", []))
    if entry and entry.get("name"):
        return entry["name"]
    # Fall back to first configured provider
    providers = cfg.get("providers", [])
    if providers:
        logger.warning("No model_prefixes matched '%s' — falling back to first provider: %s", model, providers[0].get("name"))
        return providers[0].get("name", "")
    logger.error("No providers configured and no prefix match for model '%s'", model)
    return ""


if __name__ == "__main__":
    port = int(os.getenv("PORT", "4000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
