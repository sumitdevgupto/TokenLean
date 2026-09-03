"""Tenant-scoped, content-addressed cache for text embeddings.

Artefacts inside one tenant already share a vector *collection*, but sharing stopped at
storage: every ingest re-embedded identical content from scratch, and no embedding cache
existed anywhere in the stack (``ml_models`` caches model objects, never vectors). So two
apps indexing the same document, or asking the same question, each paid the full encode.

This closes that gap the same way the CCR store does — the key IS the hash of the input, so:

* identical text embeds **once** per tenant and every later caller reuses the vector;
* concurrent writers of the same text write identical bytes to the same key, making writes
  idempotent by construction rather than something to coordinate;
* a tenant prefix keeps one tenant's cache out of another's, exactly as every other Redis
  key in the proxy is scoped.

Embeddings are deterministic for a given (model, text), so a cache hit cannot change a
retrieval result — only skip recomputing it. That is what makes this safe to apply to the
search path as well as to ingest.

Vectors are stored struct-packed (float32) rather than as JSON: a 384-dim vector is ~1.5 KB
packed against ~8 KB as JSON text, and float32 is what the model produced, so the round trip
is exact rather than merely close.
"""
import asyncio
import base64
import hashlib
import logging
import struct
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Bounded in-process cache in front of Redis, mirroring g26_context_budget's summary cache:
# expiry-aware get, purge-expired then evict-soonest on put. A cache, never the record.
_LOCAL_MAX = 512
_local: Dict[str, Tuple[float, List[float]]] = {}

DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _cache_key(prefix: str, model_name: str, text: str) -> str:
    """Tenant-scoped, content-addressed key: {tenant}emb:{model}:{sha256(text)}."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{prefix}emb:{model_name}:{digest}"


def _pack(vector: List[float]) -> str:
    return base64.b64encode(struct.pack(f"{len(vector)}f", *vector)).decode("ascii")


def _unpack(blob: str) -> List[float]:
    raw = base64.b64decode(blob)
    return list(struct.unpack(f"{len(raw) // 4}f", raw))


def _local_get(key: str) -> Optional[List[float]]:
    entry = _local.get(key)
    if not entry:
        return None
    expires_at, vector = entry
    if expires_at <= time.time():
        _local.pop(key, None)
        return None
    return vector


def _local_put(key: str, vector: List[float], ttl: int) -> None:
    now = time.time()
    for k in [k for k, (exp, _) in _local.items() if exp <= now]:
        _local.pop(k, None)
    while len(_local) >= _LOCAL_MAX:
        oldest = min(_local, key=lambda k: _local[k][0])
        _local.pop(oldest, None)
    _local[key] = (now + max(1, ttl), vector)


async def embed_cached(
    text: str,
    model_name: str,
    *,
    prefix: str = "",
    ttl: int = DEFAULT_TTL_SECONDS,
) -> List[float]:
    """Embed ``text``, reusing a cached vector when this tenant has seen it before.

    Falls back to computing on every miss, and to computing unconditionally if the cache
    layer is unreachable — a cache outage must slow things down, never change a result.
    ``model.encode`` is synchronous and CPU-bound (a cold load is 1-2s), so it runs in a
    worker thread to avoid stalling every other in-flight request behind one embedding.
    """
    key = _cache_key(prefix, model_name, text)

    cached = _local_get(key)
    if cached is not None:
        return cached

    try:
        from cache.redis_pool import get_redis
        blob = await get_redis().get(key)
        if blob:
            vector = _unpack(blob if isinstance(blob, str) else blob.decode("ascii"))
            _local_put(key, vector, ttl)
            return vector
    except Exception as exc:
        logger.debug("embedding cache read unavailable: %s", exc)

    def _encode() -> List[float]:
        from ml_models import get_sentence_transformer
        return get_sentence_transformer(model_name).encode(text).tolist()

    vector = await asyncio.to_thread(_encode)
    _local_put(key, vector, ttl)

    try:
        from cache.redis_pool import get_redis
        await get_redis().set(key, _pack(vector), ex=max(1, ttl))
    except Exception as exc:
        logger.debug("embedding cache write unavailable: %s", exc)

    return vector


def content_hash(text: str) -> str:
    """Stable hash of ingest content, used to skip re-embedding unchanged documents."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
