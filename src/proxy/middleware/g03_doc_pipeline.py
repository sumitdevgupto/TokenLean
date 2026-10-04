"""
G03 · Knowledge Strategy — Document Pipeline Trigger
Stage: Before the Request (companion pipeline — not inline optimisation)
Saving: Up to 100% retrieval context elimination for stable domains.
Technique: When a document upload event arrives (GCS object notification), trigger
           the doc-pipeline Cloud Run Job asynchronously. This module handles
           the GCS event routing — not the proxy request path.
           The doc-pipeline itself lives in src/doc-pipeline/pipeline.py.
           
Features:
  - Fine-tuning pipeline trigger for stable domains (break-even detection)
  - RAG fallback orchestration (hybrid search → fallback to broader)
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
GROUP = "G03"

_GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "")
_DOC_PIPELINE_JOB = os.getenv("DOC_PIPELINE_JOB_NAME", "token-opt-doc-pipeline")
_DOC_PIPELINE_REGION = os.getenv("GCP_REGION", "us-central1")

# Fine-tuning configuration
_FINETUNE_MIN_DOCS = int(os.getenv("FINETUNE_MIN_DOCS", "100"))  # Min docs to trigger FT
_FINETUNE_STABILITY_DAYS = int(os.getenv("FINETUNE_STABILITY_DAYS", "30"))  # Domain stability
# Cloud Run Job name — env-configurable so the trigger and the deploy agree (mirrors
# DOC_PIPELINE_JOB_NAME). Default matches the name gcp-deploy.sh actually creates.
_FINETUNE_JOB = os.getenv("FINETUNE_PIPELINE_JOB_NAME", "finetune-pipeline-job")
# The Secret Manager secret a tenant's own provider key reaches the fine-tune Job through: a
# version per run, and the Job is given only the version's NAME. An execution keeps its env
# overrides, readable by anyone who can view the Job, so the key itself never goes there. The
# Job destroys its version once the run has ended cleanly; a version older than a day (a run
# ends within the hour) was left by a failed run, and the next trigger destroys it.
_FINETUNE_KEY_SECRET = os.getenv("FINETUNE_KEY_SECRET", "finetune-tenant-key")
_FINETUNE_KEY_MAX_AGE_SECONDS = 86400
_RAG_FALLBACK_ENABLED = os.getenv("RAG_FALLBACK_ENABLED", "true").lower() == "true"

# The fallback chain, and how far each strategy lowers the similarity threshold
# (G3_doc_pipeline.rag_fallback.similarity_threshold, default 0.85 -> 0.85/0.70/0.75).
_FALLBACK_STRATEGIES = ("strict_hybrid", "relaxed_hybrid", "dense_only", "sparse_only")
_FALLBACK_RELAXATION = {"strict_hybrid": 0.0, "relaxed_hybrid": 0.15, "dense_only": 0.10,
                        "sparse_only": 0.25}


def _embed_query(query: str, model_name: str) -> List[float]:
    """Dense query vector. Blocking (and the first call loads the model): run it in a
    thread. Shared loader: cached singleton + HF_HUB_OFFLINE guard, so the baked model
    loads without an HF-CDN metadata call that hangs under VPC egress."""
    from ml_models import get_sentence_transformer
    return get_sentence_transformer(model_name).encode(query).tolist()


async def trigger_doc_ingestion(
    gcs_bucket: str, gcs_object: str, tenant_id: str = "default"
) -> bool:
    """
    Trigger the document ingestion Cloud Run Job for a newly uploaded file.
    Called from the /ingest-doc webhook endpoint in main.py.

    The tenant's Qdrant collection is derived via TenantContext — the SAME code the
    read path (G07 _resolve_collection → ctx.qdrant_collection) uses — so ingest writes
    land in exactly the collection retrieval reads from (rag_<tenant>, or rag_docs for
    the default/single-tenant deploy). This closes the old read/write asymmetry where
    writes always went to the shared rag_docs.
    """
    try:
        from google.cloud import run_v2
        from tenancy.context import TenantContext

        tctx = TenantContext.for_tenant(tenant_id)
        collection = tctx.qdrant_collection

        client = run_v2.JobsAsyncClient()
        job_name = (
            f"projects/{_GCP_PROJECT}/locations/{_DOC_PIPELINE_REGION}"
            f"/jobs/{_DOC_PIPELINE_JOB}"
        )
        request = run_v2.RunJobRequest(
            name=job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="GCS_BUCKET", value=gcs_bucket),
                            run_v2.EnvVar(name="GCS_OBJECT", value=gcs_object),
                            run_v2.EnvVar(name="QDRANT_COLLECTION", value=collection),
                            run_v2.EnvVar(name="TENANT_ID", value=tctx.tenant_id),
                        ]
                    )
                ]
            ),
        )
        await client.run_job(request=request)
        logger.info(
            "G03 triggered doc-pipeline job for gs://%s/%s → tenant=%s collection=%s",
            gcs_bucket, gcs_object, tctx.tenant_id, collection,
        )
        return True
    except Exception as exc:
        logger.error("G03 failed to trigger doc-pipeline job: %s", exc)
        return False


class FineTuneByokError(Exception):
    """Raised when strict-BYOK is on and the tenant has no provider key for training.

    The admin/trigger caller maps this to HTTP 402 — a tenant fine-tune must never fall
    back to the platform provider key (that would train the tenant's model on the
    platform account and cross the isolation boundary)."""

    def __init__(self, tenant_id: str, provider: str):
        self.tenant_id = tenant_id
        self.provider = provider
        super().__init__(f"No provider key for tenant {tenant_id!r} / provider {provider!r} (strict BYOK)")


async def trigger_fine_tuning_pipeline(
    tenant_id: str,
    domain: str,
    doc_count: int,
    *,
    provider: str = "openai",
    get_config=None,
) -> bool:
    """
    Trigger the fine-tuning Cloud Run Job for one tenant's domain corpus.

    Tenant-isolated: the Job reads ONLY the tenant's collection (rag_<tenant>) filtered by
    tenant_id, exports under finetune-training/<tenant>/<domain>/, and tenant-prefixes its
    Redis keys. Because the Job is a separate process with no key resolver, the tenant's
    BYOK provider key is resolved HERE (in-proxy, where the resolver lives), stored as a new
    version of the fine-tune key secret, and the Job is given only that version's name. Under
    strict-BYOK with no tenant key, this raises FineTuneByokError (→ 402) rather than leaking
    the platform key.
    """
    from tenancy.context import TenantContext

    tctx = TenantContext.for_tenant(tenant_id)

    if doc_count < _FINETUNE_MIN_DOCS:
        logger.debug("Fine-tuning skipped: only %d docs for tenant '%s' domain '%s' (min: %d)",
                    doc_count, tctx.tenant_id, domain, _FINETUNE_MIN_DOCS)
        return False

    # Resolve the tenant's OWN provider key at trigger time (the Job has no resolver). Using
    # the tenant-owned seam — NOT resolve_provider_key — is what prevents the platform-key
    # leak: resolve_provider_key falls back to the platform key when strict BYOK is off, and
    # the fine-tune path can't tell that fallback apart from a genuine tenant key. The
    # tenant-owned resolver returns a key ONLY when it is genuinely the tenant's; None means
    # the tenant has no key of its own, and strict-BYOK config decides refuse-vs-allow.
    tenant_key = ""
    byok_enforce = _finetune_byok_enforced(get_config)
    from providers.key_resolver import ProviderKeyDecryptError, resolve_tenant_owned_key
    try:
        tenant_key = await resolve_tenant_owned_key(provider, tctx.tenant_id) or ""
    except ProviderKeyDecryptError as exc:
        # A stored key exists but is undecryptable → fail closed, never the platform key.
        _emit_finetune_metric(tctx.tenant_id, "refused_byok", provider)
        raise FineTuneByokError(tctx.tenant_id, provider) from exc
    except Exception as exc:
        # Transient resolver error (e.g. DB blip) → treat as "no tenant key" and let the
        # strict-BYOK gate below decide, rather than silently proceeding on a platform key.
        logger.warning("Fine-tune tenant-key resolve failed for %s: %s", tctx.tenant_id, exc)

    if not tenant_key and byok_enforce:
        # Strict BYOK is on and the tenant has no key of its own — refuse (→ 402). NEVER
        # fall back to the platform key for a tenant fine-tune.
        _emit_finetune_metric(tctx.tenant_id, "refused_byok", provider)
        raise FineTuneByokError(tctx.tenant_id, provider)
    # Enforce in the Job whenever we actually have a tenant key (defense-in-depth), so a
    # direct `gcloud run jobs execute` can't silently train on the platform key either.
    byok_enforce = byok_enforce or bool(tenant_key)

    try:
        from google.cloud import run_v2

        client = run_v2.JobsAsyncClient()
        job_name = (
            f"projects/{_GCP_PROJECT}/locations/{_DOC_PIPELINE_REGION}"
            f"/jobs/{_FINETUNE_JOB}"
        )
        env = [
            run_v2.EnvVar(name="TENANT_ID", value=tctx.tenant_id),
            run_v2.EnvVar(name="QDRANT_COLLECTION", value=tctx.qdrant_collection),
            run_v2.EnvVar(name="DOMAIN", value=domain),
            run_v2.EnvVar(name="DOC_COUNT", value=str(doc_count)),
            run_v2.EnvVar(name="STABILITY_DAYS", value=str(_FINETUNE_STABILITY_DAYS)),
            run_v2.EnvVar(name="PROVIDER", value=provider),
            run_v2.EnvVar(name="BYOK_ENFORCE", value="true" if byok_enforce else "false"),
        ]
        key_version = ""
        if tenant_key:
            from google.cloud import secretmanager
            secrets = secretmanager.SecretManagerServiceAsyncClient()
            key_version = await _store_training_key(secrets, tenant_key)
            env.append(run_v2.EnvVar(name="TENANT_PROVIDER_KEY_VERSION", value=key_version))
        try:
            request = run_v2.RunJobRequest(
                name=job_name,
                overrides=run_v2.RunJobRequest.Overrides(
                    container_overrides=[
                        run_v2.RunJobRequest.Overrides.ContainerOverride(env=env)
                    ]
                ),
            )
            await client.run_job(request=request)
        except Exception:
            if key_version:   # no Job will read it
                await _destroy_key_version(secrets, key_version)
            raise
        logger.info(
            "G03 triggered fine-tuning for tenant '%s' domain '%s' (%d docs) → collection %s",
            tctx.tenant_id, domain, doc_count, tctx.qdrant_collection,
        )
        _emit_finetune_metric(tctx.tenant_id, "submitted", provider)
        return True
    except Exception as exc:
        logger.error("G03 failed to trigger fine-tuning pipeline: %s", exc)
        _emit_finetune_metric(tctx.tenant_id, "trigger_error", provider)
        return False


async def _store_training_key(secrets, key: str) -> str:
    """Add ``key`` as a new version of the fine-tune key secret and return the version's
    name. Also destroys the versions failed runs left behind (older than a day)."""
    parent = f"projects/{_GCP_PROJECT}/secrets/{_FINETUNE_KEY_SECRET}"
    version = await secrets.add_secret_version(
        request={"parent": parent, "payload": {"data": key.encode("utf-8")}})
    try:
        cutoff = time.time() - _FINETUNE_KEY_MAX_AGE_SECONDS
        async for old in await secrets.list_secret_versions(
                request={"parent": parent, "filter": "state:ENABLED"}):
            if old.create_time.timestamp() < cutoff:
                await secrets.destroy_secret_version(request={"name": old.name})
    except Exception as exc:
        logger.warning("G03: could not destroy old fine-tune key versions: %s", exc)
    return version.name


async def _destroy_key_version(secrets, name: str) -> None:
    try:
        await secrets.destroy_secret_version(request={"name": name})
    except Exception as exc:
        logger.warning("G03: could not destroy fine-tune key version %s: %s", name, exc)


def _emit_finetune_metric(tenant_id: str, status: str, provider: str) -> None:
    """Increment the finetune-jobs counter. The Job runs out-of-process and can't push to
    the proxy registry, so submissions are counted here at trigger time."""
    try:
        from middleware.g18_observability import FINETUNE_JOBS_TOTAL
        FINETUNE_JOBS_TOTAL.labels(tenant_id=tenant_id, status=status, provider=provider).inc()
    except Exception as exc:
        # metrics are best-effort; never block a trigger on them
        logger.debug("fine-tune job metric not recorded: %r", exc)


def _finetune_byok_enforced(get_config=None) -> bool:
    """Is strict BYOK enforced (byok.enforce)? Same config knob commercial_app + the chat
    path read, resolved once here so the fine-tune trigger's refuse-vs-allow decision is a
    single source of truth (not re-derived inside the Job). Defaults False (OSS/self-host)."""
    try:
        if get_config is None:
            from config_loader import get_config as _gc
            cfg = _gc() or {}
        else:
            cfg = get_config() or {}
        byok = (cfg.get("byok", {}) or {})
        return str(byok.get("enforce", False)).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def _decode(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


async def list_tenant_finetune_jobs(redis, tenant_id: str, domain=None, limit: int = 20) -> list:
    """Return a tenant's fine-tune jobs (status + model id) from its tenant-prefixed Redis
    keys, newest-first. Shared by the portal (self-serve) and admin (operator) status views so
    the key layout + decode logic live in ONE place.

    The per-job hashes are fetched with a single pipelined round trip (not N sequential
    hgetall calls) so a tenant with many jobs doesn't cost N Redis RTTs per page load.
    """
    from tenancy.context import TenantContext
    prefix = TenantContext.for_tenant(tenant_id).redis_prefix
    limit = max(1, min(int(limit), 100))

    if domain:
        ids = await redis.zrevrange(f"{prefix}tok_opt:finetune:domain:{domain}", 0, limit - 1)
        ids = [_decode(i) for i in ids]
    else:
        ids = []
        async for key in redis.scan_iter(match=f"{prefix}tok_opt:finetune:*"):
            k = _decode(key)
            if ":domain:" in k:
                continue  # skip the per-domain zset index keys
            ids.append(k.rsplit("tok_opt:finetune:", 1)[-1])
            if len(ids) >= limit:
                break

    if not ids:
        return []

    # Batch the hash fetches into ONE pipelined round trip instead of N sequential calls.
    try:
        pipe = redis.pipeline()
        for jid in ids:
            pipe.hgetall(f"{prefix}tok_opt:finetune:{jid}")
        results = await pipe.execute()
    except Exception:
        # Fallback for a redis client/mock without pipeline support — sequential fetch.
        results = [await redis.hgetall(f"{prefix}tok_opt:finetune:{jid}") for jid in ids]

    jobs = []
    for data in results:
        if data:
            jobs.append({_decode(k): _decode(v) for k, v in data.items()})
    return jobs


def _domain_stats_key(tenant_id: str, domain: str) -> str:
    """Tenant-prefixed domain-stats key (t:<tenant>:tok_opt:domain:<domain>), matching the
    TenantContext.redis_prefix convention used across the pipeline (G18 etc.)."""
    from tenancy.context import TenantContext
    return f"{TenantContext.for_tenant(tenant_id).redis_prefix}tok_opt:domain:{domain}"


async def check_domain_stability(domain: str, tenant_id: str = "default") -> Dict[str, Any]:
    """
    Check if a tenant's domain has been stable enough to trigger fine-tuning.
    Returns: {stable: bool, doc_count: int, days_active: int}
    """
    try:
        # Query metadata store (PostgreSQL) for domain statistics
        from cache.redis_pool import get_redis
        redis = get_redis()

        domain_key = _domain_stats_key(tenant_id, domain)
        stats = await redis.hgetall(domain_key)
        
        if not stats:
            return {"stable": False, "doc_count": 0, "days_active": 0}
        
        doc_count = int(stats.get("doc_count", 0))
        first_seen = float(stats.get("first_seen", 0))
        days_active = (time.time() - first_seen) / 86400 if first_seen else 0
        
        is_stable = (
            doc_count >= _FINETUNE_MIN_DOCS and 
            days_active >= _FINETUNE_STABILITY_DAYS
        )
        
        return {
            "stable": is_stable,
            "doc_count": doc_count,
            "days_active": int(days_active),
        }
    except Exception as exc:
        logger.debug("Domain stability check failed: %s", exc)
        return {"stable": False, "doc_count": 0, "days_active": 0}


async def update_domain_stats(domain: str, doc_added: bool = True, tenant_id: str = "default") -> None:
    """Update a tenant's domain statistics in Redis for fine-tuning eligibility."""
    try:
        from cache.redis_pool import get_redis
        redis = get_redis()

        domain_key = _domain_stats_key(tenant_id, domain)
        now = time.time()
        
        # Initialize first_seen if new domain
        exists = await redis.exists(domain_key)
        if not exists:
            await redis.hset(domain_key, "first_seen", str(now))
        
        if doc_added:
            await redis.hincrby(domain_key, "doc_count", 1)
        
        await redis.hset(domain_key, "last_updated", str(now))
        await redis.expire(domain_key, 90 * 86400)  # 90 day TTL
    except Exception as exc:
        logger.debug("Domain stats update failed: %s", exc)


class RAGFallbackOrchestrator:
    """
    RAG fallback: When primary search returns no results, 
    progressively broaden search strategy.
    """
    
    def __init__(self, qdrant_url: str = "http://localhost:6333"):
        self.qdrant_url = qdrant_url
        self.fallback_enabled = _RAG_FALLBACK_ENABLED
    
    async def search_with_fallback(
        self,
        query: str,
        collection: str = "rag_docs",
        top_k: int = 5,
        similarity_threshold: float = 0.85,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """
        Search with fallback strategy, broadening until one finds something:
        1. Strict hybrid search (dense, the similarity threshold)
        2. Relaxed hybrid search (threshold - 0.15, twice the results)
        3. Dense-only search (threshold - 0.10)
        4. Sparse-only search (BM25-style) — not built: needs a sparse query vector
        ``cfg`` is G3_doc_pipeline's block: its ``rag_fallback`` sets ``enabled``,
        ``strategies``, ``similarity_threshold`` and ``top_k``.
        """
        fb = (cfg or {}).get("rag_fallback") or {}
        enabled = fb.get("enabled", self.fallback_enabled)
        base = float(fb.get("similarity_threshold", similarity_threshold))
        top_k = int(fb.get("top_k", top_k))
        names = list(fb.get("strategies") or _FALLBACK_STRATEGIES) if enabled else ["strict_hybrid"]
        plan = [(name, base - _FALLBACK_RELAXATION[name]) for name in names
                if name in _FALLBACK_RELAXATION]
        model = (cfg or {}).get("dense_model") or "all-MiniLM-L6-v2"
        return await self._search(plan, query, collection, top_k, model)

    async def _search(self, plan, query: str, collection: str, top_k: int,
                      model: str = "all-MiniLM-L6-v2") -> List[Dict]:
        """Run ``plan`` ((strategy, threshold) pairs) in order; the first that finds
        anything wins. One async client, closed whatever happens, and one embedding,
        computed off the event loop, serve every strategy. A collection that does not
        exist is not searched (no embedding either)."""
        client = None
        try:
            from qdrant_client import AsyncQdrantClient
            from ml_models import qdrant_client_kwargs

            client = AsyncQdrantClient(**qdrant_client_kwargs(url=self.qdrant_url))
            if not await client.collection_exists(collection):
                return []
            vector = await asyncio.to_thread(_embed_query, query, model)
            for strategy, threshold in plan:
                results = await self._execute_search(
                    strategy, client, vector, collection, top_k, threshold)
                if results:
                    logger.debug("RAG fallback: used strategy '%s' with %d results",
                                 strategy, len(results))
                    return results
            logger.debug("RAG fallback: no results found with any strategy")
            return []
        except Exception as exc:
            logger.debug("RAG fallback search failed: %s", exc)
            return []
        finally:
            if client is not None:
                await client.close()

    async def _execute_search(
        self, strategy: str, client: Any, vector: List[float], collection: str,
        top_k: int, threshold: float,
    ) -> List[Dict]:
        """One strategy's search over the shared client and query vector."""
        if strategy not in ("strict_hybrid", "relaxed_hybrid", "dense_only"):
            return []  # sparse_only needs a sparse query vector, which this path does not build
        limit = top_k * 2 if strategy == "relaxed_hybrid" else top_k  # more, for re-ranking
        try:
            response = await client.query_points(
                collection_name=collection, query=vector, using="dense", limit=limit,
                score_threshold=threshold, with_payload=True)
            return [{"text": (p.payload or {}).get("text", ""), "score": p.score}
                    for p in response.points]
        except Exception as exc:
            logger.debug("Search strategy '%s' failed: %s", strategy, exc)
            return []
