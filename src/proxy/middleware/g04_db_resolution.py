"""
G04 Rules-Based Bypass — Database-First Resolution

Provides database-first resolution layer before LLM calls:
1. Exact match lookup for previously answered queries
2. Fuzzy matching for similar queries
3. Confidence scoring on bypass decisions
4. Audit logging for new bypass candidates

Uses PostgreSQL with pg_trgm for fuzzy text matching.
"""
import asyncio
import hashlib
import json
import logging
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import asyncpg

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G04"


class DBResolutionCache:
    """PostgreSQL-backed query resolution cache."""
    
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
    
    async def _get_pool(self) -> asyncpg.Pool:
        """Lazy-init connection pool."""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=2,
                max_size=10,
            )
        return self._pool
    
    async def ensure_table(self):
        """Ensure the resolution cache table exists."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS query_resolution_cache (
                    id SERIAL PRIMARY KEY,
                    query_hash TEXT UNIQUE NOT NULL,
                    query_text TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    metadata JSONB,
                    hit_count INTEGER DEFAULT 1,
                    last_used TIMESTAMP DEFAULT NOW(),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Create index for fuzzy matching
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_query_resolution_trgm 
                ON query_resolution_cache USING gin(query_text gin_trgm_ops)
            """)
            
            # Create index for hit count analytics
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_query_resolution_hits 
                ON query_resolution_cache(hit_count DESC)
            """)
    
    async def get_exact_match(self, query: str) -> Optional[Dict]:
        """Check for exact query match."""
        pool = await self._get_pool()
        query_hash = self._hash_query(query)
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT response_text, metadata, hit_count 
                FROM query_resolution_cache 
                WHERE query_hash = $1
                """,
                query_hash,
            )
            
            if row:
                # Update hit count
                await conn.execute(
                    """
                    UPDATE query_resolution_cache 
                    SET hit_count = hit_count + 1, last_used = NOW()
                    WHERE query_hash = $1
                    """,
                    query_hash,
                )
                
                return {
                    "response": row["response_text"],
                    "metadata": row["metadata"],
                    "hit_count": row["hit_count"] + 1,
                    "match_type": "exact",
                    "confidence": 1.0,
                }
            
            return None
    
    async def get_fuzzy_match(
        self, 
        query: str, 
        similarity_threshold: float = 0.85,
        top_k: int = 3,
    ) -> Optional[Dict]:
        """Check for fuzzy query match using pg_trgm."""
        pool = await self._get_pool()
        
        async with pool.acquire() as conn:
            # Use pg_trgm similarity
            rows = await conn.fetch(
                """
                SELECT 
                    query_text,
                    response_text,
                    metadata,
                    hit_count,
                    similarity(query_text, $1) as sim
                FROM query_resolution_cache
                WHERE query_text % $1  -- similarity operator
                ORDER BY sim DESC
                LIMIT $2
                """,
                query,
                top_k,
            )
            
            if rows and rows[0]["sim"] >= similarity_threshold:
                best = rows[0]
                
                # Update hit count for matched query
                await conn.execute(
                    """
                    UPDATE query_resolution_cache 
                    SET hit_count = hit_count + 1, last_used = NOW()
                    WHERE query_text = $1
                    """,
                    best["query_text"],
                )
                
                return {
                    "response": best["response_text"],
                    "metadata": best["metadata"],
                    "hit_count": best["hit_count"] + 1,
                    "match_type": "fuzzy",
                    "confidence": best["sim"],
                    "matched_query": best["query_text"],
                }
            
            return None
    
    async def store_response(
        self, 
        query: str, 
        response: str, 
        metadata: Optional[Dict] = None,
    ):
        """Store a new query-response pair."""
        pool = await self._get_pool()
        query_hash = self._hash_query(query)
        
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO query_resolution_cache 
                (query_hash, query_text, response_text, metadata)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (query_hash) DO UPDATE SET
                    response_text = EXCLUDED.response_text,
                    metadata = EXCLUDED.metadata,
                    last_used = NOW()
                """,
                query_hash,
                query,
                response,
                json.dumps(metadata or {}),
            )
    
    def _hash_query(self, query: str) -> str:
        """Generate hash for query text."""
        normalized = query.strip().lower()
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]
    
    async def get_stats(self) -> Dict:
        """Get cache statistics."""
        pool = await self._get_pool()
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_entries,
                    SUM(hit_count) as total_hits,
                    AVG(hit_count) as avg_hits,
                    MAX(hit_count) as max_hits
                FROM query_resolution_cache
            """)
            
            return dict(row) if row else {}
    
    async def close(self):
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None


class G04DBResolution:
    """G04 database-first resolution middleware."""
    
    def __init__(self):
        self._cache: Optional[DBResolutionCache] = None
    
    def _get_cache(self, dsn: str) -> DBResolutionCache:
        if self._cache is None:
            self._cache = DBResolutionCache(dsn)
        return self._cache
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G4_rules_bypass", {})
        if not cfg.get("db_first_resolution", False):
            return ctx
        
        dsn = ctx.config.get("database_url", "")
        if not dsn:
            logger.debug("[%s] G04: No database URL configured", ctx.request_id)
            return ctx
        
        try:
            cache = self._get_cache(dsn)
            await cache.ensure_table()
            
            # Extract query from messages
            query = self._extract_query(ctx.messages)
            if not query:
                return ctx
            
            # Check exact match first (fastest)
            exact_match = await cache.get_exact_match(query)
            if exact_match:
                ctx.cache_hit = True
                ctx.cache_level = "DB_EXACT"
                ctx.cache_response = exact_match["response"]
                ctx.db_bypass_confidence = exact_match["confidence"]
                
                ctx.savings.add_step(
                    GROUP,
                    f"DB exact match (conf={exact_match['confidence']:.2f})",
                    ctx.current_token_count,
                    0,
                )
                logger.info(
                    "[%s] G04 DB exact match: %d hits",
                    ctx.request_id,
                    exact_match["hit_count"],
                )
                return ctx
            
            # Check fuzzy match
            fuzzy_threshold = cfg.get("fuzzy_similarity_threshold", 0.85)
            fuzzy_match = await cache.get_fuzzy_match(query, fuzzy_threshold)
            
            if fuzzy_match:
                ctx.cache_hit = True
                ctx.cache_level = "DB_FUZZY"
                ctx.cache_response = fuzzy_match["response"]
                ctx.db_bypass_confidence = fuzzy_match["confidence"]
                
                ctx.savings.add_step(
                    GROUP,
                    f"DB fuzzy match (conf={fuzzy_match['confidence']:.2f})",
                    ctx.current_token_count,
                    0,
                )
                logger.info(
                    "[%s] G04 DB fuzzy match: sim=%.2f, hits=%d",
                    ctx.request_id,
                    fuzzy_match["confidence"],
                    fuzzy_match["hit_count"],
                )
                return ctx
            
            # No match - mark for potential bypass candidate
            ctx.db_bypass_candidate = True
            ctx.db_bypass_confidence = 0.0
            
        except Exception as exc:
            logger.warning("[%s] G04 DB resolution failed: %s", ctx.request_id, exc)
        
        return ctx
    
    def _extract_query(self, messages: List[Dict]) -> str:
        """Extract query text from messages."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
        return ""


# Audit script for discovering new bypass candidates
async def audit_bypass_candidates(
    dsn: str,
    min_occurrences: int = 3,
    output_path: str = "bypass_candidates.json",
):
    """
    Analyze query logs to find new bypass candidates.
    
    Queries that appear frequently with similar responses are good candidates
    for DB-first resolution.
    """
    pool = await asyncpg.create_pool(dsn)
    
    try:
        async with pool.acquire() as conn:
            # Find frequently asked similar queries
            rows = await conn.fetch("""
                SELECT 
                    query_text,
                    response_text,
                    hit_count,
                    similarity(query_text, LAG(query_text) OVER (ORDER BY hit_count DESC)) as sim
                FROM query_resolution_cache
                WHERE hit_count >= $1
                ORDER BY hit_count DESC
            """, min_occurrences)
            
            candidates = []
            for row in rows:
                if row["sim"] and row["sim"] > 0.7:
                    candidates.append({
                        "query": row["query_text"],
                        "response_preview": row["response_text"][:200],
                        "hit_count": row["hit_count"],
                        "similarity_to_previous": row["sim"],
                    })
            
            with open(output_path, 'w') as f:
                json.dump(candidates, f, indent=2)
            
            print(f"Found {len(candidates)} bypass candidates in {output_path}")
            
    finally:
        await pool.close()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) >= 3 and sys.argv[1] == "audit":
        dsn = sys.argv[2]
        output = sys.argv[3] if len(sys.argv) > 3 else "bypass_candidates.json"
        asyncio.run(audit_bypass_candidates(dsn, output_path=output))
