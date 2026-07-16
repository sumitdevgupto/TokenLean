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
  - Tika sidecar integration for document extraction
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
GROUP = "G03"

_GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "")
_DOC_PIPELINE_JOB = os.getenv("DOC_PIPELINE_JOB_NAME", "token-opt-doc-pipeline")
_DOC_PIPELINE_REGION = os.getenv("GCP_REGION", "us-central1")
_TIKA_SIDECAR_URL = os.getenv("TIKA_SIDECAR_URL", "http://tika-svc:9998")

# Fine-tuning configuration
_FINETUNE_MIN_DOCS = int(os.getenv("FINETUNE_MIN_DOCS", "100"))  # Min docs to trigger FT
_FINETUNE_STABILITY_DAYS = int(os.getenv("FINETUNE_STABILITY_DAYS", "30"))  # Domain stability
# Cloud Run Job name — env-configurable so the trigger and the deploy agree (mirrors
# DOC_PIPELINE_JOB_NAME). Default matches the name gcp-deploy.sh actually creates.
_FINETUNE_JOB = os.getenv("FINETUNE_PIPELINE_JOB_NAME", "finetune-pipeline-job")
_RAG_FALLBACK_ENABLED = os.getenv("RAG_FALLBACK_ENABLED", "true").lower() == "true"

# OOD Detection thresholds
_OOD_SIMILARITY_THRESHOLD = float(os.getenv("OOD_SIMILARITY_THRESHOLD", "0.65"))
_OOD_MAX_RETRIES = int(os.getenv("OOD_MAX_RETRIES", "3"))
_RAG_FALLBACK_INDEX = os.getenv("RAG_FALLBACK_INDEX", "broad-domain-index")


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
    BYOK provider key is resolved HERE (in-proxy, where the resolver lives) and passed as a
    Job secret. Under strict-BYOK with no tenant key, this raises FineTuneByokError (→ 402)
    rather than leaking the platform key.
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
    except ProviderKeyDecryptError:
        # A stored key exists but is undecryptable → fail closed, never the platform key.
        _emit_finetune_metric(tctx.tenant_id, "refused_byok", provider)
        raise FineTuneByokError(tctx.tenant_id, provider)
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
        if tenant_key:
            env.append(run_v2.EnvVar(name="TENANT_PROVIDER_KEY", value=tenant_key))
        request = run_v2.RunJobRequest(
            name=job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(env=env)
                ]
            ),
        )
        await client.run_job(request=request)
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


def _emit_finetune_metric(tenant_id: str, status: str, provider: str) -> None:
    """Increment the finetune-jobs counter. The Job runs out-of-process and can't push to
    the proxy registry, so submissions are counted here at trigger time."""
    try:
        from middleware.g18_observability import FINETUNE_JOBS_TOTAL
        FINETUNE_JOBS_TOTAL.labels(tenant_id=tenant_id, status=status, provider=provider).inc()
    except Exception:
        pass  # metrics are best-effort; never block a trigger on them


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
        similarity_threshold: float = 0.85
    ) -> List[Dict]:
        """
        Search with fallback strategy:
        1. Strict hybrid search (dense + sparse, high threshold)
        2. Relaxed hybrid search (lower threshold)
        3. Dense-only search
        4. Sparse-only search (BM25-style)
        5. Return empty (no results found)
        """
        if not self.fallback_enabled:
            return await self._strict_hybrid_search(query, collection, top_k, similarity_threshold)
        
        strategies = [
            ("strict_hybrid", 0.85),
            ("relaxed_hybrid", 0.70),
            ("dense_only", 0.75),
            ("sparse_only", 0.60),
        ]
        
        for strategy, threshold in strategies:
            results = await self._execute_search(strategy, query, collection, top_k, threshold)
            if results:
                logger.debug("RAG fallback: used strategy '%s' with %d results", strategy, len(results))
                return results
        
        logger.debug("RAG fallback: no results found with any strategy")
        return []
    
    async def _execute_search(
        self, 
        strategy: str, 
        query: str, 
        collection: str, 
        top_k: int, 
        threshold: float
    ) -> List[Dict]:
        """Execute search with specified strategy."""
        try:
            from qdrant_client import QdrantClient
            from ml_models import qdrant_client_kwargs

            client = QdrantClient(**qdrant_client_kwargs(url=self.qdrant_url))
            
            # Embed query — shared loader (cached singleton + HF_HUB_OFFLINE guard so the
            # baked model loads without an HF-CDN metadata call that hangs under VPC egress).
            from ml_models import get_sentence_transformer
            model = get_sentence_transformer("all-MiniLM-L6-v2")
            query_embedding = model.encode(query).tolist()
            
            if strategy == "strict_hybrid":
                # Use Qdrant's hybrid search (requires proper setup)
                results = client.search(
                    collection_name=collection,
                    query_vector=("dense", query_embedding),
                    limit=top_k,
                    score_threshold=threshold,
                )
            elif strategy == "relaxed_hybrid":
                results = client.search(
                    collection_name=collection,
                    query_vector=("dense", query_embedding),
                    limit=top_k * 2,  # Get more results for re-ranking
                    score_threshold=threshold,
                )
            elif strategy == "dense_only":
                results = client.search(
                    collection_name=collection,
                    query_vector=("dense", query_embedding),
                    limit=top_k,
                    score_threshold=threshold,
                )
            elif strategy == "sparse_only":
                # Use sparse/BM25 search (simplified - would need sparse query)
                results = []
            else:
                results = []
            
            return [{"text": r.payload.get("text", ""), "score": r.score} for r in results]
        except Exception as exc:
            logger.debug("Search strategy '%s' failed: %s", strategy, exc)
            return []
    
    async def _strict_hybrid_search(self, query, collection, top_k, threshold):
        """Default strict hybrid search without fallback."""
        return await self._execute_search("strict_hybrid", query, collection, top_k, threshold)
    
    async def detect_ood_and_fallback(
        self,
        query: str,
        primary_collection: str,
        fallback_collection: str = _RAG_FALLBACK_INDEX,
        top_k: int = 5,
    ) -> Dict:
        """
        Detect OOD (Out-of-Distribution) query and fallback to broader index.
        
        Returns:
            {
                "is_ood": bool,
                "primary_results": List[Dict],
                "fallback_results": List[Dict],
                "confidence": float,  # 0-1, similarity to domain
                "strategy_used": str,
            }
        """
        # First try primary collection
        primary_results = await self._strict_hybrid_search(
            query, primary_collection, top_k, _OOD_SIMILARITY_THRESHOLD
        )
        
        if primary_results:
            # Calculate average confidence from primary results
            avg_confidence = sum(r.get("score", 0) for r in primary_results) / len(primary_results)
            
            # If confidence is high enough, not OOD
            if avg_confidence >= _OOD_SIMILARITY_THRESHOLD:
                return {
                    "is_ood": False,
                    "primary_results": primary_results,
                    "fallback_results": [],
                    "confidence": avg_confidence,
                    "strategy_used": "primary_strict",
                }
        
        # Try fallback strategies
        if _RAG_FALLBACK_ENABLED:
            # Try relaxed search on primary
            relaxed_results = await self._execute_search(
                "relaxed_hybrid", query, primary_collection, top_k, _OOD_SIMILARITY_THRESHOLD * 0.8
            )
            
            if relaxed_results:
                avg_confidence = sum(r.get("score", 0) for r in relaxed_results) / len(relaxed_results)
                return {
                    "is_ood": False,
                    "primary_results": relaxed_results,
                    "fallback_results": [],
                    "confidence": avg_confidence,
                    "strategy_used": "primary_relaxed",
                }
            
            # Fallback to broad domain index
            fallback_results = await self._strict_hybrid_search(
                query, fallback_collection, top_k, _OOD_SIMILARITY_THRESHOLD * 0.7
            )
            
            if fallback_results:
                return {
                    "is_ood": True,  # Query was OOD for primary, found in fallback
                    "primary_results": primary_results,
                    "fallback_results": fallback_results,
                    "confidence": max(r.get("score", 0) for r in fallback_results),
                    "strategy_used": "fallback_broad_domain",
                }
        
        # No results anywhere
        return {
            "is_ood": True,
            "primary_results": primary_results,
            "fallback_results": [],
            "confidence": 0.0,
            "strategy_used": "no_results",
        }


class TikaSidecarClient:
    """Apache Tika sidecar client for document text extraction."""
    
    def __init__(self, base_url: str = _TIKA_SIDECAR_URL):
        self.base_url = base_url.rstrip("/")
    
    async def extract_text(self, content: bytes, filename: str) -> str:
        """Extract text using Tika sidecar HTTP endpoint."""
        try:
            import httpx
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    f"{self.base_url}/tika",
                    content=content,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Accept": "text/plain",
                        "X-Filename": filename,
                    },
                )
                resp.raise_for_status()
                return resp.text
        except Exception as exc:
            logger.warning("Tika extraction failed: %s — falling back to unstructured", exc)
            return ""
    
    async def extract_metadata(self, content: bytes, filename: str) -> Dict[str, Any]:
        """Extract document metadata using Tika."""
        try:
            import httpx
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    f"{self.base_url}/tika",
                    content=content,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Accept": "application/json",
                        "X-Filename": filename,
                    },
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.debug("Tika metadata extraction failed: %s", exc)
            return {}


class G03DocPipeline:
    """G03 middleware with fine-tuning trigger and RAG fallback support."""
    
    def __init__(self):
        self.rag_orchestrator = RAGFallbackOrchestrator()
        self.tika_client = TikaSidecarClient()

    async def process_request(self, ctx: Any) -> Any:
        """Hook for RAG fallback during request processing."""
        cfg = ctx.config.get("groups", {}).get("G3_doc_pipeline", {})
        if not cfg.get("enabled", False):
            return ctx
        
        # Check if this is a RAG query that needs fallback
        if hasattr(ctx, "rag_query") and ctx.rag_query:
            results = await self.rag_orchestrator.search_with_fallback(
                ctx.rag_query,
                collection=cfg.get("collection", "rag_docs"),
                top_k=cfg.get("top_k", 5),
            )
            ctx.rag_results = results
        
        return ctx
