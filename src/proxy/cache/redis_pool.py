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
    """Initialise the shared connection pool (idempotent).

    Timeouts/health-check are NOT optional hardening: without socket_timeout a read on a
    dead connection blocks indefinitely. On Cloud Run with Direct VPC egress, idle pooled
    TCP connections to Redis are dropped SILENTLY (no RST) — the next command on a stale
    connection then hangs for the kernel's retransmission give-up (~240s observed:
    per-request pipeline stages stalling 240–490s → Cloud Run 504s). health_check_interval
    PINGs a pooled connection before reuse once it has been idle past the interval, so a
    silently-dropped connection is detected and re-established in milliseconds instead.
    All knobs env-overridable; defaults are safe for local docker too.
    """
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            url or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
            socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "5")),
            retry_on_timeout=True,
            health_check_interval=int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30")),
            socket_keepalive=True,
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
