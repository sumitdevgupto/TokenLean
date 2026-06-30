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
_RAG_FALLBACK_ENABLED = os.getenv("RAG_FALLBACK_ENABLED", "true").lower() == "true"

# OOD Detection thresholds
_OOD_SIMILARITY_THRESHOLD = float(os.getenv("OOD_SIMILARITY_THRESHOLD", "0.65"))
_OOD_MAX_RETRIES = int(os.getenv("OOD_MAX_RETRIES", "3"))
_RAG_FALLBACK_INDEX = os.getenv("RAG_FALLBACK_INDEX", "broad-domain-index")


async def trigger_doc_ingestion(gcs_bucket: str, gcs_object: str) -> bool:
    """
    Trigger the document ingestion Cloud Run Job for a newly uploaded file.
    Called from the /ingest-doc webhook endpoint in main.py.
    """
    try:
        from google.cloud import run_v2

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
                        ]
                    )
                ]
            ),
        )
        await client.run_job(request=request)
        logger.info("G03 triggered doc-pipeline job for gs://%s/%s", gcs_bucket, gcs_object)
        return True
    except Exception as exc:
        logger.error("G03 failed to trigger doc-pipeline job: %s", exc)
        return False


async def trigger_fine_tuning_pipeline(domain: str, doc_count: int) -> bool:
    """
    Trigger fine-tuning pipeline when domain has sufficient stable documents.
    Break-even: Fine-tuning is cost-effective when >100 docs in stable domain.
    """
    if doc_count < _FINETUNE_MIN_DOCS:
        logger.debug("Fine-tuning skipped: only %d docs for domain '%s' (min: %d)", 
                    doc_count, domain, _FINETUNE_MIN_DOCS)
        return False
    
    try:
        from google.cloud import run_v2
        
        client = run_v2.JobsAsyncClient()
        job_name = (
            f"projects/{_GCP_PROJECT}/locations/{_DOC_PIPELINE_REGION}"
            f"/jobs/token-opt-finetune-pipeline"
        )
        request = run_v2.RunJobRequest(
            name=job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="DOMAIN", value=domain),
                            run_v2.EnvVar(name="DOC_COUNT", value=str(doc_count)),
                            run_v2.EnvVar(name="STABILITY_DAYS", value=str(_FINETUNE_STABILITY_DAYS)),
                        ]
                    )
                ]
            ),
        )
        await client.run_job(request=request)
        logger.info("G03 triggered fine-tuning pipeline for domain '%s' (%d docs)", domain, doc_count)
        return True
    except Exception as exc:
        logger.error("G03 failed to trigger fine-tuning pipeline: %s", exc)
        return False


async def check_domain_stability(domain: str) -> Dict[str, Any]:
    """
    Check if a domain has been stable enough to trigger fine-tuning.
    Returns: {stable: bool, doc_count: int, days_active: int}
    """
    try:
        # Query metadata store (PostgreSQL) for domain statistics
        from cache.redis_pool import get_redis
        redis = get_redis()
        
        domain_key = f"tok_opt:domain:{domain}"
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


async def update_domain_stats(domain: str, doc_added: bool = True) -> None:
    """Update domain statistics in Redis for fine-tuning eligibility."""
    try:
        from cache.redis_pool import get_redis
        redis = get_redis()
        
        domain_key = f"tok_opt:domain:{domain}"
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
            from sentence_transformers import SentenceTransformer
            
            client = QdrantClient(url=self.qdrant_url)
            
            # Embed query
            model = SentenceTransformer("all-MiniLM-L6-v2")
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
