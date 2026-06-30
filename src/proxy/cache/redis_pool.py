"""Shared aioredis.ConnectionPool to eliminate per-request connection churn.

Usage:
    from cache.redis_pool import get_redis
    redis = get_redis()
    await redis.get("key")
    # Do NOT call redis.aclose() — the client is long-lived.

Shutdown:
    from cache.redis_pool import close_pool
    await close_pool()
"""
import os
from typing import Optional

import redis.asyncio as aioredis

_pool: Optional[aioredis.ConnectionPool] = None


def init_pool(url: Optional[str] = None) -> aioredis.ConnectionPool:
    """Initialise the shared connection pool (idempotent)."""
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            url or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    return _pool


def get_redis() -> aioredis.Redis:
    """Return a Redis client backed by the shared connection pool."""
    if _pool is None:
        init_pool()
    return aioredis.Redis(connection_pool=_pool)


async def close_pool() -> None:
    """Disconnect all pooled connections — call once at application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
