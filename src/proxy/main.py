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
import re
import time
import uuid
from contextlib import asynccontextmanager
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
from providers.key_resolver import resolve_provider_key, ProviderKeyError, ProviderKeyDecryptError
from providers.resilience import (
    ResilienceConfig,
    CallTarget,
    AllTargetsFailedError,
    BREAKER_STATE_CODE,
    call_with_resilience,
    get_resilience_store,
)
from middleware.g18_observability import (
    REQUEST_DURATION_MS,
    HTTP_REQUESTS,
    LLM_DURATION_MS,
    PROXY_OVERHEAD_MS,
    STAGE_DURATION_MS,
    CIRCUIT_BREAKER_STATE,
    FAILOVER_TOTAL,
)
from protocols import OPENAI, ANTHROPIC, GEMINI
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

# Extension point: layers that compose on top of the core app (e.g. commercial_app)
# register an async startup callable here instead of using @app.on_event — FastAPI
# ignores on_event handlers once a custom lifespan is set, so the lifespan below invokes
# these after core startup completes. Exceptions PROPAGATE (matching the old on_event
# semantics), so a commercial startup guard that raises still refuses to boot.
_startup_hooks: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle (replaces the deprecated @app.on_event hooks).

    The startup body references module globals defined further down (``_pipeline``,
    ``_init_openllmetry``, …) — that is fine, they are resolved when the server starts,
    long after the module has finished importing."""
    # ── startup ──
    load_config()
    start_hot_reload()
    # WS22 env invariant: provider credentials must reach this process ONLY as
    # LLM_KEY_<PROVIDER> (resolved through the BYOK seam). Litellm-native vars are
    # picked up by litellm directly, bypassing per-tenant key resolution — under a
    # multi-tenant deployment that silently bills the platform account.
    _native = [v for v in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "AWS_ACCESS_KEY_ID", "AZURE_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY",
    ) if os.getenv(v)]
    if _native:
        logger.warning(
            "SECURITY: litellm-native credential env var(s) present in the proxy "
            "environment: %s — these BYPASS per-tenant BYOK key resolution and can "
            "bill the platform account. Use LLM_KEY_<PROVIDER> instead and remove "
            "these from the container env.", ", ".join(_native))
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
    # Warm the G05 L2 semantic-cache embedding model. On a fresh container the
    # sentence-transformers model (bge-small) is downloaded + loaded on first use
    # (~18s), and because the loader is lock-guarded the whole first request burst
    # blocks behind that one cold load — spiking proxy-overhead p99 after every
    # deploy. Warming it here (in a worker thread, as a background task so it never
    # delays readiness) means the first real L2 lookup reuses the already-loaded,
    # memoised instance and the cold cost is paid off the request path.
    async def _warm_l2_embedding_model():
        g5 = (cfg.get("groups", {}) or {}).get("G5_cache", {}) or {}
        if not g5.get("enabled", True):
            return
        model_name = g5.get("l2_embedding_model", "BAAI/bge-small-en-v1.5")
        try:
            _t0 = time.time()
            from ml_models import get_sentence_transformer
            await asyncio.to_thread(get_sentence_transformer, model_name)
            logger.info(
                "G05 L2 embedding model warmed (%s) in %.0fms",
                model_name, (time.time() - _t0) * 1000,
            )
        except Exception as exc:
            logger.warning("G05 L2 embedding warmup failed (%s): %s", model_name, exc)
    asyncio.create_task(_warm_l2_embedding_model())
    # Initialise OpenLLMetry (OTLP auto-instrumentation for LLM SDKs)
    _init_openllmetry(cfg)
    # Ensure billing table exists and wire UsageMeter (idempotent DDL)
    global _usage_meter, _audit_logger
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
        # D1 fix: inject the pool into the pipeline so per-tenant config_overrides
        # (model prefs + G-group knobs from the portal) are actually applied at runtime.
        _pipeline.set_db_pool(pg)
        # Trust & Safety audit (G29/G30): core audit ENGINE writes PII-free security
        # rows into audit_events (idempotent DDL). The commercial Security tab reads
        # them; core just records. No commercial import — audit/log.py is a core engine.
        from audit.log import AuditLogger, ensure_audit_schema
        await ensure_audit_schema(pg)
        _audit_logger = AuditLogger(db_pool=pg)
        logger.info("Billing: usage_events table ready; tenant-config loader wired")
        # WS25: config-driven retention loop (default OFF — retention.enabled).
        from retention import run_retention_loop
        asyncio.create_task(run_retention_loop(lambda: pg, get_config))
    except Exception as exc:
        logger.warning("Billing: could not initialise usage_events: %s", exc)
    if not _METRICS_SCRAPE_TOKEN:
        logger.warning(
            "METRICS_SCRAPE_TOKEN is not set — /metrics is unauthenticated. Set it in "
            "production so per-tenant token/cost metrics are not world-readable."
        )
    logger.info("Token Optimisation Proxy started")

    # Run registered add-on startup hooks (e.g. commercial router mounting) after core
    # startup, so Redis/config/pipeline handles are ready. Exceptions propagate — a hook
    # that raises (e.g. the managed BYOK boot guard) must still refuse to start the app.
    for _hook in _startup_hooks:
        await _hook()

    yield

    # ── shutdown ──
    from cache.redis_pool import close_pool
    await close_pool()
    logger.info("Token Optimisation Proxy shut down")


app = FastAPI(
    title="Token Optimisation Proxy",
    description="LLM proxy implementing G0-G28 token optimisations (G26 reserved).",
    version="1.0.0",
    lifespan=lifespan,
)
# CORS: Restrict to specific origins in production via CORS_ORIGINS env var
# Format: comma-separated URLs, e.g., "https://myapp.com,https://myapp-staging.com"
# For local development, set to "http://localhost:3000,http://localhost:8080"
# WS25: DEFAULT-DENY — the old unconfigured fallback was "*", which is the wrong
# posture for a credentialed multi-tenant API. Browser cross-origin access now
# requires either CORS_ORIGINS or an explicit CORS_ALLOW_ALL=true (local dev only).
# Non-browser clients (SDKs, curl) are unaffected — CORS gates browsers only.
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if not cors_origins and os.getenv("CORS_ALLOW_ALL", "").strip().lower() in ("1", "true", "yes"):
    cors_origins = ["*"]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

_pipeline = OptimisationPipeline()


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

        # WS21: tool-call recency is recorded per tenant (t:<id>:tok_opt:tool_calls).
        # This admin view unions all tenants' zsets (+ the legacy global key for
        # continuity with rows recorded before the isolation fix).
        zkeys = ["tok_opt:tool_calls"]
        async for k in redis.scan_iter(match="t:*:tok_opt:tool_calls", count=500):
            zkeys.append(k.decode() if isinstance(k, (bytes, bytearray)) else k)
        recent, all_tools = set(), set()
        for zk in zkeys:
            recent.update(await redis.zrangebyscore(zk, cutoff, "+inf"))
            all_tools.update(await redis.zrange(zk, 0, -1))
        stale = sorted(all_tools - recent)

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
        "tenant_id": "NOVA-STG-01",      # optional filter
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

# Registry of tenants that own doc buckets. The webhook caller is GCS/Pub-Sub (not the
# tenant), so tenant identity is reverse-derived from the bucket name via this registry.
# Cached briefly to avoid a DB hit per notification. `configured` distinguishes "no registry
# (single-tenant/local, DATABASE_URL unset)" from "registry configured but currently empty"
# — the caller MUST fail closed in the latter (multi-tenant mode) instead of defaulting.
_INGEST_REGISTRY_CACHE: dict = {"configured": False, "tenants": [], "ts": 0.0, "valid": False}
_INGEST_REGISTRY_TTL = float(os.getenv("INGEST_REGISTRY_TTL_SECONDS", "60"))


async def _ingest_tenant_registry() -> tuple[bool, list]:
    """Return (registry_configured, tenant_ids). Cached for INGEST_REGISTRY_TTL_SECONDS.

    ``registry_configured`` is True whenever DATABASE_URL is set (multi-tenant mode), even if
    the tenant list is momentarily empty — so the caller can fail closed rather than fall open
    to tenant_id="default". A transient DB error keeps the mode as configured with an empty
    list (still fail-closed) rather than crashing the webhook.
    """
    now = time.monotonic()
    # Serve from cache when a prior successful load is still fresh — including an empty result
    # (avoids re-querying every request in the empty-but-configured window / thundering herd).
    if _INGEST_REGISTRY_CACHE["valid"] and (now - _INGEST_REGISTRY_CACHE["ts"]) < _INGEST_REGISTRY_TTL:
        return _INGEST_REGISTRY_CACHE["configured"], _INGEST_REGISTRY_CACHE["tenants"]
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        # Single-tenant / local: no registry at all → caller uses the derived slug/default.
        _INGEST_REGISTRY_CACHE.update(configured=False, tenants=[], ts=now, valid=True)
        return False, []
    try:
        from cache.pg_pool import get_pg_pool
        pool = await get_pg_pool(db_url)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT tenant_id FROM portal_users WHERE tenant_id IS NOT NULL")
        tenants = [r["tenant_id"] for r in rows]
        _INGEST_REGISTRY_CACHE.update(configured=True, tenants=tenants, ts=now, valid=True)
        return True, tenants
    except Exception as exc:
        # DATABASE_URL is set (multi-tenant intent) but the query failed (e.g. portal_users
        # not created in an OSS-only deploy, or a transient DB error). Stay CONFIGURED with an
        # empty list so the caller fails closed (403) rather than falling open to "default".
        # Do NOT cache a failure — recover on the next call.
        logger.warning("ingest registry query failed (failing closed): %s", exc)
        return True, []


def _verify_ingest_oidc(request: Request) -> None:
    """Verify the Pub/Sub push OIDC token, gated by INGEST_REQUIRE_OIDC.

    Local/self-host (INGEST_REQUIRE_OIDC=false, default) skips this so the existing
    flat-payload curl flow and tests keep working. Managed GCP sets it true so only the
    push SA can drive ingestion. 401 on any failure.
    """
    if os.getenv("INGEST_REQUIRE_OIDC", "false").lower() != "true":
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing OIDC token")
    token = auth.removeprefix("Bearer ").strip()
    expected_sa = os.getenv("INGEST_PUSH_SA_EMAIL", "")
    audience = os.getenv("INGEST_OIDC_AUDIENCE", "")
    try:
        from google.oauth2 import id_token as _id_token
        from google.auth.transport import requests as _ga_requests
        claims = _id_token.verify_oauth2_token(
            token, _ga_requests.Request(), audience=audience or None
        )
    except Exception as exc:
        logger.warning("Rejected /ingest-doc: OIDC verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid OIDC token")
    if expected_sa and claims.get("email") != expected_sa:
        logger.warning("Rejected /ingest-doc: OIDC email %r != expected push SA", claims.get("email"))
        raise HTTPException(status_code=401, detail="OIDC token not from the ingest push SA")


def _parse_ingest_payload(payload: dict) -> tuple[str, str]:
    """Extract (bucket, object) from either a Pub/Sub push envelope or a flat body.

    Real GCS notifications arrive as {"message": {"data": "<base64 GCS resource>",
    "attributes": {...}}}; local/tests may post a flat {"bucket","name"}. Prefer the
    message attributes (bucketId/objectId) when present.
    """
    msg = payload.get("message")
    if isinstance(msg, dict):
        attrs = msg.get("attributes") or {}
        bucket = attrs.get("bucketId", "")
        obj = attrs.get("objectId", "")
        if not (bucket and obj):
            data = msg.get("data")
            if data:
                try:
                    import base64
                    resource = json.loads(base64.b64decode(data).decode("utf-8"))
                    bucket = bucket or resource.get("bucket", "")
                    obj = obj or resource.get("name", "")
                except Exception as exc:
                    logger.warning("/ingest-doc: could not decode Pub/Sub message.data: %s", exc)
        return bucket, obj
    return payload.get("bucket", ""), payload.get("name", "")


@app.post("/ingest-doc")
async def ingest_doc(request: Request):
    """GCS pub/sub push notification → triggers G03 doc pipeline job.

    Hardened (data-safety): verifies the push OIDC token, parses the Pub/Sub envelope,
    and reverse-derives the tenant from the (per-tenant) bucket name via the registry.
    An unregistered bucket is refused with 403 so a doc can never be ingested into a
    tenant it doesn't belong to.
    """
    _verify_ingest_oidc(request)
    payload = await request.json()
    bucket, obj = _parse_ingest_payload(payload)
    if not (bucket and obj):
        raise HTTPException(status_code=400, detail="Missing bucket or name in payload")

    from tenancy.context import bucket_to_tenant, sanitise_tenant_id
    registry_configured, registry = await _ingest_tenant_registry()
    if registry_configured:
        # Multi-tenant mode: the bucket MUST reverse-derive to a registered tenant. This is
        # fail-closed even when the registry is momentarily empty (fresh deploy before the
        # first signup) — never fall open to "default", which would ingest into the shared
        # collection or misattribute a doc to the wrong tenant.
        tenant_id = bucket_to_tenant(bucket, registry)
        if tenant_id is None:
            logger.warning("Rejected /ingest-doc: bucket %r not registered to any tenant", bucket)
            raise HTTPException(status_code=403, detail="Bucket not registered to any tenant")
    else:
        # Single-tenant / local (no registry configured at all): fall back to the default so
        # the existing flat-payload / self-host flow keeps working.
        tenant_id = "default"

    success = await trigger_doc_ingestion(bucket, obj, tenant_id=tenant_id)
    return {"triggered": success, "object": obj, "tenant_id": sanitise_tenant_id(tenant_id)}


# ---------------------------------------------------------------------------
# Main proxy endpoint — OpenAI-compatible
# ---------------------------------------------------------------------------

# Body params consumed by middleware for routing/retrieval/loop-control —
# not recognized by the provider's completion API and must not be forwarded.
_INTERNAL_PARAM_KEYS = {"template_id", "workflow_id", "rag_query", "batch_id", "burst_block"}
_usage_meter = None  # initialised in the lifespan startup once pg pool is ready
_audit_logger = None  # core AuditLogger for G29/G30 security events (lifespan-wired)


def _schedule_security_audit(ctx) -> None:
    """Fire-and-forget one or two PII-free ``audit_events`` rows for any G29 redaction /
    G30 guardrail activity on this request. No-op without ctx / a wired audit logger /
    a running loop, and skips the task entirely when nothing was flagged. Best-effort:
    audit must never block or break the response path."""
    if ctx is None or _audit_logger is None:
        return
    if not (getattr(ctx, "guardrail_action", None) or getattr(ctx, "pii_action", None)):
        return
    try:
        asyncio.create_task(_audit_logger.log_security_events(ctx))
    except RuntimeError:  # no running loop (not the request path) — skip
        logger.debug("[%s] security audit skipped: no loop", getattr(ctx, "request_id", "?"))


def _persist_all_outcomes() -> bool:
    """C2 config gate — persist observability-only rows for non-2xx outcomes so the
    in-dashboard error-rate / latency panels have data. Default true; disable via
    ``billing.metering.persist_all_outcomes: false`` to restore the old 2xx-only write."""
    try:
        billing = (get_config().get("billing") or {}).get("metering") or {}
        return bool(billing.get("persist_all_outcomes", True))
    except Exception:
        return True


def _schedule_billing(
    ctx, response, *, status_code: int = 200, billable: bool = True,
    total_duration_ms: int = 0, llm_duration_ms: int = 0,
) -> None:
    """Fire-and-forget exactly one ``usage_events`` row for a served request.
    Idempotent at the DB (request_id UNIQUE + ON CONFLICT DO NOTHING). No-op when
    billing isn't wired (no UsageMeter / no DB pool) or there is no running event loop.

    C2: ``billable`` marks a billable 2xx unit; non-billable rows (errors) are still
    persisted so the reliability/latency analytics have data, but are excluded from the
    request-count invoice (invoice/quota SQL filters ``WHERE billable``) and from the
    OpenMeter push. ``status_code`` + latencies feed the in-dashboard SLA panels."""
    if ctx is None or response is None or _usage_meter is None:
        return
    try:
        asyncio.create_task(_usage_meter.record(
            ctx, response,
            status_code=status_code, billable=billable,
            total_duration_ms=total_duration_ms, llm_duration_ms=llm_duration_ms,
        ))
    except RuntimeError:  # no running loop (not the request path) — skip billing
        logger.debug("[%s] billing skipped: no running event loop", getattr(ctx, "request_id", "?"))


# Item 12 — secret redaction for log lines built from upstream exceptions. A provider
# error string can embed the Authorization header / api_key / base_url or a raw sk-/tok-
# credential; strip those before logging, and never echo the raw exception to the client.
_SECRET_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|access[_-]?token|secret|password|base[_-]?url)\b"
    r"\s*[=:]\s*\S+"
)
_SECRET_TOKEN_RE = re.compile(r"\b(sk|tok|rk|pk)-[A-Za-z0-9_\-]{6,}", re.IGNORECASE)


def _redact_secrets(text: Any) -> str:
    """Best-effort scrub of credentials from a string before it reaches a log sink."""
    try:
        s = str(text)
    except Exception:
        return "<unprintable>"
    s = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=<redacted>", s)
    s = _SECRET_TOKEN_RE.sub(lambda m: f"{m.group(1)}-<redacted>", s)
    return s


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
    # Trust & Safety: record any G29/G30 activity at every exit path (a flagged
    # request that later errors is still audited). PII-free; best-effort.
    _schedule_security_audit(ctx)
    # C1/C2: persist exactly one usage_events row per outcome. 2xx = billable unit
    # (billed + quota/spend bumped); non-2xx = observability-only row (billable=false,
    # excluded from invoices) so the reliability/latency panels have error data. The 202
    # batch-defer path bills separately at defer, so skip its row here to avoid a
    # non-billable row racing the billable one (ON CONFLICT would drop the billable insert).
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    billable = status == "200"
    if billable:
        _schedule_billing(
            ctx, response if response is not None else {},
            status_code=status_code, billable=True,
            total_duration_ms=int(elapsed_ms), llm_duration_ms=int(llm_ms),
        )
        _bump_quota_counter(ctx)
        _bump_spend_counter(ctx)
    elif status_code != 202 and _persist_all_outcomes():
        # Observability-only error row. ctx may be None for very-early failures — skip then.
        _schedule_billing(
            ctx, {},
            status_code=status_code, billable=False,
            total_duration_ms=int(elapsed_ms), llm_duration_ms=int(llm_ms),
        )


def _bump_quota_counter(ctx) -> None:
    """WS23: fire-and-forget monthly billable-request counter (G00 quota gate reads it).

    Mirrors the billable unit exactly — bumped for every served 2xx, tenant-prefixed
    (`t:<id>:quota:<YYYYMM>`), ~40-day TTL so last month's key self-expires."""
    if ctx is None:
        return
    prefix = getattr(ctx, "redis_prefix", None) or f"t:{getattr(ctx, 'tenant_id', 'default')}:"

    async def _bump() -> None:
        try:
            from cache.redis_pool import get_redis
            from middleware.g00_rate_limit import G00RateLimit
            key = G00RateLimit.quota_key(prefix)
            r = get_redis()
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, 40 * 86400)
        except Exception as exc:
            logger.debug("quota counter bump failed: %s", exc)

    try:
        asyncio.create_task(_bump())
    except RuntimeError:
        pass


def _bump_spend_counter(ctx) -> None:
    """Fire-and-forget monthly running-USD spend counter (G00 spend-cap gate reads it).

    Bumped for every served 2xx by the request's REAL ``cost_actual_usd`` (set by
    G18 from the provider's actual token usage), tenant-prefixed
    (`t:<id>:spend:<YYYYMM>`), ~40-day TTL so last month's key self-expires. A
    zero/absent cost (e.g. a bypass that never reached the LLM) is skipped."""
    if ctx is None:
        return
    try:
        cost = float(getattr(getattr(ctx, "savings", None), "cost_actual_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        return
    prefix = getattr(ctx, "redis_prefix", None) or f"t:{getattr(ctx, 'tenant_id', 'default')}:"

    async def _bump() -> None:
        try:
            from cache.redis_pool import get_redis
            from middleware.g00_rate_limit import G00RateLimit
            key = G00RateLimit.spend_key(prefix)
            r = get_redis()
            total = await r.incrbyfloat(key, cost)
            # Set the TTL once, on first accrual for the period.
            if float(total) <= cost + 1e-9:
                await r.expire(key, 40 * 86400)
        except Exception as exc:
            logger.debug("spend counter bump failed: %s", exc)

    try:
        asyncio.create_task(_bump())
    except RuntimeError:
        pass


def _stream_response(ctx, call_model, call_kwargs, outgoing_params, request_id, request_start,
                     *, eff_cfg=None, provider="", routed_adapter=None, provider_key=None):
    """Pass-through SSE streaming: relay the provider's chunks unchanged.

    Request-side optimisations (G0–G13) are already applied before this is called. The
    response-side pipeline (G14/G18/G23) is intentionally skipped for streamed calls;
    usage is captured best-effort from the final chunk (needs provider stream-usage
    support) and billing fires once on completion (billing is per-request).

    Resilience (#1): the stream is established through call_with_resilience — a transient
    error establishing the primary stream retries then fails over to a configured fallback
    provider BEFORE any bytes are sent. Once chunks are flowing we can't fail over (the SSE
    response has begun), so an error mid-stream surfaces as a data:{error} event as before.

    Billing/SLA truth (review S1): a stream that fails before ANY chunk was produced is
    recorded with the real error status (429/502) — never as a served 200 — so it neither
    bills the tenant nor pollutes success metrics. A mid-stream error after content was
    delivered still records 200 (a partial response was served).
    """
    import json as _json

    params = dict(outgoing_params)
    # Ask for a usage chunk where the provider supports it; litellm.drop_params removes it
    # for providers that don't.
    params.setdefault("stream_options", {"include_usage": True})

    _rcfg = ResilienceConfig.resolve(eff_cfg or ctx.config or {}, provider)

    async def _establish_primary():
        return await litellm.acompletion(
            model=call_model, messages=ctx.messages, **call_kwargs, **params
        )

    async def _open_stream():
        targets = [CallTarget(
            model=ctx.routed_model, provider=provider, invoke=_establish_primary,
            has_key=bool(provider_key) or (routed_adapter is not None
                                           and not routed_adapter.requires_api_key()),
            adapter=routed_adapter,
        )]
        targets += _fallback_targets(
            ctx, _rcfg, eff_cfg or ctx.config or {}, request_id, stream=True
        )
        return await call_with_resilience(
            targets, get_resilience_store(), _rcfg,
            redis_prefix=ctx.redis_prefix, attempts_sink=ctx.provider_attempts,
            on_success=_make_pin_winner(ctx, request_id, "stream failover"),
        )

    async def event_gen():
        last_usage = {}
        parts = []  # accumulated assistant text for chunk-aware G23 (measurement only)
        served = False   # True once any chunk reached the client (billing/SLA truth)
        fail_status = "502"
        _llm_start = time.time()
        try:
            stream = await _open_stream()
            async for chunk in stream:
                cd = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                served = True
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
            # Item 12 (review S2): redact the log line and NEVER echo the raw upstream
            # exception to the client — litellm reprs can embed base_url/key material.
            logger.error("[%s] streaming LLM call failed: %s", request_id, _redact_secrets(exc))
            _rl = isinstance(exc, litellm.exceptions.RateLimitError) or (
                isinstance(exc, AllTargetsFailedError)
                and isinstance(exc.last_error, litellm.exceptions.RateLimitError)
            )
            fail_status = "429" if _rl else "502"
            _client_msg = (
                "LLM provider rate limit reached" if _rl
                else "LLM provider error (upstream call failed)"
            )
            yield f"data: {_json.dumps({'error': _client_msg})}\n\n"
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
                _emit_resilience_metrics(ctx)
                # Served-anything → 200 (bills; partial content counts as served).
                # Failed before the first chunk → real error status, not billed.
                _record_outcome(
                    ctx, request_start, "200" if served else fail_status,
                    {"usage": last_usage} if last_usage else None,
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
    messages, model, params = OPENAI.parse_request(body, dict(request.headers))
    return await _serve_core(
        request, request_id, _request_start, messages, model, params,
        user_id, api_key, tenant_metadata, OPENAI.name,
    )


async def _serve_core(
    request: Request, request_id: str, _request_start: float,
    messages: list, model: str, params: dict, user_id: str, api_key: str,
    tenant_metadata, ingress_protocol: str,
):
    """Shared request core — OpenAI-shaped in, OpenAI-shaped Response out.

    The protocol routes (OpenAI / Anthropic / Gemini) parse their wire format into
    (messages, model, params) and call this; the non-OpenAI routes then translate the
    returned Response back to their protocol (#4). All optimisation, resilience, and
    billing live here, protocol-agnostic; ``ctx.ingress_protocol`` flows to the usage row.
    """
    # Client omitted `model` → flag it so the pipeline resolves the tenant default, then
    # apply the global placeholder so RequestContext.create has a concrete model.
    params["_model_defaulted"] = not bool(model)
    if not model:
        model = get_fallback_request_model()

    # Map X-* proxy headers into params (X-Template-ID → x_template_id, etc.).
    # x-api-key / x-goog-api-key carry the native-SDK PROXY CREDENTIAL (#4) — they must
    # never enter ctx.params (which G13 persists to the Redis batch stream), so exclude
    # them alongside the routing headers. Auth reads them directly in _authenticate.
    for header_name, header_value in request.headers.items():
        lower = header_name.lower()
        if lower.startswith("x-") and lower not in (
            "x-user-id", "x-scenario-tag", "x-api-key", "x-goog-api-key",
        ):
            params[lower.replace("-", "_")] = header_value

    # G7 RAG: derive rag_query from the last user message when x_rag_collection is set
    if "x_rag_collection" in params and "rag_query" not in params:
        for msg in reversed(messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                params["rag_query"] = msg.get("content", "")
                break

    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    params["_api_key_hash"] = api_key_hash
    # The validated key is the authoritative tenant identity (C1); the pipeline reads
    # these _auth_* params and only honours X-Tenant-ID for admin keys.
    if isinstance(tenant_metadata, dict):
        params["_auth_tenant_id"] = tenant_metadata.get("tenant_id")
        params["_auth_tier"] = tenant_metadata.get("tier", "free")
        params["_auth_admin"] = bool(tenant_metadata.get("admin", False))

    cfg = get_config()
    ctx = RequestContext.create(
        request_id=request_id, user_id=user_id, messages=messages,
        model=model, params=params, config=cfg,
    )
    ctx.ingress_protocol = ingress_protocol

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
                    # quota_exceeded (monthly cap) vs rate_limit_exceeded (rps/rph)
                    # surface distinctly so clients can react appropriately (WS23).
                    "message": (
                        f"Monthly request quota exceeded ({exc.scope}). "
                        "Upgrade your plan or wait for the next billing period."
                        if exc.limit_type == "quota_exceeded"
                        else f"Monthly spend cap exceeded ({exc.scope}). "
                        "Raise the cap in the portal or wait for the next billing period."
                        if exc.limit_type == "spend_cap_exceeded"
                        else f"Rate limit exceeded: {exc.limit_type} for {exc.scope}"
                    ),
                    "type": "rate_limit_exceeded",
                    "code": (
                        exc.limit_type
                        if exc.limit_type in ("quota_exceeded", "spend_cap_exceeded")
                        else "rate_limit_exceeded"
                    ),
                }
            },
            headers={"Retry-After": str(exc.retry_after)},
        )

    # Trust & Safety block (G30 injection guardrail / G29 PII policy) — a served
    # content-filter refusal (HTTP 200, finish_reason="content_filter"). Billed once
    # like a bypass: it is a served proxy decision, and billing it closes the
    # free-abuse vector (spamming blocked prompts must not be free). Checked before
    # bypass/cache because a blocked request must never be served from cache.
    if ctx.security_blocked and ctx.security_block_response is not None:
        response = ctx.security_block_response
        response.setdefault("_token_opt", {}).update(ctx.savings.to_langfuse_metadata())
        langfuse_tracing.finish_trace(ctx, response)
        _record_outcome(ctx, _request_start, "200", response)
        return JSONResponse(content=response)

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
        _schedule_billing(ctx, ctx.cache_response or {}, status_code=202, billable=True)
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

    # Split-brain fix: resolve provider/adapter/entry from the TENANT-merged ctx.config
    # (the pipeline made it per-tenant), not the global cfg snapshot.
    eff_cfg = ctx.config or cfg

    # Resolve the provider key via the BYOK seam (tenant key when configured; strict
    # tenants without one raise ProviderKeyError → 402). Core default = global platform key.
    provider = _resolve_provider(ctx.routed_model, eff_cfg)
    try:
        provider_key = await resolve_provider_key(provider, ctx.tenant_id, ctx)
    except ProviderKeyDecryptError as exc:
        # Fail closed: a stored key was found but could not be decrypted. This is a
        # distinct condition from "no key" — surface it as a 502 so the tenant is not
        # told to "add a key" they already added, and we never fall back to the
        # platform key. Caught BEFORE ProviderKeyError (its subclass).
        langfuse_tracing.finish_trace(ctx, None)
        _record_outcome(ctx, _request_start, "502")
        return JSONResponse(
            status_code=502,
            content={"error": {
                "message": exc.public_message,
                "type": "invalid_request_error",
                "code": "provider_key_undecryptable",
                "param": "model",
            }},
        )
    except ProviderKeyError as exc:
        langfuse_tracing.finish_trace(ctx, None)
        _record_outcome(ctx, _request_start, "402")
        return JSONResponse(
            status_code=402,
            content={"error": {
                "message": exc.public_message,
                "type": "invalid_request_error",
                "code": "provider_key_missing",
                "param": "model",
            }},
        )
    routed_adapter = get_adapter(ctx.routed_model, eff_cfg.get("providers", []))
    # Providers using ambient / multi-field credentials (AWS SigV4 for Bedrock, Vertex ADC)
    # need no single bearer key — skip the guard for them (requires_api_key() == False).
    if not provider_key and routed_adapter.requires_api_key():
        _record_outcome(ctx, _request_start, "503")
        raise HTTPException(status_code=503, detail=f"Provider key unavailable for {provider}")

    # Provider param hygiene — reasoning-only params stripped for non-reasoning routed
    # models, service_tier only where accepted, adapter unsupported_params, thinking-budget
    # cap, native context editing. Single shared contract with failover targets (review
    # K4): _outgoing_params_for is the one source of truth. Provider-agnostic via the
    # adapter (Standard-3: no hardcoded provider names).
    outgoing_params = _outgoing_params_for(
        ctx, routed_adapter, ctx.routed_model, eff_cfg, request_id
    )

    # Resolve provider routing (model string + api_base / api_version / custom_llm_provider)
    # via the adapter so Azure, Bedrock, and OpenAI-compatible custom base URLs are
    # reachable — litellm's model-name heuristics alone can't reach them.
    _call_model, _call_kwargs = routed_adapter.build_call(
        ctx.routed_model,
        get_provider_entry(ctx.routed_model, eff_cfg.get("providers", [])) or {},
        provider_key,
    )

    # Streaming pass-through: request-side optimisations are already applied; relay the
    # provider's SSE chunks unchanged and skip the response-side pipeline (G14/G18/G23).
    # Previously stream=true 502-crashed (.model_dump() on an async iterator).
    if outgoing_params.get("stream"):
        return _stream_response(
            ctx, _call_model, _call_kwargs, outgoing_params, request_id, _request_start,
            eff_cfg=eff_cfg, provider=provider, routed_adapter=routed_adapter,
            provider_key=provider_key,
        )

    # Call LLM via LiteLLM, wrapped in the resilience layer (#1): circuit breaker +
    # retry on the routed model, then failover to any configured fallback providers.
    # Resolve resilience config for the primary provider (per-provider override merges
    # over the global `resilience:` block). When disabled or no fallbacks are configured
    # this is a single target and call_with_resilience performs exactly one attempt,
    # re-raising the provider's error unchanged — behaviour-preserving.
    _rcfg = ResilienceConfig.resolve(eff_cfg, provider)

    async def _invoke_primary():
        resp = await litellm.acompletion(
            model=_call_model, messages=ctx.messages, **_call_kwargs, **outgoing_params,
        )
        return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

    _targets = [CallTarget(
        model=ctx.routed_model, provider=provider, invoke=_invoke_primary,
        has_key=bool(provider_key) or not routed_adapter.requires_api_key(),
        adapter=routed_adapter,
    )]
    _targets += _fallback_targets(ctx, _rcfg, eff_cfg, request_id)

    _llm_start = time.time()
    try:
        logger.info("[%s] LLM call → %s starting", request_id, ctx.routed_model)
        response_dict = await call_with_resilience(
            _targets, get_resilience_store(), _rcfg,
            redis_prefix=ctx.redis_prefix, attempts_sink=ctx.provider_attempts,
            on_success=_make_pin_winner(ctx, request_id),
        )
    except litellm.exceptions.AuthenticationError as exc:
        logger.error("LLM auth error: %s", _redact_secrets(exc))
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _emit_resilience_metrics(ctx)
        _record_outcome(ctx, _request_start, "401")
        raise HTTPException(status_code=401, detail="LLM provider authentication failed")
    except litellm.exceptions.RateLimitError as exc:
        logger.warning("LLM rate limit: %s", _redact_secrets(exc))
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _emit_resilience_metrics(ctx)
        _record_outcome(ctx, _request_start, "429")
        raise HTTPException(status_code=429, detail="LLM provider rate limit reached")
    except AllTargetsFailedError as exc:
        # Every target failed or was skipped. Map to the last real error's status so
        # the client sees a meaningful 429 vs 502, and log the (safe) attempts trail —
        # skip reasons included — rather than a bare exception (review C6).
        _last = exc.last_error
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _emit_resilience_metrics(ctx)
        logger.error("[%s] %s", request_id, exc)  # message embeds safe attempt descriptors
        if isinstance(_last, litellm.exceptions.RateLimitError):
            _record_outcome(ctx, _request_start, "429")
            raise HTTPException(status_code=429, detail="All providers rate limit reached")
        if _last is None:
            # No target was even attemptable (e.g. no viable key on any target) —
            # near-unreachable thanks to fail-open, but never report it as an
            # upstream failure that didn't happen.
            _record_outcome(ctx, _request_start, "503")
            raise HTTPException(status_code=503, detail="No provider available for this request")
        _record_outcome(ctx, _request_start, "502")
        raise HTTPException(status_code=502, detail="All providers failed (upstream error)")
    except Exception as exc:
        # Item 12: never echo the raw upstream exception to the client (it can embed the
        # api_key/base_url) and redact secrets from the log line too.
        logger.error("LLM call failed: %s", _redact_secrets(exc))
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _emit_resilience_metrics(ctx)
        _record_outcome(ctx, _request_start, "502")
        raise HTTPException(status_code=502, detail="LLM provider error (upstream call failed)")

    _llm_ms = (time.time() - _llm_start) * 1000
    # += (not =) so any provider time already accumulated by middleware (G06 judge/cascade
    # fallback, G10 summary, G09 schema) is preserved — the SLA split needs the TOTAL
    # provider time (including any failover retries).
    ctx.llm_elapsed_ms += _llm_ms
    try:
        STAGE_DURATION_MS.labels(
            stage="LLM-call", tenant_id=getattr(ctx, "tenant_id", "default"),
        ).observe(_llm_ms)
    except Exception:  # never let metrics break the response
        pass
    (logger.warning if _llm_ms > 10000 else logger.info)(
        "[%s] LLM call %s completed in %.0fms", request_id, ctx.routed_model, _llm_ms
    )
    _emit_resilience_metrics(ctx)

    # Run G14-G18 response pipeline, then finalise (savings metadata, headers,
    # SLA metrics + billing) via the shared helper.
    ctx, response_dict = await _pipeline.process_response(ctx, response_dict)
    return _served_response(ctx, response_dict, _request_start)


# ---------------------------------------------------------------------------
# Native multi-protocol ingress (#4) — Anthropic /v1/messages + Gemini generateContent.
# Each route parses its wire format into OpenAI shape, runs the shared _serve_core, then
# translates the OpenAI Response back to the caller's protocol. The OpenAI path above is
# untouched (identity), so its behaviour is unchanged.
# ---------------------------------------------------------------------------
def _detail_str(exc: HTTPException) -> str:
    d = getattr(exc, "detail", "")
    return d if isinstance(d, str) else str(d)


async def _translate_stream(translator, source_iter):
    """Wrap the OpenAI SSE body-iterator, re-emitting each chunk in the caller's protocol.

    Consuming ``source_iter`` also drives the underlying _stream_response generator to
    completion — so its billing/usage `finally` (with ctx.ingress_protocol stamped) still
    fires exactly once."""
    errored = False
    for line in translator.start():
        yield line
    async for raw in source_iter:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        for frame in text.split("\n\n"):
            frame = frame.strip()
            if not frame.startswith("data:"):
                continue
            payload = frame[len("data:"):].strip()
            if payload == "[DONE]":
                continue  # translator.finish() owns the terminal framing
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict) and "error" in obj and "choices" not in obj:
                msg = obj.get("error")
                for out in translator.error(msg if isinstance(msg, str) else "upstream error"):
                    yield out
                errored = True
                continue
            for out in translator.chunk(obj):
                yield out
    # A stream that failed emits its protocol error frame and STOPS — never a synthetic
    # success termination (finish() would fabricate a finishReason:STOP / message_stop that
    # masks the upstream failure as a clean completion).
    if not errored:
        for line in translator.finish():
            yield line


def _completion_to_stream_chunk(openai_body: Dict) -> Dict:
    """Reshape a full OpenAI completion into a single streaming-shaped chunk.

    ``message`` → ``delta`` on each choice so a StreamTranslator can emit it as one native
    streaming turn (finish_reason / usage stay on the choice/top level). Used when the
    pipeline short-circuits (cache hit / bypass / block) with a whole JSON body but the
    client asked for a stream — so the SSE contract still holds."""
    chunk = {k: v for k, v in openai_body.items() if k != "choices"}
    new_choices = []
    for ch in openai_body.get("choices") or []:
        nc = {k: v for k, v in ch.items() if k != "message"}
        delta = dict(ch.get("message") or {})
        # A completion's tool_calls carry no streaming `index`; the native-stream
        # translators accumulate tool-call fragments keyed by index, so stamp a distinct
        # one per call (without this, multiple cached tool_calls collapse into slot 0 on
        # the force-stream / cache-hit path). Copy each entry so the cached body is not mutated.
        tcs = delta.get("tool_calls")
        if tcs:
            delta["tool_calls"] = [{**tc, "index": tc.get("index", i)} for i, tc in enumerate(tcs)]
        nc["delta"] = delta
        new_choices.append(nc)
    chunk["choices"] = new_choices
    return chunk


async def _one_shot_stream(translator, chunk: Dict):
    """Drive a StreamTranslator over a single synthetic chunk (start → chunk → finish)."""
    for line in translator.start():
        yield line
    for out in translator.chunk(chunk):
        yield out
    for line in translator.finish():
        yield line


async def _translate_response(protocol, resp, *, want_stream: bool = False,
                              array_wrap: bool = False):
    """Translate an OpenAI-shaped Response (from _serve_core) into ``protocol``'s shape.

    ``want_stream`` — the client requested streaming but the pipeline may have short-
    circuited with a plain JSON body; synthesise a one-chunk native stream so the route's
    SSE contract holds even on a cache hit. ``array_wrap`` — Gemini non-SSE
    streamGenerateContent returns a JSON array of GenerateContentResponse."""
    if isinstance(resp, StreamingResponse):
        return StreamingResponse(
            _translate_stream(protocol.stream_translator(), resp.body_iterator),
            media_type=protocol.stream_media_type,
            status_code=resp.status_code,
        )
    # JSONResponse — re-serialise the OpenAI body. `_token_opt` (OpenAI-only) is dropped;
    # the x-* savings headers are preserved.
    try:
        openai_body = json.loads(bytes(resp.body).decode("utf-8"))
    except Exception:
        return resp
    if resp.status_code >= 400:
        err = openai_body.get("error") if isinstance(openai_body.get("error"), dict) else {}
        msg = err.get("message") or openai_body.get("detail") or "error"
        body, status = protocol.serialise_error(resp.status_code, msg, err.get("code") or "")
        # Preserve x-* AND Retry-After: native SDK backoff honours retry-after on 429s, so
        # dropping it turns a graceful throttle into an aggressive retry storm.
        err_headers = {k: v for k, v in dict(resp.headers or {}).items()
                       if k.lower().startswith("x-") or k.lower() == "retry-after"}
        return JSONResponse(status_code=status, content=body, headers=err_headers)
    # Non-completion control body (batch-defer 202, etc.) has no ``choices`` — pass it
    # through untranslated so the ``request_id`` needed to poll the result survives, rather
    # than fabricating an empty "successful" message from it (cross-protocol batch replay is
    # a documented follow-up).
    if resp.status_code == 202 or "choices" not in openai_body:
        return resp
    passthru = {k: v for k, v in dict(resp.headers or {}).items() if k.lower().startswith("x-")}
    if want_stream:
        return StreamingResponse(
            _one_shot_stream(protocol.stream_translator(),
                             _completion_to_stream_chunk(openai_body)),
            media_type=protocol.stream_media_type, headers=passthru,
        )
    body = protocol.serialise_response(openai_body)
    if array_wrap:
        body = [body]
    return JSONResponse(status_code=resp.status_code, content=body, headers=passthru)


async def _serve_protocol(request: Request, proto, *, path_model: str = "",
                          force_stream: bool = False, array_wrap: bool = False):
    """Shared entry for a non-OpenAI ingress route: authenticate → parse → _serve_core →
    translate. ``proto`` is the ingress adapter (its ``.name`` is the ingress protocol).
    Auth/parse/pipeline errors are serialised into the caller's protocol."""
    _request_start = time.time()
    request_id = str(uuid.uuid4())
    try:
        user_id, api_key, tenant_metadata = await _authenticate(request, proto)
        body = await request.json()
        messages, model, params = proto.parse_request(body, dict(request.headers), path_model)
        if force_stream:
            params["stream"] = True
    except HTTPException as exc:
        eb, st = proto.serialise_error(exc.status_code, _detail_str(exc))
        return JSONResponse(status_code=st, content=eb)
    except Exception as exc:
        logger.warning("[%s] %s ingress: bad request: %s", request_id, proto.name, _redact_secrets(exc))
        eb, st = proto.serialise_error(400, "Invalid request body")
        return JSONResponse(status_code=st, content=eb)
    # array_wrap serves non-streaming and boxes the result in a JSON array, so it never
    # wants a synthesised stream.
    want_stream = bool(params.get("stream")) and not array_wrap
    try:
        resp = await _serve_core(request, request_id, _request_start, messages, model, params,
                                 user_id, api_key, tenant_metadata, proto.name)
    except HTTPException as exc:
        eb, st = proto.serialise_error(exc.status_code, _detail_str(exc))
        return JSONResponse(status_code=st, content=eb)
    return await _translate_response(proto, resp, want_stream=want_stream, array_wrap=array_wrap)


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API ingress (Claude SDK / Claude Code point here one-line)."""
    return await _serve_protocol(request, ANTHROPIC)


@app.post("/v1beta/models/{model}:generateContent")
async def gemini_generate_content(request: Request, model: str):
    """Google Gemini generateContent ingress (non-streaming)."""
    return await _serve_protocol(request, GEMINI, path_model=model)


@app.post("/v1beta/models/{model}:streamGenerateContent")
async def gemini_stream_generate_content(request: Request, model: str):
    """Google Gemini streamGenerateContent ingress.

    Honours Gemini's wire contract: ``?alt=sse`` streams SSE frames; the default (no
    ``alt``) returns a JSON array of GenerateContentResponse objects (which non-SSE REST
    clients parse), served as a single-element array from the aggregated response."""
    if request.query_params.get("alt") == "sse":
        return await _serve_protocol(request, GEMINI, path_model=model, force_stream=True)
    return await _serve_protocol(request, GEMINI, path_model=model, array_wrap=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _authenticate(
    request: Request, proto=OPENAI,
) -> tuple[str, Optional[str], Optional[dict]]:
    """
    Validate proxy API key, return (user_id, api_key, tenant_metadata). Raises 401 on failure.

    Native-SDK credential channels are scoped to the ingress protocol that needs them (#4):
    ``proto`` declares its own ``credential_headers`` / ``credential_query_param``, so
    ``x-api-key`` (Anthropic) and ``?key=`` (Gemini — appears in URL logs) are accepted
    ONLY on those protocols' routes. Every other route defaults to ``OPENAI`` = Bearer-only.

    Optional: Accept X-User-ID header to override user_id for testing/tracking.
    Header is only accepted if the value matches the allowlist in config.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header.removeprefix("Bearer ").strip()
    else:
        # The proxy key rides in the protocol's native field (the tenant's BYOK provider
        # key is resolved server-side), so no provider secret ever transits the client.
        api_key = ""
        for hdr in getattr(proto, "credential_headers", ()):
            val = request.headers.get(hdr)
            if val:
                api_key = val.strip()
                break
        query_param = getattr(proto, "credential_query_param", "")
        if not api_key and query_param:
            api_key = (request.query_params.get(query_param) or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=("Missing proxy key. Use 'Authorization: Bearer <proxy-key>' "
                    "(or x-api-key / x-goog-api-key / ?key= for native Anthropic/Gemini SDKs)."),
        )
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

    # Check for X-User-ID header override (per-user attribution within a tenant)
    cfg = get_config()
    header_override_enabled = cfg.get("proxy", {}).get("allow_user_id_header_override", False)
    if header_override_enabled:
        header_user_id = request.headers.get("X-User-ID") or request.headers.get("x-user-id")
        if header_user_id:
            # WS25: allowlist = global config patterns + the tenant's own email domain
            # (owner_domain stamped into the key metadata at signup → "*@acme.com").
            # An EMPTY combined allowlist now REJECTS the override — accept-any allowed
            # attribution spoofing (any caller could book usage under any user_id).
            allowed_patterns = list(cfg.get("proxy", {}).get("allowed_user_id_headers", []) or [])
            if isinstance(tenant_metadata, dict) and tenant_metadata.get("owner_domain"):
                allowed_patterns.append(f"*@{tenant_metadata['owner_domain']}")
            if allowed_patterns:
                from fnmatch import fnmatch
                if any(fnmatch(header_user_id, pattern) for pattern in allowed_patterns):
                    logger.debug("User ID overridden by X-User-ID header: %s", header_user_id)
                    return header_user_id, api_key, tenant_metadata
                else:
                    logger.warning("X-User-ID header value '%s' not in allowlist, ignoring", header_user_id)
            else:
                logger.warning(
                    "X-User-ID override rejected: no allowlist configured "
                    "(set proxy.allowed_user_id_headers or an owner_domain on the key)")

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


def _outgoing_params_for(ctx, adapter, model: str, eff_cfg: Dict[str, Any],
                         request_id: str = "") -> Dict[str, Any]:
    """Build provider-hygienic outgoing params for `model` under `adapter`.

    THE single source of the provider param-hygiene contract — used by the primary
    path and every failover target alike (review K4: no divergent copies). Steps:
    strip internal keys, strip reasoning-only params on non-reasoning models, strip
    service_tier / unsupported params, cap the thinking budget, apply native context
    editing.
    """
    outgoing = {
        k: v for k, v in ctx.params.items()
        if not k.startswith("_") and not k.startswith("x_") and k not in _INTERNAL_PARAM_KEYS
    }
    if not adapter.supports_reasoning(model):
        for rk in adapter.reasoning_param_keys():
            if outgoing.pop(rk, None) is not None and request_id:
                logger.debug(
                    "[%s] Stripped reasoning param '%s' — model %s does not support reasoning",
                    request_id, rk, model,
                )
    if "service_tier" in outgoing and not adapter.supports_service_tier():
        outgoing.pop("service_tier", None)
        if request_id:
            logger.debug("[%s] Stripped service_tier — %s does not support it", request_id, model)
    for _uk in adapter.unsupported_params():
        if outgoing.pop(_uk, None) is not None and request_id:
            logger.debug("[%s] Stripped unsupported param '%s' for %s", request_id, _uk, model)
    outgoing = adapter.cap_reasoning_params(outgoing, outgoing.get("max_tokens"))
    outgoing = apply_context_management(outgoing, adapter, eff_cfg, ctx.tenant_id)
    return outgoing


# Provider-scoped request params injected by G21 for the PRIMARY provider — they must
# never leak onto a failover target routed to a different provider (review P1).
_PRIMARY_SCOPED_PARAMS = ("prompt_cache_key", "prompt_cache_retention")


def _sanitized_failover_messages(messages):
    """Copy `messages` with provider-specific annotations removed (review P2).

    G21's Anthropic marker opt-in writes ``cache_control`` INTO message dicts; a
    fallback routed to another provider must not carry them (OpenAI/Gemini 400 on
    unknown message fields, and a fallback 400 would abort the chain). Shallow-copies
    each message dict; content is shared (read-only downstream).
    """
    out = []
    for m in messages:
        if isinstance(m, dict) and "cache_control" in m:
            m = {k: v for k, v in m.items() if k != "cache_control"}
        out.append(m)
    return out


def _sanitized_failover_tools(tools):
    """Same scrub for tool definitions (G21 annotates tools[-1] with cache_control)."""
    if not isinstance(tools, list):
        return tools
    out = []
    for t in tools:
        if isinstance(t, dict) and "cache_control" in t:
            t = {k: v for k, v in t.items() if k != "cache_control"}
        out.append(t)
    return out


def _lazy_fallback_target(ctx, model: str, eff_cfg: Dict[str, Any], request_id: str,
                          *, stream: bool = False) -> CallTarget:
    """Build a LAZY failover target: nothing is resolved until it is actually attempted.

    Review P3: eager building cost a key resolution (potentially a blocking Secret
    Manager RPC, or a BYOK decrypt + `provider_key.used` audit row) per fallback on
    EVERY request even when the primary succeeded. Here resolution happens inside
    ``invoke`` — i.e. only after the primary has already failed. A missing/undecryptable
    tenant key raises ProviderKeyError inside invoke, which call_with_resilience treats
    as a non-retryable fallback error and simply moves to the next target (the
    'never spend another tenant's key' guarantee, enforced at resolution time).
    """
    provider = _resolve_provider(model, eff_cfg)
    target = CallTarget(model=model, provider=provider, invoke=None, has_key=True)

    async def _invoke():
        providers_cfg = eff_cfg.get("providers", [])
        adapter = get_adapter(model, providers_cfg)
        key = await resolve_provider_key(provider, ctx.tenant_id, ctx)  # may raise → next target
        if not key and adapter.requires_api_key():
            raise ProviderKeyError(provider, ctx.tenant_id)
        target.adapter = adapter  # pinned onto ctx by on_success for cost/provider attribution
        outgoing = _outgoing_params_for(ctx, adapter, model, eff_cfg, request_id)
        for pk in _PRIMARY_SCOPED_PARAMS:
            outgoing.pop(pk, None)
        if stream:
            outgoing.setdefault("stream_options", {"include_usage": True})
        call_model, call_kwargs = adapter.build_call(
            model, get_provider_entry(model, providers_cfg) or {}, key
        )
        resp = await litellm.acompletion(
            model=call_model,
            messages=_sanitized_failover_messages(ctx.messages),
            **call_kwargs,
            **{**outgoing, **({"tools": _sanitized_failover_tools(outgoing.get("tools"))}
                              if outgoing.get("tools") is not None else {})},
        )
        if stream:
            return resp  # the stream iterator; caller relays it
        return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

    target.invoke = _invoke
    return target


def _fallback_targets(ctx, rcfg, eff_cfg, request_id: str, *, stream: bool = False):
    """The (lazy) failover targets for ctx.routed_model per config — [] when disabled."""
    if not rcfg.enabled:
        return []
    targets = []
    for fb_model in rcfg.fallbacks.get(ctx.routed_model, []):
        if fb_model == ctx.routed_model:
            continue
        targets.append(_lazy_fallback_target(ctx, fb_model, eff_cfg, request_id, stream=stream))
    return targets


def _make_pin_winner(ctx, request_id: str, label: str = "failover"):
    """on_success callback: pin the winning target's model + adapter onto ctx so
    pricing (model-keyed), the usage_events.provider column, and the cache-discount
    multiplier all attribute to the provider that actually served."""
    def _pin(t):
        if t.model != ctx.routed_model:
            logger.info("[%s] %s: %s → %s", request_id, label, ctx.routed_model, t.model)
        ctx.routed_model = t.model
        if t.adapter is not None:
            ctx.provider_adapter = t.adapter
    return _pin


def _emit_resilience_metrics(ctx) -> None:
    """Emit the breaker-state gauge + failover counter from ctx.provider_attempts.

    Gauge is labelled by provider ONLY (the breaker is global — a tenant label would
    just fan identical state into tenants×providers stale series; review S4) and uses
    peek_provider_state so display never creates breakers nor fabricates probes.
    The failover counter fires whenever a non-first target served — including
    same-provider model fallbacks (review S5). Never raises.
    """
    attempts = getattr(ctx, "provider_attempts", None) or []
    if not attempts:
        return
    try:
        store = get_resilience_store()
        seen = set()
        for a in attempts:
            if a.provider and a.provider not in seen:
                seen.add(a.provider)
                CIRCUIT_BREAKER_STATE.labels(provider=a.provider).set(
                    BREAKER_STATE_CODE.get(store.peek_provider_state(a.provider), 0)
                )
        winner = next((a for a in attempts if a.outcome == "success"), None)
        first = attempts[0]
        if winner is not None and winner is not first:
            FAILOVER_TOTAL.labels(
                from_provider=first.provider or "unknown",
                to_provider=winner.provider or "unknown",
                reason=first.outcome or "unknown",
                tenant_id=getattr(ctx, "tenant_id", "default"),
            ).inc()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("resilience metrics emit failed: %s", exc)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "4000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
