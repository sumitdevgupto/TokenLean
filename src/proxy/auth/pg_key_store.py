"""
Postgres-backed proxy-key store engine (CORE — ships in OSS, UNWIRED by default).

Lifts the JSON-blob backends' limits (Secret Manager 64KiB ≈ 300 keys; process-local
write lock racing across instances) for deployments where self-serve signup mints keys
at volume. The blob backends (local file / Secret Manager) stay the OSS default —
nothing changes unless a host installs this via ``api_key_manager.install_key_store_backend``
(the commercial app does that at startup when ``PROXY_KEYS_BACKEND=postgres``).

Design constraints honoured here:
  * ``validate_proxy_key`` is called synchronously ON the event loop, so the installed
    sync ``load_fn`` must never block there → it returns ``None`` on the loop thread
    (meaning "keep the current cache") and the async background refresher keeps the
    cache warm. Worker threads (all write paths run via ``asyncio.to_thread``) block on
    ``run_coroutine_threadsafe`` for a fresh, consistent read.
  * ``persist_fn(store)`` replaces the FULL store transactionally — writes are rare
    (signup / rotate / suspend), so replace-all keeps exact parity with the blob
    contract including deletions.
"""
import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

PROXY_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS proxy_keys (
    key_hash   TEXT    PRIMARY KEY,
    tenant_id  TEXT    NOT NULL,
    tier       TEXT    NOT NULL DEFAULT 'free',
    admin      BOOLEAN NOT NULL DEFAULT FALSE,
    suspended  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT,
    extra      JSONB   NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proxy_keys_tenant ON proxy_keys (tenant_id);
"""

_CORE_FIELDS = ("tenant_id", "tier", "admin", "suspended", "created_at")


async def ensure_proxy_keys_schema(pg_pool) -> None:
    if pg_pool is None:
        return
    async with pg_pool.acquire() as conn:
        await conn.execute(PROXY_KEYS_DDL)


def _row_to_meta(row) -> dict:
    meta = {
        "tenant_id": row["tenant_id"],
        "tier": row["tier"] or "free",
        "created_at": row["created_at"],
    }
    if row["admin"]:
        meta["admin"] = True
    if row["suspended"]:
        meta["suspended"] = True
    extra = row["extra"]
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            meta.setdefault(k, v)
    return meta


async def load_all(pg_pool) -> dict:
    """Read the complete store: {key_hash: metadata} — same shape as the blob."""
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key_hash, tenant_id, tier, admin, suspended, created_at, extra "
            "FROM proxy_keys")
    return {r["key_hash"]: _row_to_meta(r) for r in rows}


async def replace_all(pg_pool, store: dict) -> None:
    """Transactionally replace the full store (exact blob-persist parity, incl. deletes)."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM proxy_keys")
            for key_hash, entry in store.items():
                if not isinstance(entry, dict):
                    # Legacy string-format rows: keep them round-trippable.
                    entry = {"tenant_id": str(entry), "tier": "legacy"}
                extra = {k: v for k, v in entry.items() if k not in _CORE_FIELDS}
                await conn.execute(
                    "INSERT INTO proxy_keys (key_hash, tenant_id, tier, admin, suspended, created_at, extra) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
                    key_hash,
                    entry.get("tenant_id", "default"),
                    entry.get("tier", "free"),
                    bool(entry.get("admin")),
                    bool(entry.get("suspended")),
                    entry.get("created_at"),
                    json.dumps(extra),
                )


async def import_blob_store_once(pg_pool) -> int:
    """One-time idempotent migration: if the table is empty and the active blob store
    has keys, copy them in (existing hashes keep validating unchanged). Returns count."""
    async with pg_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT COUNT(*) FROM proxy_keys")
    if existing:
        return 0
    from auth import api_key_manager as km
    try:
        blob = km._load_full_store()  # backend not yet installed → reads the blob
    except Exception as exc:
        logger.warning("proxy_keys import: blob read failed: %s", exc)
        return 0
    if not blob:
        return 0
    await replace_all(pg_pool, blob)
    logger.info("proxy_keys: imported %d key(s) from the blob store", len(blob))
    return len(blob)


def make_backend(pg_pool, loop, timeout_seconds: float = 10.0):
    """Build the sync (load_fn, persist_fn) pair for install_key_store_backend().

    ``loop`` is the running event loop captured at startup; worker threads submit
    coroutines to it and block on the result. On the loop thread itself load returns
    ``None`` (keep cache) — the refresher below is what keeps validation fresh there.
    """

    def _on_loop_thread() -> bool:
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def load_fn() -> Optional[dict]:
        if _on_loop_thread():
            return None
        fut = asyncio.run_coroutine_threadsafe(load_all(pg_pool), loop)
        return fut.result(timeout_seconds)

    def persist_fn(store: dict) -> None:
        if _on_loop_thread():
            raise RuntimeError("proxy_keys persist must run on a worker thread")
        fut = asyncio.run_coroutine_threadsafe(replace_all(pg_pool, store), loop)
        fut.result(timeout_seconds)

    return load_fn, persist_fn


async def run_cache_refresher(pg_pool, interval_seconds: int = 30) -> None:
    """Background task: keep api_key_manager's validate cache warm from Postgres.

    This is what makes a key minted on another instance validate here within
    ``interval_seconds`` (the blob backends relied on reload-on-miss instead, which
    the loop-thread guard disables for the PG backend).
    """
    from auth import api_key_manager as km
    while True:
        try:
            km.replace_cache(await load_all(pg_pool))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("proxy_keys cache refresh failed: %s", exc)
        await asyncio.sleep(max(5, interval_seconds))
