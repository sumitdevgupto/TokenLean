"""
G07 pgvector Fallback — PostgreSQL/pgvector as alternative RAG store

Provides pgvector as a fallback RAG retrieval backend when Qdrant
is unavailable or for hybrid retrieval strategies.

Uses hybrid dense + sparse retrieval via pgvector cosine similarity
and PostgreSQL tsvector full-text search.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)

_PGVECTOR_URL = os.getenv("DATABASE_URL", "")


class PGVectorRAG:
    """PostgreSQL/pgvector fallback RAG retrieval."""
    
    def __init__(self, dsn: Optional[str] = None, redis_prefix: str = ""):
        self.dsn = dsn or _PGVECTOR_URL
        self._pool: Optional[asyncpg.Pool] = None
        # Scopes the embedding cache, like every other Redis key in the proxy. Empty is
        # the default tenant, matching the convention in tenancy/context.py.
        self._prefix = redis_prefix
    
    async def _get_pool(self) -> Optional[asyncpg.Pool]:
        """Lazy-init connection pool."""
        if self._pool is None and self.dsn:
            try:
                self._pool = await asyncpg.create_pool(
                    self.dsn,
                    min_size=2,
                    max_size=10,
                )
                logger.info("PGVector pool initialized")
            except Exception as exc:
                logger.error("PGVector pool init failed: %s", exc)
                return None
        return self._pool
    
    async def ensure_table(self, table_name: str = "rag_chunks"):
        """Ensure the RAG chunks table exists with proper indexes."""
        pool = await self._get_pool()
        if not pool:
            return False
        
        async with pool.acquire() as conn:
            # Create table with vector support
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    id SERIAL PRIMARY KEY,
                    chunk_id TEXT UNIQUE NOT NULL,
                    text TEXT NOT NULL,
                    embedding vector(384),  -- all-MiniLM-L6-v2 dimension
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT NOW(),
                    search_vector tsvector
                )
            """)
            
            # Delta-sync key: lets add_chunk skip re-embedding content that has not
            # changed. Self-healed here because the table may predate this column.
            await conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS content_hash TEXT")

            # Create indexes
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table_name}_embedding 
                ON {table_name} USING ivfflat (embedding vector_cosine_ops)
            """)
            
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table_name}_search 
                ON {table_name} USING GIN (search_vector)
            """)
            
            logger.info("PGVector table %s ensured", table_name)
            return True
    
    async def hybrid_search(
        self,
        query: str,
        top_k: int = 5,
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
        table_name: str = "rag_chunks",
    ) -> List[Dict]:
        """
        Hybrid search combining pgvector cosine similarity with 
        PostgreSQL full-text search (tsvector).
        """
        pool = await self._get_pool()
        if not pool:
            return []
        
        try:
            # Generate embedding for the query. Route through the shared ml_models loader so it
            # (a) reuses the cached singleton and (b) honours HF_HUB_OFFLINE (baked-model, no
            # HF-CDN metadata call that would hang under egress-restricted VPC).
            from ml_models import get_text_embedding
            model = get_text_embedding("sentence-transformers/all-MiniLM-L6-v2")
            query_embedding = list(model.embed([query]))[0].tolist()
            
            async with pool.acquire() as conn:
                # Hybrid query: combine vector similarity with text search
                # Using RRF-inspired ranking
                rows = await conn.fetch(
                    f"""
                    WITH vector_results AS (
                        SELECT 
                            chunk_id,
                            text,
                            metadata,
                            1 - (embedding <=> $1::vector) as vector_score,
                            ts_rank_cd(search_vector, plainto_tsquery($2), 32) as text_score
                        FROM {table_name}
                        WHERE 1 - (embedding <=> $1::vector) > 0.5
                           OR search_vector @@ plainto_tsquery($2)
                        ORDER BY (embedding <=> $1::vector)
                        LIMIT $3 * 2
                    )
                    SELECT 
                        chunk_id,
                        text,
                        metadata,
                        vector_score,
                        text_score,
                        ({dense_weight} * vector_score + {sparse_weight} * COALESCE(text_score, 0)) as hybrid_score
                    FROM vector_results
                    ORDER BY hybrid_score DESC
                    LIMIT $3
                    """,
                    query_embedding,
                    query,
                    top_k,
                )
                
                results = []
                for row in rows:
                    results.append({
                        "chunk_id": row["chunk_id"],
                        "text": row["text"],
                        "metadata": row["metadata"],
                        "score": float(row["hybrid_score"]),
                        "vector_score": float(row["vector_score"]),
                        "text_score": float(row["text_score"] or 0),
                    })
                
                logger.debug("PGVector hybrid search returned %d results", len(results))
                return results
                
        except Exception as exc:
            logger.error("PGVector search failed: %s", exc)
            return []
    
    async def add_chunk(
        self,
        chunk_id: str,
        text: str,
        metadata: Optional[Dict] = None,
        table_name: str = "rag_chunks",
    ) -> bool:
        """Add a chunk to pgvector."""
        pool = await self._get_pool()
        if not pool:
            return False
        
        try:
            from embedding_cache import content_hash, embed_cached

            digest = content_hash(text)

            async with pool.acquire() as conn:
                # Skip unchanged content entirely. Re-ingesting a corpus used to re-encode
                # every chunk unconditionally, so an unchanged document cost a full pass on
                # each run - and a second app indexing the same document paid it again.
                existing = await conn.fetchval(
                    f"SELECT content_hash FROM {table_name} WHERE chunk_id = $1", chunk_id)
                if existing and existing == digest:
                    logger.debug("PGVector add_chunk: %s unchanged, skipping embed", chunk_id)
                    return True

                # Cached, content-addressed embedding: identical text encodes once per
                # tenant, however many chunks or apps reference it.
                embedding = await embed_cached(
                    text, "all-MiniLM-L6-v2", prefix=self._prefix)

                await conn.execute(
                    f"""
                    INSERT INTO {table_name} (chunk_id, text, embedding, metadata,
                                              search_vector, content_hash)
                    VALUES ($1, $2, $3::vector, $4, to_tsvector($2), $5)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        text = EXCLUDED.text,
                        embedding = EXCLUDED.embedding,
                        metadata = EXCLUDED.metadata,
                        search_vector = EXCLUDED.search_vector,
                        content_hash = EXCLUDED.content_hash
                    """,
                    chunk_id,
                    text,
                    embedding,
                    json.dumps(metadata or {}),
                    digest,
                )
                return True
                
        except Exception as exc:
            logger.error("PGVector add_chunk failed: %s", exc)
            return False
    
    async def close(self):
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None


import json
