"""
G07 · Retrieval Optimisation
Stage: Into the LLM
Saving: 50–80% context tokens
Technique: Hybrid dense + sparse (SPLADE BM25) retrieval via Qdrant prefetch + RRF
           fusion, followed by cross-encoder rerank → top-1/2.
           Named vectors: dense (cosine) + sparse (dot) stored in same collection.
           
Features:
  - JIT toggle: Just-in-time retrieval enablement
  - pgvector RAG fallback: PostgreSQL/pgvector as alternative vector store
  - Chunk guard: Size limits and validation for retrieved chunks
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing
from middleware.g03_doc_pipeline import RAGFallbackOrchestrator
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G07"

_QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
_QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_docs")
_QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")   # Static key override; auto-fetched from GCP if empty
_DENSE_MODEL = os.getenv("DENSE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
_SPARSE_MODEL = os.getenv("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25")
_PGVECTOR_URL = os.getenv("DATABASE_URL", "")  # PostgreSQL connection for pgvector fallback

# GCP Cloud Run IAM auth (identity-token Bearer) is attached centrally by
# ml_models.qdrant_client_kwargs — the old per-module token fetch lived here but sent
# the token via the WRONG header (qdrant-client's api_key → `api-key`, which Cloud Run
# IAM ignores; the Bearer needs auth_token_provider). _QDRANT_API_KEY remains the
# Qdrant APP-LAYER key (defense-in-depth behind IAM), passed as api_key below.


class ChunkGuard:
    """Validate and guard against oversized chunks from retrieval."""
    
    def __init__(self, max_chunk_tokens: int = 1000, max_total_tokens: int = 4000):
        self.max_chunk_tokens = max_chunk_tokens
        self.max_total_tokens = max_total_tokens
    
    def validate_chunks(self, chunks: List[Dict]) -> List[Dict]:
        """Filter chunks that exceed size limits."""
        valid = []
        total_tokens = 0
        
        for chunk in chunks:
            text = chunk.get("text", "")
            # Rough token estimate: 1 token ≈ 4 chars
            chunk_tokens = len(text) // 4
            
            if chunk_tokens > self.max_chunk_tokens:
                logger.warning("Chunk exceeds max size: %d tokens > %d limit", 
                             chunk_tokens, self.max_chunk_tokens)
                continue
            
            if total_tokens + chunk_tokens > self.max_total_tokens:
                logger.debug("Skipping chunk: would exceed total token limit")
                break
            
            valid.append(chunk)
            total_tokens += chunk_tokens
        
        return valid
    
    def truncate_chunk(self, text: str, max_tokens: Optional[int] = None) -> str:
        """Truncate text to max tokens."""
        if max_tokens is None:
            max_tokens = self.max_chunk_tokens
        max_chars = max_tokens * 4
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "... [truncated]"


# I3: the pgvector fallback interpolates the collection name straight into the
# SQL `FROM {collection}` (asyncpg cannot parameterise an identifier), so the
# name MUST be validated against a strict allowlist first — both to stop SQL
# injection and to keep retrieval inside the rag_* / tenant namespace.
#
# The allowlist matches EXACTLY what TenantContext.for_tenant produces (rag_<sanitised>,
# where sanitise_tenant_id allows [A-Za-z0-9_-], so real tenant ids like NOVA-STG-01 yield
# rag_NOVA-STG-01 — uppercase + hyphens). The old lowercase-only-no-hyphen pattern silently
# rejected every standard {CODE4}-{ENV}-{NN} tenant's collection, breaking pgvector RAG for
# them. Uppercase/hyphenated names are not valid UNQUOTED SQL identifiers, so the name is
# double-quoted at interpolation (_quote_collection); the allowlist forbids '"' so the quote
# cannot be broken out of.
_VALID_COLLECTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")


def _is_valid_collection_name(name: str) -> bool:
    return bool(name and _VALID_COLLECTION_RE.match(name))


def _quote_collection(name: str) -> str:
    """Double-quote a validated collection name for safe use as a SQL identifier.

    Only call on a name that passed _is_valid_collection_name (which forbids '"'), so no
    quote-escaping is possible — this just makes uppercase/hyphenated identifiers valid."""
    return '"' + name + '"'


async def _pgvector_search(
    query: str, top_k: int, db_url: str, collection: str, cfg: Dict
) -> List[Dict]:
    """Fallback search using PostgreSQL/pgvector."""
    if not db_url:
        return []

    # I3: never interpolate an unvalidated identifier into the FROM clause.
    if not _is_valid_collection_name(collection):
        logger.warning("G07 pgvector: rejected invalid collection name %r", collection)
        return []

    try:
        from cache.pg_pool import get_pg_pool

        # Embed query (threaded via _embed_dense so it doesn't block the loop).
        query_embedding = await _embed_dense(query)
        embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        pool = await get_pg_pool(db_url)
        async with pool.acquire() as conn:
            # Cosine similarity search in pgvector. The collection is double-quoted so
            # uppercase/hyphenated tenant collection names (rag_NOVA-STG-01) are valid
            # identifiers; safe because it passed the _VALID_COLLECTION_RE allowlist (no '"').
            rows = await conn.fetch(
                f"""
                SELECT text, source, 1 - (embedding <=> $1::vector) as score
                FROM {_quote_collection(collection)}
                WHERE 1 - (embedding <=> $1::vector) >= $2
                ORDER BY score DESC
                LIMIT $3
                """,
                embedding_str,
                cfg.get("similarity_threshold", 0.85),
                top_k,
            )

            return [
                {"text": r["text"], "source": r["source"], "score": r["score"]}
                for r in rows
            ]
    except Exception as exc:
        logger.debug("pgvector fallback search failed: %s", exc)
        return []


def _resolve_collection(ctx, cfg: Dict) -> str:
    """Resolve the RAG collection for a request (C2).

    The client-supplied ``x_rag_collection`` header is honoured ONLY for admin
    keys — otherwise a tenant could point retrieval at another tenant's
    collection (e.g. ``X-Rag-Collection: rag_<victim>``). Non-admin callers are
    pinned to their own tenant collection: operator config > tenant-scoped
    default > env-var fallback.
    """
    tenant_collection = (
        cfg.get("collection")
        or getattr(ctx, "qdrant_collection", None)
        or _QDRANT_COLLECTION
    )
    client_collection = getattr(ctx, "params", {}).get("x_rag_collection")
    if client_collection and getattr(ctx, "is_admin_key", False):
        return client_collection
    if client_collection and client_collection != tenant_collection:
        logger.warning(
            "[%s] G07 ignoring x_rag_collection=%r for non-admin tenant %s — using %s",
            getattr(ctx, "request_id", "?"), client_collection,
            getattr(ctx, "tenant_id", "default"), tenant_collection,
        )
    return tenant_collection


class G07Retrieval:
    """Retrieval with JIT toggle, pgvector fallback, and chunk guard."""
    
    def __init__(self):
        self._chunk_guard: Optional[ChunkGuard] = None
        self._rag_fallback = RAGFallbackOrchestrator(_QDRANT_URL)
    
    def _get_chunk_guard(self, cfg: Dict) -> ChunkGuard:
        if self._chunk_guard is None:
            max_chunk = cfg.get("max_chunk_tokens", 1000)
            max_total = cfg.get("max_total_context_tokens", 4000)
            self._chunk_guard = ChunkGuard(max_chunk, max_total)
        return self._chunk_guard
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G7_retrieval", {})
        if not cfg.get("enabled", False):
            return ctx
        
        # JIT toggle: Check if JIT retrieval is enabled (runtime toggle)
        jit_enabled = cfg.get("jit_retrieval_enabled", True)
        if not jit_enabled:
            logger.debug("[%s] G07 JIT retrieval disabled by config", ctx.request_id)
            return ctx
        
        # Check for per-request JIT override
        jit_override = ctx.params.get("x_jit_retrieval")
        if jit_override is not None:
            jit_enabled = str(jit_override).lower() in ("true", "1", "yes")
            if not jit_enabled:
                logger.debug("[%s] G07 JIT retrieval disabled by request param", ctx.request_id)
                return ctx

        # Explicit param takes priority; JIT mode auto-extracts from last user message
        rag_query = ctx.params.get("rag_query")
        # jit_require_rag_intent (default false = current behaviour): when true, do
        # NOT auto-extract a query from an arbitrary chat turn — only run retrieval
        # when the caller actually signalled RAG intent (rag_query or the
        # x_rag_collection header). This spares every non-RAG request the embed +
        # Qdrant hybrid search + cross-encoder rerank it currently pays for.
        require_intent = cfg.get("jit_require_rag_intent", False)
        has_rag_intent = bool(rag_query) or bool(ctx.params.get("x_rag_collection"))
        if not rag_query and jit_enabled and (not require_intent or has_rag_intent):
            rag_query = _extract_rag_query(ctx.messages)
        if not rag_query:
            return ctx

        tokens_before = ctx.current_token_count
        top_k: int = int(ctx.params.get("x_rag_top_k", cfg.get("top_k", 3)))
        top_k_after_rerank: int = cfg.get("top_k_after_rerank", 1)
        sim_threshold: float = cfg.get("similarity_threshold", 0.85)

        # Collection resolution (C2): client-supplied x_rag_collection is honoured
        # only for admin keys; non-admin callers are pinned to their own tenant
        # collection. See _resolve_collection().
        collection = _resolve_collection(ctx, cfg)

        # Check for pgvector fallback preference
        use_pgvector = cfg.get("use_pgvector_fallback", False)

        fallback_used = False
        try:
            if use_pgvector and _PGVECTOR_URL:
                chunks = await _pgvector_search(
                    rag_query, top_k, _PGVECTOR_URL, collection, cfg
                )
            else:
                chunks = await _hybrid_search(
                    rag_query, top_k, top_k_after_rerank,
                    _QDRANT_URL, collection, cfg,
                )
                # No (or low-confidence) primary results — escalate through
                # G3's strict→relaxed→dense→sparse fallback chain instead of
                # returning empty context.
                if not chunks:
                    chunks = await self._rag_fallback.search_with_fallback(
                        rag_query,
                        collection=collection,
                        top_k=top_k,
                        similarity_threshold=sim_threshold,
                    )
                    fallback_used = bool(chunks)

            # Freshness (Task 10): soft-filter chunks older than max_age_days before
            # anything else consumes them (unknown-age chunks are kept). Off unless
            # `max_age_days` is configured.
            max_age_days = cfg.get("max_age_days")
            if max_age_days:
                _before = len(chunks)
                chunks = _filter_by_freshness(chunks, max_age_days)
                if len(chunks) < _before:
                    logger.debug("[%s] G07 freshness: dropped %d stale chunk(s) (> %s days)",
                                 ctx.request_id, _before - len(chunks), max_age_days)

            # Apply chunk guard
            chunk_guard = self._get_chunk_guard(cfg)
            chunks = chunk_guard.validate_chunks(chunks)

            # Rerank if Qdrant mode
            if not use_pgvector:
                reranker_model = cfg.get("reranker_model", "cross-encoder/ms-marco-MiniLM-L-6-v2")
                ranked = await _rerank(rag_query, chunks, top_k_after_rerank, sim_threshold,
                                       reranker_model, fallback_floor=sim_threshold)
            else:
                ranked = chunks[:top_k_after_rerank]

            # Quality metrics (Task 11): record the retrieval outcome — hit/miss,
            # chunks injected, and freshness (oldest injected chunk age). Emitted on
            # every RAG retrieval attempt (a miss is a signal too). Never breaks the path.
            _max_age = _max_chunk_age_seconds(ranked)
            try:
                from middleware.quality_metrics import record_retrieval
                record_retrieval(getattr(ctx, "tenant_id", "default"), len(ranked), _max_age)
            except Exception:
                pass

            if ranked:
                context_text = "\n\n".join(c["text"] for c in ranked)
                ctx.messages = _inject_context(ctx.messages, context_text)
                tokens_after = count_messages_tokens(ctx.messages, ctx.model)
                ctx.savings.add_step(
                    GROUP,
                    f"RAG hybrid RRF: top-{top_k} → reranked top-{top_k_after_rerank} (JIT={jit_enabled})",
                    tokens_before,
                    tokens_after,
                )
                langfuse_tracing.add_span(
                    ctx,
                    name="G07-retrieval",
                    span_input={"rag_query": rag_query, "tokens_before": tokens_before},
                    output={"chunks_retrieved": len(ranked), "tokens_after": tokens_after},
                    metadata={
                        "top_k": top_k,
                        "top_k_after_rerank": top_k_after_rerank,
                        "similarity_threshold": sim_threshold,
                        "chunk_scores": [round(c.get("score", 0.0), 3) for c in ranked],
                        # Freshness (Task 10): oldest injected chunk age in seconds
                        # (None = no chunk carried a timestamp). Also emitted as a metric (Task 11).
                        "context_max_age_seconds": _max_age,
                        "jit_enabled": jit_enabled,
                        "pgvector_fallback": use_pgvector,
                        "rag_fallback_chain_used": fallback_used,
                        "chunk_guard_applied": True,
                    },
                )
                logger.debug(
                    "[%s] G07 RAG: %d → %d tokens (JIT=%s, pgvector=%s)",
                    ctx.request_id,
                    tokens_before,
                    tokens_after,
                    jit_enabled,
                    use_pgvector,
                )
        except Exception as exc:
            logger.warning("G07 retrieval failed: %s", exc)

        return ctx


def _extract_rag_query(messages: List[Dict]) -> Optional[str]:
    """Extract the last user message as the RAG query if rag_enabled is signalled."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content[:500]  # cap query length
    return None


def _inject_context(messages: List[Dict], context: str) -> List[Dict]:
    """Prepend retrieved context as a system message before the last user turn."""
    injected = [{"role": "system", "content": f"[Retrieved context]\n{context}"}]
    return injected + messages


async def _embed_dense(text: str) -> List[float]:
    from ml_models import get_text_embedding

    # Synchronous, CPU-bound (cold load 1–2s) — offload so it doesn't block the
    # event loop and serialise every concurrent request behind it.
    def _embed() -> List[float]:
        model = get_text_embedding(_DENSE_MODEL)
        return list(model.embed([text]))[0].tolist()

    return await asyncio.to_thread(_embed)


async def _embed_sparse(text: str) -> Optional[object]:
    """Return a Qdrant SparseVector for the query text.
    
    Phase 2 fix: Returns None if fastembed unavailable (allows dense-only fallback).
    """
    try:
        from qdrant_client.models import SparseVector
        from ml_models import get_sparse_text_embedding

        def _embed():
            model = get_sparse_text_embedding(_SPARSE_MODEL)
            sparse = list(model.embed([text]))[0]
            return SparseVector(
                indices=sparse.indices.tolist(),
                values=sparse.values.tolist(),
            )

        # Synchronous, CPU-bound — offload to keep the event loop responsive.
        return await asyncio.to_thread(_embed)
    except ImportError as e:
        logger.debug("G07 fastembed unavailable for sparse embedding: %s", e)
        return None
    except Exception as e:
        logger.warning("G07 sparse embedding failed: %s", e)
        return None


async def _hybrid_search(
    query: str, top_k: int, top_k_final: int,
    qdrant_url: str, collection: str, cfg: Dict,
) -> List[Dict]:
    """Hybrid dense + sparse (SPLADE/BM25) via Qdrant prefetch + RRF fusion."""
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import (
            Prefetch, Fusion, FusionQuery,
        )
        from ml_models import qdrant_client_kwargs

        # api_key = the Qdrant app-layer key (`api-key` header); the Cloud Run IAM
        # Bearer token is attached automatically by qdrant_client_kwargs on GCP.
        client = AsyncQdrantClient(**qdrant_client_kwargs(url=qdrant_url, api_key=_QDRANT_API_KEY or None))
        dense_embedding = await _embed_dense(query)
        sparse_embedding = await _embed_sparse(query)  # Phase 2: may be None if fastembed unavailable

        # Phase 2 fix: Build prefetch list dynamically based on sparse availability.
        # qdrant-client query_points API (verified on 1.12.x): pass the raw query value in each
        # Prefetch and select the named vector via `using=` — do NOT wrap in NamedVector /
        # NamedSparseVector (those are rejected by the Prefetch.query union and were the source of
        # the float_type/model_type validation errors). The fusion query itself, however, DOES need
        # the FusionQuery wrapper (see below) — the bare Fusion enum is not a valid top-level query.
        prefetches = [
            Prefetch(
                query=dense_embedding,
                using="dense",
                limit=top_k,
            ),
        ]
        if sparse_embedding is not None:
            prefetches.append(
                Prefetch(
                    query=sparse_embedding,
                    using="sparse",
                    limit=top_k,
                ),
            )

        # Cosine floor for the DENSE-ONLY paths (matches pgvector + the G3 relaxed
        # fallback chain). NOT applied to the RRF fusion query — reciprocal-rank
        # fusion scores (~0.016) are not on the cosine scale, so a cosine threshold
        # there would drop every result. `_score_kind` records which scale each
        # returned chunk carries, so the rerank fail-closed path floors only cosine.
        sim_threshold = float(cfg.get("similarity_threshold", 0.85))
        score_kind = "rrf"

        try:
            if sparse_embedding is not None:
                # Hybrid search with RRF fusion. The top-level fusion query MUST be wrapped in
                # FusionQuery(fusion=Fusion.RRF) — passing the bare `Fusion.RRF` enum serialises
                # to an invalid request body on qdrant-client 1.12.x / server 1.12.x (the server
                # 400s with "Expected some form of vector, id, or a type of query"), which silently
                # dropped every hybrid search to the dense-only except-fallback below.
                results = await client.query_points(
                    collection_name=collection,
                    prefetch=prefetches,
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=top_k_final,
                    with_payload=True,
                )
            else:
                # Phase 2: Dense-only search when fastembed unavailable. The
                # collection stores named vectors, so `using="dense"` is required
                # to disambiguate which vector to query against.
                logger.debug("G07 using dense-only search (fastembed unavailable)")
                score_kind = "cosine"
                results = await client.query_points(
                    collection_name=collection,
                    query=dense_embedding,
                    using="dense",
                    limit=top_k_final,
                    score_threshold=sim_threshold,
                    with_payload=True,
                )
        except Exception as inner_exc:
            # Fallback: named vectors may not exist (dense-only collection) → plain search
            logger.warning(
                "G07 named-vector hybrid failed (%s) — falling back to dense-only search",
                inner_exc,
            )
            score_kind = "cosine"
            results = await client.query_points(
                collection_name=collection,
                query=dense_embedding,
                using="dense",
                limit=top_k_final,
                score_threshold=sim_threshold,
                with_payload=True,
            )

        await client.close()
        # `source` is carried through (seeded + ingested points both stamp it) so callers
        # that need provenance — e.g. the docs-chat citations via retrieve() — can use it.
        # The request-path caller ignores it, so this is a no-op for existing behaviour.
        return [
            {"text": r.payload.get("text", ""), "score": r.score,
             "source": (r.payload or {}).get("source", ""), "_score_kind": score_kind,
             # Freshness (Task 10): carried through so G07 can compute chunk age +
             # apply the optional max-age filter. Absent on old points → treated as
             # unknown age (never dropped).
             "ingested_at": (r.payload or {}).get("ingested_at"),
             "source_date": (r.payload or {}).get("source_date")}
            for r in results.points
        ]
    except Exception as exc:
        logger.warning("G07 Qdrant search failed: %s", exc)
        return []


def _chunk_age_seconds(chunk: Dict, now: Optional[datetime] = None) -> Optional[float]:
    """Age of a retrieved chunk in seconds, from its `source_date` (the document's own
    date, preferred) or else `ingested_at`. Returns None when the chunk carries no
    parseable timestamp — such a chunk has unknown freshness and is never dropped."""
    now = now or datetime.now(timezone.utc)
    raw = chunk.get("source_date") or chunk.get("ingested_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:                      # treat a naive stamp as UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())


def _filter_by_freshness(chunks: List[Dict], max_age_days: Optional[float],
                         now: Optional[datetime] = None) -> List[Dict]:
    """Soft freshness filter: drop chunks older than `max_age_days`. A chunk with no
    timestamp (unknown age) is KEPT — freshness filtering must never silently gut RAG
    for a corpus ingested before freshness stamping existed. `max_age_days` None/<=0 →
    no filtering (returns the input unchanged)."""
    if not max_age_days or max_age_days <= 0:
        return chunks
    now = now or datetime.now(timezone.utc)
    cutoff = float(max_age_days) * 86400.0
    kept = []
    for c in chunks:
        age = _chunk_age_seconds(c, now)
        if age is None or age <= cutoff:
            kept.append(c)
    return kept


def _max_chunk_age_seconds(chunks: List[Dict], now: Optional[datetime] = None) -> Optional[float]:
    """Oldest chunk age in seconds across those that carry a timestamp (for the
    freshness metric). None when no chunk has a timestamp."""
    now = now or datetime.now(timezone.utc)
    ages = [a for a in (_chunk_age_seconds(c, now) for c in chunks) if a is not None]
    return max(ages) if ages else None


async def _rerank(
    query: str,
    chunks: List[Dict],
    top_k: int,
    threshold: float,
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    fallback_floor: Optional[float] = None,
) -> List[Dict]:
    """Cross-encoder reranker — keeps only chunks above similarity threshold.

    On reranker error the reranker FAILS CLOSED rather than open: instead of
    injecting the unfiltered candidate set, it re-applies the retrieval cosine floor
    (`fallback_floor`) to any chunk that carries a cosine-scale score (`_score_kind ==
    "cosine"`). RRF-fused chunks (`_score_kind == "rrf"`) keep their fusion ranking —
    a cosine floor is meaningless on reciprocal-rank scores — so RAG still works when
    the reranker has a transient failure, without silently injecting low-relevance
    context. `fallback_floor=None` preserves the legacy pass-through for callers that
    don't supply a floor."""
    if not chunks:
        return []
    try:
        from ml_models import get_cross_encoder

        # Cross-encoder inference is synchronous and CPU-bound (cold load 1–2s) —
        # offload so a rerank never freezes the event loop for other requests.
        def _predict():
            model = get_cross_encoder(model_name)
            pairs = [(query, c["text"]) for c in chunks]
            return model.predict(pairs)

        scores = await asyncio.to_thread(_predict)
        ranked = sorted(
            zip(chunks, scores), key=lambda x: x[1], reverse=True
        )
        return [c for c, s in ranked[:top_k] if s >= threshold]
    except Exception as exc:
        if fallback_floor is None:
            logger.warning("G07 reranker failed: %s — using original order", exc)
            return chunks[:top_k]
        kept = [
            c for c in chunks
            if c.get("_score_kind") != "cosine"
            or float(c.get("score", 0.0) or 0.0) >= fallback_floor
        ]
        logger.warning(
            "G07 reranker failed: %s — failing closed via cosine floor %.2f (%d/%d chunks kept)",
            exc, fallback_floor, len(kept[:top_k]), len(chunks),
        )
        return kept[:top_k]


async def retrieve(
    query: str,
    collection: str,
    *,
    top_k: int = 5,
    top_k_final: int = 3,
    similarity_threshold: float = 0.3,
    max_chunk_tokens: int = 1000,
    max_total_tokens: int = 4000,
    use_pgvector: Optional[bool] = None,
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
) -> List[Dict]:
    """Context-free retrieval helper — the reusable core of G07, decoupled from
    ``RequestContext`` so non-pipeline callers (e.g. the commercial docs-chat endpoint)
    can retrieve against an arbitrary collection.

    Runs the same hybrid dense+sparse Qdrant search + ChunkGuard + cross-encoder rerank as
    ``G07Retrieval.process_request``, and returns ``[{text, score, source}]`` (rerank score
    preserved, ``source`` carried for citations). ``use_pgvector`` defaults to auto:
    pgvector when Qdrant is unset but ``DATABASE_URL`` is present, else Qdrant.

    ``similarity_threshold`` here gates the CROSS-ENCODER rerank score (typically wider-range
    than the cosine gate on the request path), so it defaults lower; tune per caller.
    """
    if not query or not collection:
        return []
    if not _is_valid_collection_name(collection):
        logger.warning("retrieve(): rejected invalid collection name %r", collection)
        return []

    cfg = {"similarity_threshold": similarity_threshold}
    if use_pgvector is None:
        use_pgvector = not _QDRANT_URL and bool(_PGVECTOR_URL)

    try:
        if use_pgvector and _PGVECTOR_URL:
            chunks = await _pgvector_search(query, top_k, _PGVECTOR_URL, collection, cfg)
            guarded = ChunkGuard(max_chunk_tokens, max_total_tokens).validate_chunks(chunks)
            return guarded[:top_k_final]

        chunks = await _hybrid_search(query, top_k, top_k_final, _QDRANT_URL, collection, cfg)
        guarded = ChunkGuard(max_chunk_tokens, max_total_tokens).validate_chunks(chunks)
        return await _rerank(query, guarded, top_k_final, similarity_threshold, reranker_model,
                             fallback_floor=similarity_threshold)
    except Exception as exc:
        logger.warning("retrieve() failed for collection %s: %s", collection, exc)
        return []
