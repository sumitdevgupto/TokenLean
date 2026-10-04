"""
Token Optimisation Proxy — main entry point.

Exposes an OpenAI-compatible /v1/chat/completions endpoint.
Developers swap only their base_url; all optimisations (G0-G28, G27 reserved) are transparent.

Authentication: Bearer <proxy-key>  (issued per developer/team, stored in Secret Manager)
                Developers NEVER receive LLM provider keys.
"""
import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple

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

from auth.api_key_manager import (
    validate_proxy_key, is_admin_key, is_gateway_key, is_suspended,
    is_contract_inactive, get_ip_allowlist, key_cached_and_fresh, key_store_backend_installed,
)
# Unused here; kept because the test fixtures patch main.get_llm_provider_key.
from auth.api_key_manager import get_llm_provider_key  # noqa: F401
from net.client_ip import ClientIPMiddleware, request_client_ip
from net.ip_allowlist import ip_allowed
from tracing.otel import TraceIdHeaderMiddleware
from config_loader import get_config, get_fallback_request_model, load_config, start_hot_reload
from tenancy.resolver import resolve_tenant
from providers import get_adapter, get_provider_entry
from providers.key_resolver import resolve_provider_key, ProviderKeyError, ProviderKeyDecryptError
from providers.resilience import (
    ResilienceConfig,
    CallTarget,
    AllTargetsFailedError,
    BREAKER_STATE_CODE,
    BreakerState,
    call_with_resilience,
    describe_error,
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
    MODEL_LOCKOUT_STATE,
    price_billed_call,
)
from protocols import OPENAI, ANTHROPIC, GEMINI
from protocols.base import UnsupportedRequestField, header_params
import events
from middleware import RequestContext, record_provider_call
from middleware.g00_rate_limit import RateLimitExceeded, add_to_spend, counter_prefix
from middleware.g03_doc_pipeline import trigger_doc_ingestion
# The SAME predicate G05 uses to refuse STORING an empty answer, reused here to refuse
# SERVING one that is already stored. One definition, so the two sides cannot drift.
# Public on purpose (backlog #59) — pinned by test_is_empty_answer_import_contract.
from middleware.g05_cache import is_empty_answer as _is_empty_cached_answer
from middleware.g13_batch import start_batch_consumer, start_batch_poller
from middleware.pipeline import OptimisationPipeline
from middleware import langfuse_tracing

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


# H2: bearer token guarding the /metrics scrape endpoint, which lists tenant ids with their
# token and cost figures. When unset the endpoint refuses every scrape, unless
# METRICS_ALLOW_UNAUTHENTICATED opens it (local dev, on a machine nobody else can reach).
_METRICS_SCRAPE_TOKEN = os.getenv("METRICS_SCRAPE_TOKEN", "")
_METRICS_ALLOW_UNAUTHENTICATED = _env_flag("METRICS_ALLOW_UNAUTHENTICATED")
# The token Alertmanager presents to /admin/alert-webhook (Terraform generates it). It opens
# that endpoint only; unset, the webhook takes an admin key as before.
_ALERT_WEBHOOK_TOKEN = os.getenv("ALERT_WEBHOOK_TOKEN", "")

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
    # required=True: refuse to start rather than serve on an empty config (no rate card,
    # spend cap or provider tiers). Hot reload keeps the last good config afterwards.
    load_config(required=True)
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
    _service_task(start_batch_consumer(cfg), "g13-batch-consumer")
    # Start G13 provider-native batch poller (no-op unless provider_native is on)
    _service_task(start_batch_poller(cfg), "g13-batch-poller")
    # G08's daily pruning of registry tools the model has stopped calling: report only
    # until pruning.dry_run_first is false, and nothing at all while pruning.enabled is off
    from middleware.g08_tool_loading import run_tool_pruning_loop
    _service_task(run_tool_pruning_loop(get_config), "g08-tool-pruning")
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
    _service_task(_warm_l2_embedding_model(), "g05-embedding-warmup")
    # OTel pipeline tracing, from the `tracing` block (and OTEL_EXPORTER_OTLP_ENDPOINT)
    from tracing import otel as _otel
    _otel.configure(cfg)
    # Initialise OpenLLMetry (OTLP auto-instrumentation for LLM SDKs)
    _init_openllmetry(cfg)
    # Billing, the security audit log and per-tenant settings. Before the startup hooks:
    # a managed deploy waits here for the database, so the hooks always get one.
    await _wire_database_at_startup()
    if not _METRICS_SCRAPE_TOKEN and _METRICS_ALLOW_UNAUTHENTICATED:
        logger.warning(
            "METRICS_ALLOW_UNAUTHENTICATED is on and METRICS_SCRAPE_TOKEN is not set — "
            "/metrics, with every tenant's token and cost figures, is open to anyone who can "
            "reach this port."
        )
    elif not _METRICS_SCRAPE_TOKEN:
        logger.warning(
            "METRICS_SCRAPE_TOKEN is not set — /metrics refuses every scrape. Set it (your "
            "Prometheus presents it), or METRICS_ALLOW_UNAUTHENTICATED=true on a machine "
            "nobody else can reach."
        )
    if _ingest_oidc_required() and not all(_ingest_oidc_settings()):
        logger.warning(
            "/ingest-doc requires a Pub/Sub OIDC token but INGEST_PUSH_SA_EMAIL and "
            "INGEST_OIDC_AUDIENCE are not both set, so it refuses every notification. Set "
            "them, or INGEST_REQUIRE_OIDC=false where nobody else can reach the proxy."
        )
    logger.info("Token Optimisation Proxy started")

    # Run registered add-on startup hooks (e.g. commercial router mounting) after core
    # startup, so Redis/config/pipeline handles are ready. Exceptions propagate — a hook
    # that raises (e.g. the managed BYOK boot guard) must still refuse to start the app.
    for _hook in _startup_hooks:
        await _hook()

    yield

    # ── shutdown ──
    # Billing rows and counter updates of the last requests first: they need the pools.
    await _drain_after_response()
    from cache.redis_pool import close_pool
    await close_pool()
    logger.info("Token Optimisation Proxy shut down")


app = FastAPI(
    title="Token Optimisation Proxy",
    description="LLM proxy implementing G0-G28 token optimisations (G27 reserved).",
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
# The caller's address, resolved once per request from the right of X-Forwarded-For (or a
# trusted forwarder's vouch), then the forwarding headers are dropped: see net/client_ip.py.
# The lambda reads get_config at call time, so a hot reload (or a test patch) applies.
app.add_middleware(ClientIPMiddleware, get_config=lambda: get_config())
# X-Trace-ID on every response to a pipeline request (tracing.propagate_trace_id_header).
app.add_middleware(TraceIdHeaderMiddleware)

_pipeline = OptimisationPipeline()


def _init_openllmetry(cfg: Dict[str, Any]) -> None:
    # The flag is read BEFORE the import. The import used to come first, so every start of
    # an image without `traceloop-sdk` (i.e. every image we build: it has never been a
    # dependency) logged "OpenLLMetry init failed: No module named 'traceloop'" while the
    # feature was switched off in every shipped config. That warning was then read as the
    # reason Langfuse traces were missing - it is unrelated (OpenLLMetry is OTLP
    # auto-instrumentation; Langfuse tracing is middleware/langfuse_tracing.py).
    g18 = ((cfg.get("groups") or {}).get("G18_observability") or {})
    if not g18.get("openllmetry_enabled", False):
        return
    try:
        from traceloop.sdk import Traceloop
    except ImportError:
        logger.warning(
            "OpenLLMetry is enabled (groups.G18_observability.openllmetry_enabled) but the "
            "'traceloop-sdk' package is not installed in this image - OTLP auto-instrumentation "
            "is OFF. Install traceloop-sdk, or set openllmetry_enabled: false. Langfuse "
            "tracing is separate and unaffected.")
        return
    try:
        endpoint = g18.get("openllmetry_endpoint", "")
        kwargs = {"app_name": "token-optimisation-proxy"}
        if endpoint:
            kwargs["api_endpoint"] = endpoint
        Traceloop.init(**kwargs)
        logger.info("OpenLLMetry initialised")
    except Exception as exc:
        logger.warning("OpenLLMetry init failed: %s", exc)


# ---------------------------------------------------------------------------
# Database wiring: billing, the security audit log and per-tenant settings
# ---------------------------------------------------------------------------

# Until these are wired, requests go unbilled (_schedule_billing), G29-G32 audit rows are
# dropped (_schedule_security_audit) and tenant settings made in the portal are ignored.
# Startup used to try once, so a database blip at start left an instance like that for
# its whole life.
_DB_RETRY_FIRST_S = 1.0
_DB_RETRY_MAX_S = 30.0
_db_expected = False    # DATABASE_URL was set at startup
_db_pool = None         # set once the database has answered
_db_retry_task = None   # a self-hosted proxy's background retry
_retention_task = None


def _db_not_wired() -> list:
    """The database-backed parts this instance is running without (reported by /health)."""
    if not _db_expected:
        return []
    return [part for part, wired in (("billing", _usage_meter is not None),
                                     ("audit", _audit_logger is not None),
                                     ("tenant_config", _db_pool is not None)) if not wired]


async def _wire_database(db_url: str) -> None:
    """Wire each part not wired yet. The parts are independent, so a failing schema step
    leaves the others wired. Raises, naming the parts that failed."""
    global _db_pool, _usage_meter, _audit_logger, _retention_task
    if _db_pool is None:
        from cache.pg_pool import get_pg_pool
        pg = await get_pg_pool(db_url)
        # D1 fix: inject the pool into the pipeline so per-tenant config_overrides
        # (model prefs + G-group knobs from the portal) are actually applied at runtime.
        _pipeline.set_db_pool(pg)
        _db_pool = pg
        # WS25: config-driven retention loop (default OFF — retention.enabled).
        from retention import run_retention_loop
        _retention_task = asyncio.create_task(run_retention_loop(lambda: pg, get_config))
    failed = []
    if _usage_meter is None:
        try:
            from billing.models import ensure_usage_events_schema
            from billing.metering import UsageMeter
            await ensure_usage_events_schema(_db_pool)
            _usage_meter = UsageMeter(db_pool=_db_pool)
        except Exception as exc:
            failed.append(f"billing: {exc}")
    if _audit_logger is None:
        try:
            # Trust & Safety audit (G29/G30): core audit ENGINE writes PII-free security
            # rows into audit_events (idempotent DDL). The commercial Security tab reads
            # them; core just records. No commercial import — audit/log.py is a core engine.
            from audit.log import AuditLogger, ensure_audit_schema
            await ensure_audit_schema(_db_pool)
            _audit_logger = AuditLogger(db_pool=_db_pool)
        except Exception as exc:
            failed.append(f"audit: {exc}")
    if failed:
        raise RuntimeError("; ".join(failed))


async def _try_wire_database(db_url: str) -> bool:
    try:
        await _wire_database(db_url)
    except Exception as exc:
        logger.warning("Database: %s not wired yet: %s", ", ".join(_db_not_wired()), exc)
        return False
    logger.info("Database: usage_events table ready; audit log and tenant settings wired")
    return True


async def _keep_wiring_database(db_url: str) -> None:
    """Retry until every part is wired: 1 s apart at first, doubling up to 30 s."""
    delay = _DB_RETRY_FIRST_S
    while True:
        await asyncio.sleep(delay)
        if await _try_wire_database(db_url):
            return
        delay = min(delay * 2, _DB_RETRY_MAX_S)


async def _wire_database_at_startup() -> None:
    """Wire the database-backed parts, or arrange for them to be wired.

    One attempt now. If it fails, a self-hosted proxy serves without the missing parts
    while a background task retries, and /health reports them. With MANAGED_DEPLOY=true
    the retries happen here instead, so startup, and the startup hooks after it, wait for
    the database. uvicorn opens its port only once startup has finished: no request reaches
    a managed instance that would serve it unbilled, unaudited or without tenant settings,
    and a deploy made during a database outage fails instead of replacing a working one."""
    global _db_expected, _db_retry_task
    db_url = os.getenv("DATABASE_URL", "")
    _db_expected = bool(db_url)
    if not db_url:
        logger.warning("DATABASE_URL not set: billing, the security audit log and "
                       "per-tenant settings are off")
        return
    if await _try_wire_database(db_url):
        return
    if os.getenv("MANAGED_DEPLOY", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning("MANAGED_DEPLOY: startup waits until the database is wired")
        await _keep_wiring_database(db_url)
    else:
        logger.warning("Serving without %s until the database is wired, retrying in the "
                       "background (/health reports degraded)", ", ".join(_db_not_wired()))
        _db_retry_task = asyncio.create_task(_keep_wiring_database(db_url))


# ---------------------------------------------------------------------------
# Health / info endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    # "degraded" while DATABASE_URL is set but billing, the audit log or tenant settings are
    # not wired yet: a self-hosted proxy keeps serving and retrying meanwhile. Part names
    # only, since this endpoint is public; the error, which can name hosts and users, is
    # in the log.
    missing = _db_not_wired()
    if missing:
        return {"status": "degraded", "version": "1.0.0", "not_wired": missing}
    return {"status": "ok", "version": "1.0.0"}


@app.get("/metrics")
async def metrics(request: Request):
    """Prometheus scrape endpoint — exposes all token-optimisation metrics.

    Gated by ``METRICS_SCRAPE_TOKEN`` (Bearer). The metrics carry per-tenant
    token/cost labels, so they must not be world-readable. When the token is unset
    the endpoint refuses every scrape, unless ``METRICS_ALLOW_UNAUTHENTICATED`` is on
    (local dev; the startup log says which applies).

    The image runs ``uvicorn --workers 2``, so each worker process holds its own
    in-process registry and a scrape would otherwise answer from whichever worker
    the OS routed to — the series oscillates and ``rate()`` reads every flip as a
    counter reset. When ``PROMETHEUS_MULTIPROC_DIR`` is set (baked into the
    Dockerfiles) the workers write mmap files into that directory and this endpoint
    aggregates ACROSS all of them. The env var is read per request, not at import,
    so the single-process path stays exercised by the unit suite.

    Note this fixes the workers axis only. Under ``max_instances > 1`` each
    container is still a separate registry — that is a Prometheus-side scrape and
    aggregation concern (scrape every instance), not something this endpoint can
    solve.
    """
    if _METRICS_SCRAPE_TOKEN:
        auth = request.headers.get("Authorization", "")
        provided = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(provided, _METRICS_SCRAPE_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid or missing metrics scrape token")
    elif not _METRICS_ALLOW_UNAUTHENTICATED:
        raise HTTPException(status_code=403, detail=(
            "/metrics is closed: set METRICS_SCRAPE_TOKEN on the proxy and present it as a "
            "Bearer token, or METRICS_ALLOW_UNAUTHENTICATED=true on a machine nobody else "
            "can reach"))
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import CollectorRegistry, multiprocess
        registry = CollectorRegistry()
        # Reads the per-PID mmap files of EVERY worker in this container. uvicorn
        # does not recycle workers, so `mark_process_dead` reaping is unnecessary;
        # staleness is bounded by the container-start purge in the Dockerfile CMD.
        multiprocess.MultiProcessCollector(registry)
        return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
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
        raise HTTPException(status_code=500, detail=f"Tool governance query failed: {exc}") from exc


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

    Alertmanager authenticates with ``ALERT_WEBHOOK_TOKEN`` (Bearer), which opens this
    endpoint only; an admin key is accepted too.
    """
    auth = request.headers.get("Authorization", "")
    presented = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if _ALERT_WEBHOOK_TOKEN and hmac.compare_digest(presented, _ALERT_WEBHOOK_TOKEN):
        user_id = "alertmanager"
    else:
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
        raise HTTPException(status_code=500, detail=f"Alert processing failed: {exc}") from exc


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
        # client-supplied tenant_id (and never return the all-tenant set: without a tenant
        # the filter below would drop out, so that is refused).
        if not is_admin_key(tenant_metadata):
            tenant_filter = _caller_tenant_id(tenant_metadata)
            if not tenant_filter:
                raise HTTPException(status_code=403, detail="This proxy key is not bound to a tenant.")

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
        raise HTTPException(status_code=500, detail=f"Usage export failed: {exc}") from exc


@app.get("/v1/batch/results/{request_id}")
async def batch_results(request_id: str, request: Request):
    """Poll for a deferred batch response by request_id (returned in the 202 body)."""
    user_id, _api_key, tenant_metadata = await _authenticate(request)
    try:
        from cache.redis_pool import get_redis
        from middleware.g13_batch import get_batch_result_owner
        r = get_redis()
        # H1: only the owning tenant (or an admin key) may poll this result.
        # Return 404 (not 403) to a non-owner so we don't confirm the id exists. With no
        # owner on record only an admin may read it: G13 records the owner before it queues
        # a request, so no caller can claim a result that has none.
        owner = await get_batch_result_owner(request_id)
        if not is_admin_key(tenant_metadata) and owner != _caller_tenant_id(tenant_metadata):
            return JSONResponse(status_code=404, content={"status": "not_found", "request_id": request_id})
        key = f"tok_opt:batch_result:{request_id}"
        raw = await r.get(key)
        if raw is None:
            return JSONResponse(status_code=202, content={"status": "pending", "request_id": request_id})
        stored = json.loads(raw)
        headers = (
            _batch_result_headers(request_id, stored)
            if stored.get("status") == "completed" else None
        )
        return JSONResponse(content=stored, headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Batch result lookup failed: {exc}") from exc


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


@app.get("/v1/groups")
async def list_group_enablement(request: Request):
    """Effective per-tenant group enablement for the calling tenant: ids + booleans only.

    Added 2026-09-06 to close a blind spot in `run-readiness`, which gates every deploy.
    Its only enable signal was the COMMERCIAL `GET /portal/groups`; on the free image that
    404s, so it assumed every group enabled. A disabled group's stage still RUNS (it
    early-returns on its own `enabled` check) and therefore still moves the stage-duration
    metric readiness accepts as proof of firing — so a group shipping `enabled: false`
    scored a tick. G09 had been passing that way on every OSS deploy.

    Returns booleans ONLY, never knob values: enough to tell "disabled by config" from
    "enabled but did not fire", and nothing that leaks a sidecar URL, model or threshold
    through a tenant-scoped endpoint. Keys are the REAL config keys the pipeline reads
    (`G1_compression`, not `G01`) plus the top-level `rate_limit`, because that is what the
    caller must reason about; `null` means the key carries no `enabled` field at all.
    """
    _user_id, api_key, tenant_metadata = await _authenticate(request)
    # Resolve the tenant EXACTLY as the pipeline does for traffic: the key is authoritative,
    # and `X-Tenant-ID` is honoured only for admin keys (resolve_tenant enforces that).
    # Reading the key alone here made an admin's `--tenant-id OTHER` readiness run score
    # one tenant's traffic against another tenant's enable map (review of 9336dfa).
    headers = {k.lower(): v for k, v in request.headers.items()}
    md = tenant_metadata if isinstance(tenant_metadata, dict) else {}
    tenant = resolve_tenant(
        headers,
        key_tenant_id=_caller_tenant_id(tenant_metadata),
        key_tier=md.get("tier", "free"),
        key_is_admin=is_admin_key(tenant_metadata),
        api_key_hash=hashlib.sha256((api_key or "").encode("utf-8")).hexdigest(),
    )
    groups = await _pipeline.effective_group_enablement(tenant.tenant_id)
    return {"object": "list", "tenant_id": tenant.tenant_id, "groups": groups}


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


def _ingest_oidc_required() -> bool:
    """INGEST_REQUIRE_OIDC, else whether DATABASE_URL is set (a multi-tenant deploy, whose
    tenants' buckets anyone could otherwise make it re-ingest). Only an explicit off opts
    out, so a mistyped value keeps the check."""
    flag = os.getenv("INGEST_REQUIRE_OIDC", "").strip().lower()
    if flag:
        return flag not in ("0", "false", "no", "off")
    return bool(os.getenv("DATABASE_URL", ""))


def _ingest_oidc_settings() -> tuple[str, str]:
    """(the push SA's email, the audience) a Pub/Sub push token must carry."""
    return (os.getenv("INGEST_PUSH_SA_EMAIL", "").strip(),
            os.getenv("INGEST_OIDC_AUDIENCE", "").strip())


def _verify_ingest_oidc(request: Request) -> None:
    """Verify the Pub/Sub push OIDC token: required with INGEST_REQUIRE_OIDC on, or unset
    while DATABASE_URL is set (see _ingest_oidc_required).

    Single-tenant local runs (no DATABASE_URL) skip this so the flat-payload curl flow and
    tests keep working; INGEST_REQUIRE_OIDC=false opts any deploy out. When required, both
    INGEST_PUSH_SA_EMAIL and INGEST_OIDC_AUDIENCE must be set, since without either the
    check accepted any Google-signed token (503 until they are). Managed GCP sets all three
    so only the push SA can drive ingestion. 401 on any token failure.
    """
    if not _ingest_oidc_required():
        return
    expected_sa, audience = _ingest_oidc_settings()
    if not (expected_sa and audience):
        logger.error("Refused /ingest-doc: OIDC is required but INGEST_PUSH_SA_EMAIL and "
                     "INGEST_OIDC_AUDIENCE are not both set")
        raise HTTPException(status_code=503, detail="Document ingestion is not configured")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing OIDC token")
    token = auth.removeprefix("Bearer ").strip()
    try:
        from google.oauth2 import id_token as _id_token
        from google.auth.transport import requests as _ga_requests
        claims = _id_token.verify_oauth2_token(token, _ga_requests.Request(), audience=audience)
    except Exception as exc:
        logger.warning("Rejected /ingest-doc: OIDC verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid OIDC token") from exc
    if claims.get("email") != expected_sa:
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

# Body params consumed by middleware for routing/retrieval/loop-control — the
# canonical set + hygiene builder now live in providers (shared with the deferred
# G06 cascade, review S2); these aliases keep main.py's call sites/tests stable.
from providers import outgoing_messages_for
from providers import outgoing_params_for as _outgoing_params_for
_usage_meter = None  # set by _wire_database once the database answers
_audit_logger = None  # core AuditLogger for G29/G30 security events (_wire_database)

# Work a request leaves to run after its response: the billing row, the security audit,
# the quota/trial/spend counters. The loop keeps only a weak reference to a task, so each
# is held here until it is done; a failure is logged; and shutdown waits for what is still
# running before the pools close (bounded: Cloud Run allows 10 s after SIGTERM).
_AFTER_RESPONSE: set = set()
_DRAIN_TIMEOUT_S = 5.0


def _after_response(coro, what: str) -> None:
    """Run ``coro`` after the response, held in ``_AFTER_RESPONSE``. RuntimeError (no
    running loop) is raised as before, with ``coro`` closed: it never started."""
    try:
        task = asyncio.create_task(coro, name=what)
    except RuntimeError:
        coro.close()
        raise
    _AFTER_RESPONSE.add(task)
    task.add_done_callback(_after_response_done)


_SERVICE_TASKS: set = set()


def _service_task(coro, what: str) -> None:
    """Start a background service (the G13 consumer and poller, a warm-up), held in
    ``_SERVICE_TASKS`` so the loop's weak reference is not its only one, with a failure
    logged by name. Not drained at shutdown: the consumer and poller never finish."""
    task = asyncio.create_task(coro, name=what)
    _SERVICE_TASKS.add(task)
    task.add_done_callback(_service_task_done)


def _service_task_done(task) -> None:
    _SERVICE_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("background service %s stopped: %s", task.get_name(),
                     _redact_secrets(task.exception()))


def _after_response_done(task) -> None:
    _AFTER_RESPONSE.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("%s failed after the response: %s", task.get_name(),
                       _redact_secrets(task.exception()))


async def _drain_after_response(limit_s: float = _DRAIN_TIMEOUT_S) -> None:
    """Wait (at most ``limit_s`` seconds) for after-response work still running, webhook
    event deliveries included."""
    pending = set(_AFTER_RESPONSE) | events.pending_tasks()
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=limit_s)
    if still:
        logger.warning("shutdown: %d after-response task(s) still running after %.0f s; their "
                       "billing rows or counter updates may be lost", len(still), limit_s)


def _schedule_security_audit(ctx) -> None:
    """Fire-and-forget PII-free ``audit_events`` rows for any G29 redaction / G30
    guardrail / G31 context-trust / G32 tool-eligibility activity on this request — and
    for an empty billed completion. No-op without ctx / a wired audit logger / a running
    loop, and skips the task entirely when nothing was flagged. Best-effort: audit must
    never block or break the response path.

    This gate is the ONLY dispatcher of ``log_security_events``. Every condition that
    file can write a row for must therefore appear here: a signal missing from this
    tuple is a signal whose row can only ever be emitted as a side effect of some
    unrelated event firing on the same request. ``empty_completion`` was exactly that
    until 2026-09-09 — the writer existed, six tests certified it, and every one of them
    called ``log_security_events`` directly and so never crossed this line."""
    if ctx is None or _audit_logger is None:
        return
    if not (getattr(ctx, "guardrail_action", None) or getattr(ctx, "pii_action", None)
            or getattr(ctx, "context_trust_action", None)
            or getattr(ctx, "context_trust_managed_recorded", None)
            or getattr(ctx, "context_trust_pii_action", None)
            or getattr(ctx, "tool_eligibility_action", None)
            or getattr(ctx, "tool_dispatch_blocked", None)
            or getattr(ctx, "empty_completion", None)):
        return
    try:
        _after_response(_audit_logger.log_security_events(ctx), "security-audit")
    except RuntimeError:  # no running loop (not the request path) — skip
        logger.debug("[%s] security audit skipped: no loop", getattr(ctx, "request_id", "?"))


def _refuse_empty_cache_hit(ctx) -> bool:
    """Refuse a cache HIT whose stored answer is empty, so the request falls through to
    the provider. Returns True when the hit was refused.

    The store-side guard in ``g05_cache.store_response`` stops NEW empty answers going
    in; it can do nothing about the ones already there. Those entries keep their full TTL
    (L1 1h, L2 24h) and this branch returns without running the response pipeline at all,
    so the empty-completion detector never sees them either — every look-alike question
    from that tenant would be answered, and billed, with nothing, until the TTL expired.
    A READ-side refusal is the only layer that reaches them.

    Deliberately confined to ``cache_hit``: ``bypassed`` shares the branch but its
    response never came from the cache, and a bypass rule may legitimately serve a
    content-less canned reply.

    ``no_cache`` is deliberately NOT set. The fresh provider answer stores under the same
    key and so OVERWRITES the poisoned entry — the path self-heals on the first refusal
    instead of refusing, and paying for, every request for the rest of the TTL. If that
    answer is empty too, ``g05_cache.store_response``'s own guard refuses it, so a bad
    entry can never be re-created here.

    Mirrors the G32 hoist immediately below — the same reasoning about this branch
    returning before the response pipeline, applied to the other thing that pipeline
    would have caught.
    """
    try:
        if not getattr(ctx, "cache_hit", False):
            return False
        cached = getattr(ctx, "cache_response", None)
        if not isinstance(cached, dict) or not _is_empty_cached_answer(cached):
            return False
        logger.warning(
            "[%s] G05 refusing an EMPTY cached answer (level=%s, finish_reason=%s) — "
            "falling through to the provider rather than billing for nothing",
            getattr(ctx, "request_id", "?"), getattr(ctx, "cache_level", "?"),
            ((cached.get("choices") or [{}])[0] or {}).get("finish_reason") or "(none)",
        )
        ctx.cache_hit = False
        ctx.cache_response = None
        ctx.cache_level = None
        savings = getattr(ctx, "savings", None)
        if savings is not None:
            for _attr, _val in (("cache_hit", False), ("cache_level", None)):
                if hasattr(savings, _attr):
                    setattr(savings, _attr, _val)
            # G05 records "L1 exact-match cache hit" with tokens_after=0 at LOOKUP time.
            # If that step survived a refused hit, the ledger would claim a 100% saving
            # for a request that then went to the provider in full — the same plan-time
            # phantom D-073 removed from G06, arriving through a different door. The
            # cache did not serve this request, so it gets no step.
            _steps = getattr(savings, "step_savings", None)
            if isinstance(_steps, list):
                savings.step_savings = [s for s in _steps
                                        if getattr(s, "group", None) != "G05"]
        return True
    except Exception as exc:  # noqa: BLE001 — never turn a served hit into an error
        logger.debug("[%s] empty-cache-hit check failed: %s",
                     getattr(ctx, "request_id", "?"), exc)
        return False


async def _apply_tool_eligibility_on_short_circuit(ctx, response: Dict) -> Dict:
    """Run ONLY G32 over a bypassed / cache-hit response.

    Those paths return without touching the response pipeline, so without this a cached
    answer carrying a tool call would never be gated — and because the gate also runs
    when the entry is *stored*, a policy tightened afterwards would never reach it. G32
    is a trust & safety group: it must not be bypassable, exactly like G29/G30 on the
    request side.

    Calling the single group rather than ``_pipeline.process_response`` is deliberate —
    see the call site. Best-effort: an unexpected failure here must not turn a served
    cache hit into an error, so it degrades to the response as-is (the gate still ran
    on the miss that populated the entry).
    """
    if ctx is None or not isinstance(response, dict) or _pipeline is None:
        return response
    g32 = getattr(_pipeline, "g32", None)
    if g32 is None:
        return response
    try:
        return await g32.process_response(ctx, response)
    except Exception as exc:
        # ERROR + a dedicated counter, not a WARNING: this branch serves a response the
        # gate was supposed to check and did NOT. Staying fail-open here is deliberate
        # (the gate already ran on the miss that populated the entry, and failing a cache
        # hit closed would be an outage), but "gate broke" must be distinguishable from
        # "gate passed" — otherwise a permanently-broken gate looks identical to a clean
        # one on every dashboard.
        logger.error(
            "[%s] G32 FAILED on the short-circuit path — response served WITHOUT "
            "tool-call enforcement: %s", getattr(ctx, "request_id", "?"), exc,
        )
        try:
            from middleware.quality_metrics import record_tool_gate_failure
            record_tool_gate_failure(getattr(ctx, "tenant_id", "default"), path="short_circuit")
        except Exception as err:  # metrics must never break the served response
            logger.debug("tool-gate failure metric not recorded: %r", err)
        return response


def _schedule_notifications(ctx) -> None:
    """Fire-and-forget outbound webhook events for security activity on this request
    (guardrail block / PII detected). No-op in OSS (no dispatcher installed) and when
    nothing was flagged — PII-free payloads (categories / entity TYPES + counts only).

    Attribution note: G30 (user prompt) and G31 (retrieved context) are independent
    guardrails that can each be configured differently per tenant (e.g. G30 in `flag`
    mode alongside G31 in `block` mode). The payload fields below are built from
    WHICHEVER guardrail(s) actually blocked/acted — never picked by "whichever value
    happens to be non-empty" — so a non-blocking G30 flag can never mask a G31 block
    (or vice versa) in the category/action reported to the tenant's webhook consumer."""
    if ctx is None or not events.dispatcher_installed():
        return
    tenant_id = getattr(ctx, "tenant_id", "default")
    guardrail_blocked = getattr(ctx, "guardrail_action", None) == "block"
    context_trust_blocked = getattr(ctx, "context_trust_action", None) == "block"
    if guardrail_blocked or context_trust_blocked:
        categories = []
        sources = []
        if guardrail_blocked:
            categories += list(getattr(ctx, "guardrail_categories", []) or [])
            sources.append("user")
        if context_trust_blocked:
            categories += list(getattr(ctx, "context_trust_categories", []) or [])
            sources.append("retrieved")
        events.schedule_event(tenant_id, events.GUARDRAIL_BLOCK, {
            "request_id": getattr(ctx, "request_id", ""),
            "categories": list(dict.fromkeys(categories)),  # de-dup, preserve order
            "source": "+".join(sources),
        })
    # G29 (request PII) and G31 (retrieved-context PII) can each independently flag/mask/
    # block; `action` reports the MOST SEVERE outcome across both (block > mask > flag),
    # never "whichever ran" — a non-blocking G29 flag must never hide a G31 block.
    pii_action = getattr(ctx, "pii_action", None)
    ct_pii_action = getattr(ctx, "context_trust_pii_action", None)
    if pii_action or ct_pii_action:
        severity = {"block": 3, "mask": 2, "flag": 1}
        action = max((pii_action, ct_pii_action), key=lambda a: severity.get(a, 0))
        sources = []
        if pii_action:
            sources.append("user")
        if ct_pii_action:
            sources.append("retrieved")
        events.schedule_event(tenant_id, events.PII_DETECTED, {
            "request_id": getattr(ctx, "request_id", ""),
            "action": action,
            "entities": sorted(set(list(getattr(ctx, "pii_entities", []) or [])
                                   + list(getattr(ctx, "context_trust_pii_entities", []) or []))),
            "count": int(getattr(ctx, "pii_redactions", 0) or 0)
                     + int(getattr(ctx, "context_trust_pii_redactions", 0) or 0),
            "source": "+".join(sources),
        })


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
        _after_response(_usage_meter.record(
            ctx, response,
            status_code=status_code, billable=billable,
            total_duration_ms=total_duration_ms, llm_duration_ms=llm_duration_ms,
        ), "billing-row")
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
            # Feed the served model's latency into G06's least_latency strategy EWMA —
            # gated on a genuine SUCCESS (status == "200"), never on a failed/errored exit.
            # Without this, a model that fails FAST (immediate 4xx/5xx) would look "fast"
            # to the EWMA and get preferentially routed to by least_latency — the opposite
            # of intended, and directly undermining the sibling per-model-lockout feature
            # (a model being locked out for failing should not simultaneously look
            # attractive to the latency strategy). Known residual limitation: on a
            # failover/cascade escalation, llm_elapsed_ms is cumulative across every
            # attempt in the request, so a multi-tier escalation still attributes combined
            # latency to the final winning model — fixing that needs per-attempt timing
            # plumbed through ctx.provider_attempts, deferred as a separate improvement.
            if status == "200":
                _served_model = getattr(ctx, "routed_model", None) or getattr(ctx, "model", None)
                if _served_model:
                    from middleware.g06_routing import record_model_latency
                    _g6_cfg = (getattr(ctx, "config", None) or {}).get("groups", {}) \
                        .get("G6_routing", {}) or {}
                    record_model_latency(
                        _served_model, llm_ms,
                        alpha=float(_g6_cfg.get("least_latency_alpha", 0.3) or 0.3),
                    )
        # ... and a model whose call failed on the provider's side, served or not, is
        # passed over by least_latency for a while: only successes are measured, so it
        # otherwise stayed unmeasured and was picked first on every request.
        _failed = [a.model for a in (getattr(ctx, "provider_attempts", None) or [])
                   if a.outcome == "error" and getattr(a, "transient", False)]
        if _failed:
            from middleware.g06_routing import record_model_failure
            for _model in _failed:
                record_model_failure(_model)
    except Exception as exc:  # never let metrics break the response
        logger.debug("SLA metric record failed: %s", exc)
    # Trust & Safety: record any G29/G30 activity at every exit path (a flagged
    # request that later errors is still audited). PII-free; best-effort.
    _schedule_security_audit(ctx)
    # Outbound webhook events for the same activity (no-op in OSS / when nothing flagged).
    _schedule_notifications(ctx)
    # C1/C2: persist exactly one usage_events row per outcome. 2xx = billable unit
    # (billed + quota/spend bumped); non-2xx = observability-only row (billable=false,
    # excluded from invoices) so the reliability/latency panels have error data. The 202
    # batch-defer path bills separately at defer, so skip its row here to avoid a
    # non-billable row racing the billable one (ON CONFLICT would drop the billable insert).
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    served = status == "200"
    if served and not _impersonated(ctx):
        _schedule_billing(
            ctx, response if response is not None else {},
            status_code=status_code, billable=True,
            total_duration_ms=int(elapsed_ms), llm_duration_ms=int(llm_ms),
        )
        _bump_quota_counter(ctx)
        _bump_spend_counter(ctx)
        _bump_trial_counter(ctx)
    elif served or (status_code != 202 and _persist_all_outcomes()):
        # A row never billed: an answer to an admin key acting as this tenant (the row names
        # the impersonator), or an observability-only error row. ctx may be None for
        # very-early failures — skip then.
        _schedule_billing(
            ctx, (response if response is not None else {}) if served else {},
            status_code=status_code, billable=False,
            total_duration_ms=int(elapsed_ms), llm_duration_ms=int(llm_ms),
        )


def _impersonated(ctx) -> bool:
    """Whether an admin key sent this request as another tenant (X-Tenant-ID). That is the
    operator's traffic, not the tenant's: it is recorded, naming the impersonator, but never
    billed to the tenant or counted against its quota, spend cap or trial."""
    return ctx is not None and bool(getattr(ctx, "impersonator_tenant_id", None))


def _bill_batch_deferred(ctx) -> None:
    """C1b: a batched request is billable at defer, since it was accepted and will be served
    async (the result-serve endpoint has no ctx); request_id UNIQUE keeps it single even if
    the client polls the result. Being billed, it counts against the quota and trial now
    too; its cost is known only when its answer arrives, and G13 adds it to the spend
    counter then. None of this when an admin key sent it as the tenant."""
    billable = not _impersonated(ctx)
    _schedule_billing(ctx, getattr(ctx, "cache_response", None) or {}, status_code=202,
                      billable=billable)
    if billable:
        _bump_quota_counter(ctx)
        _bump_trial_counter(ctx)


def _bump_quota_counter(ctx) -> None:
    """WS23: fire-and-forget monthly billable-request counter (G00 quota gate reads it).

    Mirrors the billable unit exactly — bumped for every served 2xx and every batched
    request at defer, tenant-prefixed (`t:<id>:quota:<YYYYMM>`), ~40-day TTL so last
    month's key self-expires."""
    if ctx is None:
        return
    prefix = counter_prefix(ctx)

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
        _after_response(_bump(), "quota-counter")
    except RuntimeError:
        pass


def _bump_trial_counter(ctx) -> None:
    """Fire-and-forget lifetime free-trial served-2xx counter (G00 trial gate reads it).

    Bumped for every served 2xx while the tenant's trial is ``active`` — the counting
    basis is the billable unit exactly (cache hits + bypasses + content-filter 200s all
    reach this path, and a batched request at defer, where it is billed). Tenant-prefixed
    (`t:<id>:trial_used`) with **no TTL** — a trial spans arbitrary calendar time; the
    counter is reset (DEL) by the admin console's start/convert/cancel actions, never by
    expiry. No-op unless a trial is active, so OSS/non-trial traffic is untouched."""
    if ctx is None:
        return
    if ((getattr(ctx, "config", None) or {}).get("trial") or {}).get("status") != "active":
        return
    prefix = counter_prefix(ctx)

    async def _bump() -> None:
        try:
            from cache.redis_pool import get_redis
            from middleware.g00_rate_limit import G00RateLimit
            r = get_redis()
            await r.incr(G00RateLimit.trial_key(prefix))
        except Exception as exc:
            logger.debug("trial counter bump failed: %s", exc)

    try:
        _after_response(_bump(), "trial-counter")
    except RuntimeError:
        pass


def _bump_spend_counter(ctx) -> None:
    """Fire-and-forget monthly running-USD spend counter (G00 spend-cap gate reads it).

    Bumped for every served 2xx by the request's REAL ``cost_actual_usd`` (set by
    G18 from the provider's actual token usage), through G00's ``add_to_spend``, which
    G13 uses for a batched request's cost when its answer arrives: tenant-prefixed
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
    try:
        _after_response(add_to_spend(counter_prefix(ctx), cost), "spend-counter")
    except RuntimeError:
        pass


def _stream_options_with_usage(requested) -> tuple:
    """(stream_options for the provider, withhold): the provider is always asked for its
    usage chunk, because pricing, billing and the spend cap depend on it and a client's own
    ``include_usage: false`` must not switch them off. ``withhold`` is True when the client
    sent stream_options without asking for usage: the usage chunk is then kept from it, as
    the provider would have. A client that sent no stream_options gets the chunk, as it
    always has."""
    opts = dict(requested) if isinstance(requested, dict) else {}
    withhold = isinstance(requested, dict) and not requested.get("include_usage")
    opts["include_usage"] = True
    return opts, withhold


def _stream_response(ctx, call_model, call_kwargs, outgoing_params, request_id, request_start,
                     *, eff_cfg=None, provider="", routed_adapter=None, provider_key=None):
    """Pass-through SSE streaming: relay the provider's chunks unchanged.

    Request-side optimisations (G0–G13) are already applied before this is called. The
    response-side pipeline (G14/G18/G23) is skipped for streamed calls; the call is priced
    on completion through G18's own ``price_response`` and ``emit_usage_metrics``
    (``_price_stream``), from the provider's usage chunk, which is always requested, or
    from an estimate when none arrived. Billing fires once on completion (per request).

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
    # Always ask for the usage chunk (the client's own include_usage cannot switch metering
    # off); litellm.drop_params removes stream_options for providers that don't support it.
    params["stream_options"], withhold_usage = _stream_options_with_usage(
        params.get("stream_options"))

    _rcfg = ResilienceConfig.resolve(eff_cfg or ctx.config or {}, provider)

    async def _establish_primary():
        return await litellm.acompletion(
            model=call_model, messages=outgoing_messages_for(routed_adapter, ctx.messages),
            **call_kwargs, **params, **_rcfg.call_limits(),
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

    # G32 on the streaming path (#25). Before this, a tenant's tool policy silently did
    # not apply to streamed responses: the call was relayed and the caller's own agent
    # loop executed it. Built ONCE per stream and None on a default install (no policy),
    # so the common path does no per-chunk work and stays byte-identical.
    _tool_gate = None
    try:
        _g32 = getattr(_pipeline, "g32", None)
        if _g32 is not None:
            _tool_gate = _g32.stream_gate(ctx)
    except Exception as exc:
        # A gate that cannot be built must not take the request down; it is logged at
        # ERROR because the stream then goes out UNGATED, which is the thing #25 fixed.
        logger.error("[%s] G32 stream gate unavailable — stream is UNGATED: %s",
                     request_id, exc)

    async def event_gen():
        last_usage = {}
        parts = []  # accumulated assistant text for chunk-aware G23 (measurement only)
        generated = []  # everything the model generated, to estimate usage if none arrives
        called = set()  # the tools the model called (G08's pruning signal)
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
                if withhold_usage and "usage" in cd:
                    if cd.get("usage") and not cd.get("choices"):
                        continue          # the usage-only chunk this client did not ask for
                    cd = {k: v for k, v in cd.items() if k != "usage"}
                try:
                    delta = (cd.get("choices") or [{}])[0].get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        parts.append(delta["content"])
                        generated.append(delta["content"])
                    if isinstance(delta.get("reasoning_content"), str):
                        generated.append(delta["reasoning_content"])
                    for call in delta.get("tool_calls") or []:
                        fn = (call or {}).get("function") or {}
                        generated.extend(v for v in (fn.get("name"), fn.get("arguments"))
                                         if isinstance(v, str))
                        if isinstance(fn.get("name"), str) and fn["name"]:
                            called.add(fn["name"])
                except Exception as err:
                    logger.debug("stream chunk delta unreadable: %r", err)
                if _tool_gate is not None:
                    try:
                        cd = _tool_gate.filter(cd)
                    except Exception as exc:
                        # Bounded to this chunk: relaying it ungated is wrong, but tearing
                        # down a stream mid-flight is worse, and the caller would have no
                        # way to tell the difference from a provider error.
                        logger.error("[%s] G32 stream gate failed on a chunk: %s",
                                     request_id, exc)
                    if cd is None:
                        continue              # whole chunk was policy-stripped
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
            # Record the tool-policy verdict (#25). Kept in its OWN try and placed ahead
            # of the accounting below, deliberately: the streaming path is where
            # billing/SLA truth is computed, and gating must never be able to disturb it.
            if _tool_gate is not None:
                try:
                    _tool_gate.finish()
                except Exception as exc:
                    logger.debug("[%s] G32 stream gate finish failed: %s", request_id, exc)
            # Chunk-aware G23: run output compression on the reassembled stream to record the
            # output-side savings the (skipped) response pipeline would have. The live chunks
            # are emitted unchanged — rewriting them mid-stream would corrupt the SSE output.
            _apply_stream_g23(ctx, "".join(parts))
            # G06's routing step is written on the RESPONSE now (from the model that
            # actually answered), and the response pipeline — G18 included — is skipped
            # for streams. Without this a streamed request would disclose no route at
            # all, which is the same silence in the other direction. Idempotent.
            try:
                from middleware.g06_routing import record_routing_step
                record_routing_step(ctx)
            except Exception as exc:  # noqa: BLE001 — accounting never breaks a stream
                logger.debug("[%s] streaming routing-step record failed: %s", request_id, exc)
            # G08's pruning signal, which the (skipped) response pipeline records for other
            # calls: which offered registry tools the model called. After the response.
            if called and getattr(ctx, "g08_offered_tools", None):
                try:
                    from middleware.g08_tool_loading import record_called_tools
                    _after_response(record_called_tools(ctx, set(called)), "g08-called-tools")
                except Exception as exc:  # noqa: BLE001 — accounting never breaks a stream
                    logger.debug("[%s] recording the streamed tool calls failed: %s",
                                 request_id, exc)
            # The response pipeline (incl. G18) is skipped for streamed calls, so price the
            # served call here, through G18's own pricing and counters. A stream that failed
            # before any chunk served nothing and is not billed, so it is not priced.
            if served:
                _price_stream(ctx, last_usage, "".join(generated), request_id)
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


def _price_stream(ctx, usage: Dict, generated_text: str, request_id: str) -> None:
    """Price a served stream and emit G18's token and cost counters for it.

    The response pipeline, G18 included, does not run for streamed calls, so this calls
    G18's own ``price_response`` and ``emit_usage_metrics``: a streamed call is priced like
    any other (cache, reasoning surcharge, every judge or cascade call booked in
    ``ctx.provider_calls``), and budget alerts count it. It is priced even when G18 is off,
    as the spend cap needs the cost; the counters follow G18's own switches.

    With no usage chunk (the client disconnected first, or the provider does not report
    stream usage) the call is priced from an estimate: the proxy's own prompt count and
    the generated text's token count. It is recorded the way G18 records any call the
    provider did not report, with ``provider_prompt_tokens`` left None, and counted.

    Never raises: a failure is logged at warning and counted, and the stream is unaffected.
    """
    from middleware import record_provider_call
    from middleware.g18_observability import (
        STREAM_ACCOUNTING_ERRORS, STREAM_USAGE_ESTIMATED, emit_usage_metrics, price_response,
    )
    tenant_id = getattr(ctx, "tenant_id", "default")
    try:
        if usage:
            record_provider_call(ctx, ctx.routed_model, {"usage": usage})
        else:
            from savings.calculator import estimate_tokens
            completion = estimate_tokens(generated_text, ctx.routed_model or "")
            usage = {"completion_tokens": completion}  # no prompt_tokens: the provider sent none
            record_provider_call(ctx, ctx.routed_model, {"usage": {
                "prompt_tokens": ctx.savings.proxy_optimised_tokens,
                "completion_tokens": completion}})
            STREAM_USAGE_ESTIMATED.labels(tenant_id=tenant_id).inc()
            logger.info("[%s] stream ended without a usage chunk: priced from an estimate "
                        "(%d completion tokens)", request_id, completion)
        cfg = ((getattr(ctx, "config", None) or {}).get("groups", {})
               .get("G18_observability", {})) or {}
        priced = price_response(ctx, {"usage": usage}, cfg)
        if cfg.get("enabled", False) and cfg.get("prometheus_enabled", True):
            emit_usage_metrics(ctx, priced)
    except Exception as exc:  # noqa: BLE001 — accounting never breaks a stream
        STREAM_ACCOUNTING_ERRORS.labels(tenant_id=tenant_id).inc()
        logger.warning("[%s] streamed call could not be priced (its cost is missing): %s",
                       request_id, exc)


def _apply_stream_g23(ctx, content: str) -> None:
    """G23's measurement of a streamed answer, as the (skipped) response pipeline would
    take it: the client got the whole stream, so it is not a saving."""
    try:
        from middleware.g23_streaming_compression import measure_output
        measure_output(ctx, content)
    except Exception as exc:
        logger.debug("[%s] stream G23 failed: %s", ctx.request_id, exc)


def _savings_headers(ctx, request_start: float) -> Dict[str, str]:
    """Build the per-call ``x-tokenlean-*`` savings/routing headers from ctx.savings so a
    customer's FinOps/observability pipeline can attribute cost per request without parsing
    the body. ``x-savings-usd`` is kept as a back-compat alias of the cost header.

    Emitted on EVERY served 2xx — the normal LLM path + the G06 cascade / F2 agent
    short-circuits (via ``_served_response``) AND the cache-hit / bypass / content-filter
    short-circuits (which serve a raw JSONResponse). Attaching them on the cache/bypass
    paths is not cosmetic: ``x-tokenlean-cache`` must be present exactly when a request WAS
    served from cache — previously those (highest-volume) responses carried no headers at
    all, silently breaking the advertised always-on attribution + failing the readiness
    header gate. NOTE: two paths are disclosed exceptions, not covered by this helper —
    streamed responses (matching ``_token_opt``/G23) and the G13 batch-results poller
    (``GET /v1/batch/results/{request_id}``), which has no RequestContext at poll time and
    builds its own smaller header set from the stored baseline/actual token counts instead.
    """
    meta = ctx.savings.to_langfuse_metadata()
    headers = {"x-savings-usd": f"{ctx.savings.cost_saving_usd:.6f}"}
    _cache_hit = bool(meta.get("cache_hit"))
    _cache_level = meta.get("cache_level")
    header_fields = {
        "x-tokenlean-request-id": meta.get("request_id"),
        "x-tokenlean-routed-model": meta.get("routed_model") or meta.get("model_requested"),
        "x-tokenlean-cache": (
            f"hit:{_cache_level}" if (_cache_hit and _cache_level) else "hit" if _cache_hit else "miss"
        ),
        "x-tokenlean-tokens-saved": meta.get("total_abs_saving"),
        "x-tokenlean-pct-saved": (
            None if meta.get("total_pct_saving") is None
            else f"{meta.get('total_pct_saving'):.2f}"
        ),
        "x-tokenlean-cost-saved-usd": f"{ctx.savings.cost_saving_usd:.6f}",
        "x-tokenlean-latency-ms": f"{(time.time() - request_start) * 1000:.1f}",
        # Provider prompt-cache accounting (#34). Left out entirely when the provider
        # reported nothing — an absent header reads as "unknown", a "0" would have
        # asserted "no cache activity", which is the misreport this exists to end.
        "x-tokenlean-cache-read-tokens": meta.get("cache_read_tokens"),
        "x-tokenlean-cache-write-tokens": meta.get("cache_write_tokens"),
        "x-tokenlean-cache-share-pct": meta.get("cache_share_of_bill_pct"),
        # Two disclosures the caller cannot infer from the body. The budget raise
        # INCREASES what they can be billed for output, so it is stated, not assumed;
        # the empty-completion flag says a billed response contains nothing, which is
        # otherwise indistinguishable from a model that simply had little to say.
        "x-tokenlean-output-budget-raised": (
            None if not getattr(ctx, "output_budget_raised", None)
            else "{param}:{frm}->{to}".format(
                param=ctx.output_budget_raised.get("param"),
                frm=ctx.output_budget_raised.get("from"),
                to=ctx.output_budget_raised.get("to"),
            )
        ),
        "x-tokenlean-empty-completion": (
            None if not getattr(ctx, "empty_completion", None)
            else ctx.empty_completion.get("reason")
        ),
        # The caller's explicit model choice was overridden (unconfigured model → the
        # configured default). Absent on every request that got the model it asked for
        # or that was routed by G06, which routed_model already discloses.
        "x-tokenlean-model-substituted": getattr(ctx, "model_substituted", "") or None,
    }
    for _k, _v in header_fields.items():
        if _v is not None:
            headers[_k] = str(_v)

    # G17: expose InterAgentState via x-token-opt-state header for downstream agents
    token_budget_state = ctx.params.get("_token_budget")
    if token_budget_state:
        import base64
        import json

        headers["x-token-opt-state"] = base64.b64encode(
            json.dumps(token_budget_state, separators=(",", ":")).encode("utf-8")
        ).decode("utf-8")
    return headers


def _batch_result_headers(request_id: str, stored: Dict) -> Dict[str, str]:
    """Best-effort x-tokenlean-* headers for a completed G13 batch result.

    No RequestContext exists at poll time (the flush loop calls litellm directly, outside
    the pipeline), so this is deliberately smaller than ``_savings_headers``: it compares
    the baseline token count persisted at accumulate-time (``ctx.savings.baseline_tokens``)
    against the provider's actual reported ``usage.prompt_tokens`` for this response —
    real numbers, not the full per-group savings breakdown a normal response gets.
    """
    headers = {"x-tokenlean-request-id": request_id}
    response = stored.get("response") or {}
    model = response.get("model")
    if model:
        headers["x-tokenlean-routed-model"] = str(model)
    baseline_tokens = stored.get("baseline_tokens")
    actual_tokens = (response.get("usage") or {}).get("prompt_tokens")
    if baseline_tokens is not None and actual_tokens is not None:
        try:
            from savings.calculator import estimate_cost
            tokens_saved = max(0, int(baseline_tokens) - int(actual_tokens))
            cost_saved = max(0.0, estimate_cost(int(baseline_tokens), 0, model or "") -
                              estimate_cost(int(actual_tokens), 0, model or ""))
            headers["x-tokenlean-tokens-saved"] = str(tokens_saved)
            headers["x-tokenlean-cost-saved-usd"] = f"{cost_saved:.6f}"
            headers["x-savings-usd"] = f"{cost_saved:.6f}"
            if baseline_tokens:
                headers["x-tokenlean-pct-saved"] = f"{(tokens_saved / baseline_tokens) * 100:.2f}"
        except Exception as exc:
            logger.debug("[%s] batch result header cost estimate failed: %s", request_id, exc)
    return headers


# ── Sent-prompt echo (E4) ────────────────────────────────────────────────────
# Every optimisation acts on the PROMPT, so the prompt is the only place its defects are
# visible — and it was the one thing no artefact kept. The #48 compaction defect destroyed
# customer tool-result content on billed 200s and nothing recorded what the model had
# actually received. This is the opt-in way to keep it.
#
# TWO gates, both required: the caller asks per request with `x_echo_prompt`, and the
# operator has to have allowed it with `observability.echo_sent_prompt`. The operator gate
# exists because the echo can contain content the CALLER never sent — G07 retrieved chunks,
# G10 memories, G02 templates, G28-resolved blocks — so a tenant must not be able to
# self-serve it with a request parameter alone.
# Exact names, not substrings. The first version matched the bare substring "token", which
# silently censored `max_tokens` and `max_completion_tokens` — the two parameters most worth
# seeing in an echo, since they are what an output-budget group changes. A redaction that
# quietly removes real data is the same class of defect as an echo that quietly shortens a
# prompt: the artefact reads as evidence while being something else.
_ECHO_SECRET_EXACT_NAMES = frozenset({
    "api_key", "apikey", "api_base", "authorization", "auth", "access_token", "auth_token",
    "bearer_token", "id_token", "refresh_token", "session_token", "password", "secret",
    "credential", "credentials", "aws_secret_access_key", "aws_access_key_id",
})
# Suffixes are deliberately narrow: `<something>_key` / `_secret` / `_password` / `_token`
# are credential SHAPES, and no provider sampling parameter uses them (`max_tokens` and
# `max_completion_tokens` end in `_tokens`, plural — which is not a suffix here).
_ECHO_SECRET_SUFFIXES = ("_key", "_secret", "_password", "_token", "_credential")
# Real provider parameters that a suffix rule would otherwise eat. `prompt_cache_key` is
# G21's OpenAI prefix-cache key — a routing hint, not a credential, and the single most
# useful field in an echo when diagnosing why a provider cache did not hit. Found by the
# test written for the `max_tokens` regression, which is the point of parametrising it.
_ECHO_NEVER_REDACT = frozenset({"prompt_cache_key", "user", "safety_identifier"})


def _echo_enabled(ctx) -> bool:
    """Both gates, resolved with the per-tenant operator overlay already applied to
    ctx.config at pipeline entry. Fails CLOSED on anything unexpected — an echo that
    should not have happened cannot be un-sent."""
    try:
        if str(ctx.params.get("x_echo_prompt", "")).lower() not in ("true", "1", "yes"):
            return False
        return bool((ctx.config.get("observability") or {}).get("echo_sent_prompt", False))
    except Exception:
        return False


def _is_credential_key(name: str) -> bool:
    """Exact auth names, plus a short list of credential-shaped SUFFIXES.

    Not substring matching: `"token" in "max_tokens"` is True, and censoring the caller's
    output budget out of a diagnostic is a data-loss bug wearing a security costume.
    """
    low = str(name).lower()
    if low in _ECHO_NEVER_REDACT:
        return False
    return low in _ECHO_SECRET_EXACT_NAMES or low.endswith(_ECHO_SECRET_SUFFIXES)


def _sanitise_echo_params(params: Dict) -> Dict:
    """Belt-and-braces. The echo is built from `outgoing_params`, which by construction
    never contains the provider credential (that lives in the adapter's `build_call`
    kwargs and is passed separately) — but a future adapter change must not turn this
    into a credential leak, so anything credential-SHAPED is dropped by name as well."""
    return {k: v for k, v in (params or {}).items() if not _is_credential_key(k)}


def capture_sent_prompt(ctx, messages, outgoing_params: Dict, model: str) -> None:
    """Record what is about to go to the provider, on `ctx.sent_prompt`.

    Called immediately before the provider call so a G06 cascade tier or a failover target
    is recorded as the thing that actually served, not as the thing we first intended.
    """
    if not _echo_enabled(ctx):
        return
    try:
        limit = int((ctx.config.get("observability") or {}).get("max_echo_chars", 200000))
    except Exception:
        limit = 200000
    payload = {
        "model": model,
        "messages": copy.deepcopy(messages) if messages else [],
        "params": _sanitise_echo_params(outgoing_params),
        "truncated": False,
    }
    try:
        size = len(json.dumps(payload, default=str))
    except Exception:
        size = 0
    if limit > 0 and size > limit:
        # Truncate LOUDLY. A silently shortened prompt is worse than no prompt at all —
        # it would read as evidence of what the model saw while being something else.
        payload["truncated"] = True
        payload["original_chars"] = size
        payload["messages"] = [
            {"role": m.get("role"), "content": str(m.get("content"))[:2000]}
            for m in (payload["messages"] or [])
        ]
    ctx.sent_prompt = payload


def _attach_sent_prompt(ctx, response_dict: Dict, not_sent_reason: Optional[str] = None) -> None:
    """Attach the echo (or an explicit 'nothing was sent') under `_token_opt.sent`."""
    if not _echo_enabled(ctx):
        return
    block = response_dict.setdefault("_token_opt", {})
    if not_sent_reason is not None:
        # A cache hit / bypass / content-filter block never reached a provider. Saying so
        # explicitly stops an artefact from ever implying a prompt was sent when none was.
        block["sent"] = None
        block["sent_skipped_reason"] = not_sent_reason
        return
    block["sent"] = ctx.sent_prompt


def _served_response(ctx, response_dict: Dict, request_start: float) -> JSONResponse:
    """Finalise a served 2xx response: attach savings metadata + headers, record
    SLA/billing (`_record_outcome`), and return the JSONResponse.

    Shared by the normal LLM path and the G06 cascade short-circuit so both bill
    and surface headers identically.
    """
    meta = ctx.savings.to_langfuse_metadata()
    response_dict.setdefault("_token_opt", {}).update(meta)
    # Disclose the two facts the savings ledger cannot express. `output_budget_raised`
    # is a cost INCREASE the proxy chose on the caller's behalf; `empty_completion` says
    # this billed response delivered nothing (the ledger would otherwise report a small
    # positive saving on it, which is worse than silence). Both omitted when absent.
    if getattr(ctx, "output_budget_raised", None):
        response_dict["_token_opt"]["output_budget_raised"] = ctx.output_budget_raised
    if getattr(ctx, "empty_completion", None):
        response_dict["_token_opt"]["empty_completion"] = ctx.empty_completion
    # The proxy served a model the caller neither asked for nor chose (an unconfigured
    # model swapped for the configured default). Routing to a cheaper tier is the product
    # and is disclosed by routed_model; this is the case where the caller's own explicit
    # choice was overridden, and it was previously invisible in every surface.
    if getattr(ctx, "model_substituted", ""):
        response_dict["_token_opt"]["model_substituted"] = ctx.model_substituted
    _attach_sent_prompt(ctx, response_dict)
    headers = _savings_headers(ctx, request_start)
    # C1: SLA metrics + the billable usage_events row, centralised so every
    # 2xx-served path bills exactly once.
    _record_outcome(ctx, request_start, "200", response_dict)
    return JSONResponse(content=response_dict, headers=headers)


async def _process_and_serve(ctx, response: Dict, request_start: float) -> JSONResponse:
    """Run the response stages over a paid answer, then serve it (`_served_response`).

    The pipeline skips a response stage that fails, except the safety stages (G29, G30,
    G32): their failure ends the request here, and the answer is withheld rather than served
    unmasked or unchecked. The provider was paid all the same, so the outcome is recorded: a
    non-billable usage row priced at what the provider billed, the security audit row and
    the SLA metrics. The tenant is not billed for our failure."""
    try:
        ctx, response_dict = await _pipeline.process_response(ctx, response)
    except Exception as exc:
        rid = getattr(ctx, "request_id", "?")
        logger.error("[%s] response stages failed, answer withheld: %s", rid, _redact_secrets(exc))
        try:
            price_billed_call(ctx, response)
        except Exception as price_exc:
            logger.warning("[%s] the call could not be priced: %s", rid, price_exc)
        langfuse_tracing.finish_trace(ctx, None)
        _record_outcome(ctx, request_start, "500")
        raise HTTPException(status_code=500,
                            detail="The answer could not be processed, so it was withheld") from exc
    return _served_response(ctx, response_dict, request_start)


_OPENAI_VALID_ROLES = {"system", "user", "assistant", "tool", "function", "developer"}


def _ingress_extra_allowed_params(cfg: Any) -> Tuple[str, ...]:
    """Operator-admitted extra OpenAI body fields: ``ingress.extra_allowed_params``.

    Read from the GLOBAL config only — never tenant config, so a tenant cannot widen
    what its own callers may send. A malformed value admits nothing extra.
    """
    section = cfg.get("ingress") if isinstance(cfg, dict) else None
    raw = section.get("extra_allowed_params") if isinstance(section, dict) else None
    if not isinstance(raw, list):
        return ()
    return tuple(name for name in raw if isinstance(name, str) and name)


def _validate_openai_request(messages: Any) -> None:
    """Light shape check for the OpenAI ingress body. Malformed input gets a clean
    400 here — matching the Anthropic/Gemini routes, which already return a 400 —
    instead of surfacing as a 500 or being forwarded to the provider only to 400
    there. Deliberately minimal: it validates the envelope (`messages` is a non-empty
    array of role-bearing objects), not the full schema (litellm/the provider still
    own semantic validation)."""
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="`messages` must be a non-empty array")
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail=f"messages[{i}] must be an object")
        role = m.get("role")
        if not isinstance(role, str) or not role:
            raise HTTPException(status_code=400, detail=f"messages[{i}] is missing a string `role`")
        if role not in _OPENAI_VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"messages[{i}] has an invalid role '{role}'")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _request_start = time.time()
    request_id = str(uuid.uuid4())
    user_id, api_key, tenant_metadata = await _authenticate(request)
    # Parse + validate the body inside a guard so a malformed OpenAI request returns a
    # clean OpenAI-shaped 400 (the Anthropic/Gemini routes already do this via
    # _serve_protocol). _authenticate stays outside — its 401/403 handling is unchanged.
    try:
        body = await request.json()
        messages, model, params = OPENAI.parse_request(
            body, dict(request.headers),
            extra_allowed=_ingress_extra_allowed_params(get_config()))
        _validate_openai_request(messages)
    except HTTPException as exc:
        eb, st = OPENAI.serialise_error(exc.status_code, _detail_str(exc))
        return JSONResponse(status_code=st, content=eb)
    except Exception as exc:
        logger.warning("[%s] OpenAI ingress: bad request: %s", request_id, _redact_secrets(exc))
        eb, st = OPENAI.serialise_error(400, "Invalid request body")
        return JSONResponse(status_code=st, content=eb)
    # Read before the core runs: what the client asked for, not what the pipeline made of it.
    want_stream = bool(params.get("stream"))
    withhold_usage = _stream_options_with_usage(params.get("stream_options"))[1]
    resp = await _serve_core(
        request, request_id, _request_start, messages, model, params,
        user_id, api_key, tenant_metadata, OPENAI.name,
    )
    return _as_requested_stream(resp, withhold_usage=withhold_usage) if want_stream else resp


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

    # Map TokenLean's X-* headers into params (X-Template-ID → x_template_id). Only those:
    # ctx.params is persisted by G13, so infrastructure headers and a caller's own routing
    # hints stay out (the pipeline gives the routing rules every X-* header separately), and
    # the native-SDK proxy credential (x-api-key / x-goog-api-key) never enters it.
    params.update(header_params(request.headers))

    # G7 RAG: derive rag_query from the last user message when x_rag_collection is set
    if "x_rag_collection" in params and "rag_query" not in params:
        for msg in reversed(messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                params["rag_query"] = msg.get("content", "")
                break

    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    params["_api_key_hash"] = api_key_hash
    # The validated key is the authoritative tenant identity (C1); the pipeline reads
    # these _auth_* params and only honours X-Tenant-ID for admin keys. Stamped for EVERY
    # key. The OpenAI ingress now drops `_`-prefixed body fields (protocols/base.py), and
    # this unconditional overwrite stays as the second line of defence — until 2026-09-18 a
    # legacy (string-format) key skipped this block, and `_auth_admin` / `_auth_tenant_id`
    # in its body were believed.
    meta = tenant_metadata if isinstance(tenant_metadata, dict) else {}
    params["_auth_tenant_id"] = meta.get("tenant_id")
    params["_auth_tier"] = meta.get("tier", "free")
    params["_auth_admin"] = bool(meta.get("admin", False))
    params["_auth_gateway"] = is_gateway_key(tenant_metadata)
    # G00 rate-limits on the key-bound identity, never an X-User-ID override (which the
    # caller chooses within its allowlist). _authenticate records it before any override.
    params["_auth_principal"] = str(
        getattr(getattr(request, "state", None), "key_principal", None)
        or meta.get("tenant_id") or user_id or "")

    cfg = get_config()
    ctx = RequestContext.create(
        request_id=request_id, user_id=user_id, messages=messages,
        model=model, params=params, config=cfg,
    )
    ctx.ingress_protocol = ingress_protocol
    # Read when the response starts (TraceIdHeaderMiddleware): its X-Trace-ID, on every path.
    state = getattr(request, "state", None)
    if state is not None:
        state.pipeline_ctx = ctx

    # Run the request-side pipeline (authoritative stage order: middleware/pipeline.py)
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
        # Free-trial expiry is a distinct, billing-relevant refusal — surface it as a
        # 402 (payment required), NOT a 429. Routed through _record_outcome as a "402"
        # so the rejection is never billed and never consumes trial allowance (mirrors
        # the BYOK provider_key_missing 402 path).
        if exc.limit_type == "trial_expired":
            _record_outcome(ctx, _request_start, "402")
            return JSONResponse(
                status_code=402,
                content={
                    "error": {
                        "message": (
                            f"Free trial ended ({exc.scope}). Contact your account owner "
                            "to convert to a paid plan or extend the trial."
                        ),
                        "type": "invalid_request_error",
                        "code": "trial_expired",
                    }
                },
                headers={"Retry-After": str(exc.retry_after)},
            )
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
        _attach_sent_prompt(ctx, response, not_sent_reason="security_block")
        langfuse_tracing.finish_trace(ctx, response)
        _record_outcome(ctx, _request_start, "200", response)
        return JSONResponse(content=response, headers=_savings_headers(ctx, _request_start))

    # An empty answer cached before the store-side guard existed is still live for the
    # rest of its TTL, and this branch returns without the response pipeline, so nothing
    # downstream can notice. Refusing the hit here sends the request to the provider
    # instead of billing the customer for nothing a second time. Checked BEFORE the
    # short-circuit so a refused hit falls through to the normal call path.
    _refuse_empty_cache_hit(ctx)

    # Short-circuit: bypass or cache hit
    if ctx.bypassed or ctx.cache_hit:
        response = ctx.cache_response or {}
        # G32 tool-eligibility is NOT bypassable. This branch returns without running
        # the response pipeline at all, so a cached answer carrying a tool call would
        # otherwise sail past the gate — and a policy tightened after the entry was
        # cached would never apply. Mirrors how G29/G30 are hoisted outside the
        # skip_groups loops on the request side.
        #
        # Deliberately ONE targeted call, never the full process_response: that chain
        # also re-runs G18 observability, re-applies G29 response redaction to
        # already-redacted content, and ends in g05.store_response — turning a security
        # fix into a double-record/double-store bug.
        response = await _apply_tool_eligibility_on_short_circuit(ctx, response)
        response.setdefault("_token_opt", {}).update(ctx.savings.to_langfuse_metadata())
        # E4: no provider call happened on this path. `response` here is the object G05
        # served from the cache — attaching the echo AFTER the store/lookup, and only as an
        # explicit null, keeps a request-specific field out of any cached body.
        _attach_sent_prompt(
            ctx, response, not_sent_reason="cache_hit" if ctx.cache_hit else "bypassed")
        langfuse_tracing.finish_trace(ctx, response)
        _record_outcome(ctx, _request_start, "200", response)  # C1: bill cache hit / bypass
        # Attach the x-tokenlean-* headers here too so cache hits / bypasses carry per-call
        # attribution (esp. x-tokenlean-cache:hit) — they previously returned header-less.
        return JSONResponse(content=response, headers=_savings_headers(ctx, _request_start))

    # Batch deferred — response delivered async
    if ctx.batch_deferred:
        langfuse_tracing.finish_trace(ctx, None)
        _bill_batch_deferred(ctx)
        _record_outcome(ctx, _request_start, "202")
        return JSONResponse(
            status_code=202,
            content={"status": "queued", "request_id": request_id},
        )

    # G06 cascade execution — DEFERRED to here (2026-08-08). G06 only stores the
    # plan; executing inside G06 (Stage 2) sent the PRE-optimisation messages/tools,
    # so every later pipeline group optimised a request whose provider call had
    # already happened (proven live: DS12/DS13/DS14 all-on byte-identical to
    # all-off). Running it here sends the fully optimised prompt. On any cascade
    # error we fall through to the normal single call on ctx.routed_model (G06 set
    # it to the tier-1 pick, with the reachability/cost-floor guards applied).
    if ctx.cascade_plan is not None and ctx.cascade_response is None:
        try:
            from middleware.g06_routing import (
                _execute_three_tier_cascade as g06_execute_cascade,
            )
            _c_model, _c_resp = await g06_execute_cascade(
                ctx,
                ctx.cascade_plan.get("tiers") or {},
                ctx.cascade_plan.get("cfg") or {},
                # Plan-time decisions, never re-derived at call time (reviews S4/S5):
                # the pipeline has since compressed ctx.messages, and stateful tier
                # strategies must not be consulted twice.
                tier1_model=ctx.cascade_plan.get("tier1_model"),
                max_tier_idx=ctx.cascade_plan.get("max_tier_idx"),
            )
        except Exception as _c_exc:  # noqa: BLE001 — cascade failure must never 500
            # Class and status only: the exception's text can carry a key or the base_url.
            _c_model, _c_resp = None, {"error": describe_error(_c_exc)}
        if _c_model and isinstance(_c_resp, dict) and "error" not in _c_resp:
            ctx.routed_model = _c_model
            ctx.savings.routed_model = _c_model
            ctx.cascade_response = _c_resp
            # The plan-time stamp was 'cascade_planned' — only a successful execution
            # earns 'cascade_execution' (review S6: metadata must not claim a cascade
            # that never ran, and the F1 learning loop cohorts on this field).
            ctx.savings.routing_mode = "cascade_execution"
        else:
            # Never retry the model that just failed (review S3): tier-1-call failures
            # are exactly the error class that reaches this path, and ctx.routed_model
            # IS the tier-1 pick. The caller's own model is the safe re-route — its key
            # is present (they chose it) and it can never violate the cost floor.
            _failed_tier1 = ctx.cascade_plan.get("tier1_model")
            if ctx.model and ctx.routed_model == _failed_tier1 and ctx.model != _failed_tier1:
                ctx.routed_model = ctx.model
                ctx.savings.routed_model = ctx.model
            # Backlog #60 follow-up: cascade tier calls set output_budget_raised as a
            # side effect of _tier_params / outgoing_params_for; an error fallback means
            # we'll call the NORMAL path which will re-set it correctly for the actual
            # model we're about to call. Clear the stale cascade tier disclosure.
            ctx.output_budget_raised = None
            ctx.savings.routing_mode = "cascade_planned+exec_error"
            logger.warning(
                "[%s] G06 deferred cascade errored (%s) — falling back to a normal "
                "call on %s", request_id,
                _redact_secrets((_c_resp or {}).get("error") if isinstance(_c_resp, dict) else _c_resp),
                ctx.routed_model,
            )

    # The cascade already produced the final answer (its provider time is
    # accumulated in ctx.llm_elapsed_ms). Return it directly — calling the LLM
    # again here would be a duplicate provider round-trip.
    if ctx.cascade_response is not None:
        logger.info(
            "[%s] Using G06 cascade result (model=%s); skipping duplicate main LLM call",
            request_id, ctx.routed_model,
        )
        return await _process_and_serve(ctx, ctx.cascade_response, _request_start)

    # F2 Intent Orchestration — the request matched a registered downstream agent and
    # IntentOrchestration already dispatched it there (its provider time is accumulated in
    # ctx.llm_elapsed_ms). Serve the agent's answer directly; calling the LLM here would
    # duplicate the round-trip. Mirrors the G06 cascade_response short-circuit above so the
    # agent response still runs response-side groups + billing via process_response.
    if ctx.agent_dispatched and ctx.agent_response is not None:
        logger.info(
            "[%s] Served by downstream agent '%s'; skipping main LLM call",
            request_id, ctx.agent_id,
        )
        return await _process_and_serve(ctx, ctx.agent_response, _request_start)

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
        # E4: snapshot INSIDE the invoke, not before building the target list — the
        # resilience layer may run a failover target instead, and each target captures
        # its own, so what is echoed is what the provider that actually served received.
        # Deliberately NOT `**_call_kwargs`: that is where the provider credential lives.
        _sent_messages = outgoing_messages_for(routed_adapter, ctx.messages)
        capture_sent_prompt(ctx, _sent_messages, outgoing_params, _call_model)
        resp = await litellm.acompletion(
            model=_call_model, messages=_sent_messages, **_call_kwargs, **outgoing_params,
            **_rcfg.call_limits(),
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
        raise HTTPException(status_code=401, detail="LLM provider authentication failed") from exc
    except litellm.exceptions.RateLimitError as exc:
        logger.warning("LLM rate limit: %s", _redact_secrets(exc))
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _emit_resilience_metrics(ctx)
        _record_outcome(ctx, _request_start, "429")
        raise HTTPException(status_code=429, detail="LLM provider rate limit reached") from exc
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
            raise HTTPException(status_code=429, detail="All providers rate limit reached") from exc
        if _last is None:
            # No target was even attemptable (e.g. no viable key on any target) —
            # near-unreachable thanks to fail-open, but never report it as an
            # upstream failure that didn't happen.
            _record_outcome(ctx, _request_start, "503")
            raise HTTPException(status_code=503, detail="No provider available for this request") from exc
        _record_outcome(ctx, _request_start, "502")
        raise HTTPException(status_code=502, detail="All providers failed (upstream error)") from exc
    except Exception as exc:
        # Item 12: never echo the raw upstream exception to the client (it can embed the
        # api_key/base_url) and redact secrets from the log line too.
        logger.error("LLM call failed: %s", _redact_secrets(exc))
        ctx.llm_elapsed_ms += (time.time() - _llm_start) * 1000
        _emit_resilience_metrics(ctx)
        _record_outcome(ctx, _request_start, "502")
        raise HTTPException(status_code=502, detail="LLM provider error (upstream call failed)") from exc

    # Book the served call against ctx.provider_calls, so G18's cost is the sum over
    # every provider call this request paid for rather than the price of one. Recorded
    # once, after resilience settled, at the model that actually won (_make_pin_winner
    # has already pinned it) — failed attempts cost nothing and are not recorded.
    record_provider_call(ctx, ctx.routed_model, response_dict)

    _llm_ms = (time.time() - _llm_start) * 1000
    # += (not =) so any provider time already accumulated by middleware (G06 judge/cascade
    # fallback, G10 summary, G09 schema) is preserved — the SLA split needs the TOTAL
    # provider time (including any failover retries).
    ctx.llm_elapsed_ms += _llm_ms
    try:
        STAGE_DURATION_MS.labels(
            stage="LLM-call", tenant_id=getattr(ctx, "tenant_id", "default"),
        ).observe(_llm_ms)
    except Exception as err:  # never let metrics break the response
        logger.debug("LLM-call duration metric not recorded: %r", err)
    (logger.warning if _llm_ms > 10000 else logger.info)(
        "[%s] LLM call %s completed in %.0fms", request_id, ctx.routed_model, _llm_ms
    )
    _emit_resilience_metrics(ctx)

    # Run the response-side pipeline (authoritative stage order: middleware/pipeline.py),
    # then finalise (savings metadata, headers, SLA metrics + billing) via the
    # shared helper.
    return await _process_and_serve(ctx, response_dict, _request_start)


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
            except Exception as exc:
                logger.debug("stream payload is not JSON, skipped: %r", exc)
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


async def _openai_one_shot_stream(openai_body: Dict, *, withhold_usage: bool):
    """A whole OpenAI completion sent as the stream the client asked for: one content
    chunk, then the usage chunk the way a live stream sends it (``choices: []``), then
    ``[DONE]``. The usage chunk follows the live stream's rule
    (:func:`_stream_options_with_usage`): kept from a client that sent stream_options
    without include_usage."""
    chunk = _completion_to_stream_chunk(openai_body)
    chunk["object"] = "chat.completion.chunk"
    chunk.setdefault("created", int(time.time()))
    usage = chunk.pop("usage", None)
    frames = [chunk]
    if usage and not withhold_usage:
        head = {k: chunk[k] for k in ("id", "object", "created", "model") if k in chunk}
        frames.append({**head, "choices": [], "usage": usage})
    translator = OPENAI.stream_translator()
    for line in translator.start():
        yield line
    for frame in frames:
        for out in translator.chunk(frame):
            yield out
    for line in translator.finish():
        yield line


def _as_requested_stream(resp, *, withhold_usage: bool):
    """Answer a stream=true OpenAI request with a stream, whichever path produced it.

    A cache hit, a G04 bypass, a G29/G30/G31 block, an F2 agent answer and a G06 cascade
    return a whole JSON completion. An SDK reading that body as a stream finds no events
    and shows an empty reply, while the request is billed as served. Such a body is sent
    as a one-chunk stream instead, as the Anthropic and Gemini routes already do
    (:func:`_translate_response`), with its ``x-*`` headers. A live stream, an error and
    the batch 202 pass through unchanged."""
    if not isinstance(resp, JSONResponse) or resp.status_code != 200:
        return resp
    try:
        body = json.loads(bytes(resp.body).decode("utf-8"))
    except Exception:
        return resp
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
        return resp
    headers = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-")}
    return StreamingResponse(_openai_one_shot_stream(body, withhold_usage=withhold_usage),
                             media_type=OPENAI.stream_media_type, headers=headers)


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
    except UnsupportedRequestField as exc:   # names the field, never request content
        eb, st = proto.serialise_error(400, str(exc))
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

async def _validate_key(api_key: str):
    """validate_proxy_key without making the event loop wait on the key store. A lookup
    that may reload the cache from a blob store (Secret Manager: a blocking RPC) runs in a
    worker thread; a cached fresh key, or an installed backend whose load never blocks on
    the loop, is answered here."""
    if key_store_backend_installed() or key_cached_and_fresh(api_key):
        return validate_proxy_key(api_key)
    return await asyncio.to_thread(validate_proxy_key, api_key)


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
    is_valid, user_id, tenant_metadata = await _validate_key(api_key)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid proxy API key. Contact your platform team for a key.",
        )
    # A key record whose tenant is empty or null is malformed (create_key refuses one): its
    # tenant cannot scope anything, and a tenant filter that tests truthy would drop out.
    if isinstance(tenant_metadata, dict) and "tenant_id" in tenant_metadata \
            and not tenant_metadata["tenant_id"]:
        logger.warning("Refused a proxy key whose record has an empty tenant_id")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This proxy key is not bound to a tenant.")
    # The key-bound identity, recorded BEFORE any X-User-ID override below: _serve_core
    # hands it to G00 as the rate-limit principal (an allow-listed override is still the
    # caller's choice). getattr: some callers pass a bare request object with no .state.
    _state = getattr(request, "state", None)
    if _state is not None:
        _state.key_principal = user_id

    # Suspended keys authenticate to a known tenant but are rejected here (403).
    # The suspended flag is set out-of-band by the key-store lifecycle; the proxy
    # only enforces it. Checked before any X-User-ID override so a suspended key
    # can never slip through the header path.
    if is_suspended(tenant_metadata):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key suspended. Contact your platform team.",
        )

    cfg = get_config()

    # Contract gate: a tenant whose contract is not active (pending / inactive /
    # expired) is rejected here (403), same immediacy as suspension. The flag is
    # mirrored into the key metadata by the commercial admin lifecycle
    # (set_contract_active); companies.contract_status is the source-of-record.
    # Checked before any X-User-ID override so the header path can't slip through.
    if is_contract_inactive(tenant_metadata):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Contract inactive. Contact your account administrator.",
        )

    # Source-IP allowlist: the caller's IP must fall in the union of the global CIDRs
    # (config.yaml, applied while ip_allowlist.enabled is on) and the tenant's own CIDRs
    # (key metadata, applied whenever the tenant has any: an operator who set them for a
    # tenant was told nothing when the global switch was off). Empty+empty ⇒ unrestricted.
    # Enforced pre-override for the same reason as the gates above.
    ipcfg = cfg.get("ip_allowlist", {}) or {}
    global_cidrs = (ipcfg.get("global_cidrs", []) or []) if ipcfg.get("enabled") else []
    tenant_cidrs = get_ip_allowlist(tenant_metadata)
    if global_cidrs or tenant_cidrs:
        client_ip = request_client_ip(request, cfg)
        if not ip_allowed(client_ip, global_cidrs, tenant_cidrs):
            logger.warning(
                "IP allowlist: rejected %s for tenant=%s",
                client_ip, _caller_tenant_id(tenant_metadata))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Source IP not allowed for this tenant.",
            )

    # Check for X-User-ID header override (per-user attribution within a tenant)
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


# (_outgoing_params_for now lives in providers.outgoing_params_for — imported above
# as an alias. Relocated 2026-08-08 so the deferred G06 cascade shares it, review S2.)


# Provider-scoped request params injected by G21 for the PRIMARY provider — they must
# never leak onto a failover target routed to a different provider (review P1). Prompt-
# cache markers (the caller's, or G21's) follow the target's own rule instead: kept for a
# provider that caches by marker, removed for any other (providers.outgoing_messages_for
# and outgoing_params_for), so a fallback to another Anthropic model keeps its caching.
_PRIMARY_SCOPED_PARAMS = ("prompt_cache_key", "prompt_cache_retention")


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
            # As on the primary: the usage chunk is always requested (whether the client
            # sees it is decided once, in _stream_response).
            outgoing["stream_options"] = _stream_options_with_usage(
                outgoing.get("stream_options"))[0]
        call_model, call_kwargs = adapter.build_call(
            model, get_provider_entry(model, providers_cfg) or {}, key
        )
        _failover_messages = outgoing_messages_for(adapter, ctx.messages)
        # E4: a failover target sends DIFFERENT bytes (its own messages/tools, a different
        # model). Capturing here overwrites the primary's snapshot, so the echo describes
        # the call that actually served rather than the one that failed.
        capture_sent_prompt(ctx, _failover_messages, outgoing, call_model)
        # A failover target exists only while the resilience layer is on, and it retries
        # this call itself: the client library adds no retries of its own.
        resp = await litellm.acompletion(
            model=call_model,
            messages=_failover_messages,
            **call_kwargs,
            **outgoing,
            timeout=ResilienceConfig.resolve(eff_cfg, provider).request_timeout_seconds,
            max_retries=0,
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
        # The ledger must name the model that ANSWERED. Pinning only ctx.routed_model
        # priced the failover correctly (G18 reads it) while _token_opt.routed_model, the
        # portal and the export all kept naming the model that had just FAILED.
        if getattr(ctx, "savings", None) is not None:
            ctx.savings.routed_model = t.model
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
        seen_models = set()
        for a in attempts:
            if a.provider and a.provider not in seen:
                seen.add(a.provider)
                CIRCUIT_BREAKER_STATE.labels(provider=a.provider).set(
                    BREAKER_STATE_CODE.get(store.peek_provider_state(a.provider), 0)
                )
            # Per-model lockout gauge — only for models actually tracked (feature on),
            # so the series set stays empty when model_lockout is off. 1=locked (OPEN).
            mk = (a.provider, a.model)
            if a.provider and a.model and mk not in seen_models \
                    and store.has_model_breaker(a.provider, a.model):
                seen_models.add(mk)
                MODEL_LOCKOUT_STATE.labels(provider=a.provider, model=a.model).set(
                    1 if store.peek_model_state(a.provider, a.model) is BreakerState.OPEN else 0
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
    # proxy_headers=False: uvicorn would otherwise rewrite the socket peer from
    # X-Forwarded-For whenever FORWARDED_ALLOW_IPS trusts the caller (net/client_ip.py).
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info", proxy_headers=False)
