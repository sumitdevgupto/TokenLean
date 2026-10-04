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
  * Lifecycle writes (signup / rotate / suspend / allowlist / offboard) go through
    ``transact_fn(mutation)``: ONE transaction that takes an advisory lock, reads the
    current store inside it, applies the change and writes only the rows that differ.
    A process-local lock cannot serialise writers on other instances; this does.
  * ``persist_fn(store)`` still replaces the full store transactionally, for callers
    that hold a whole store (the one-time blob import).
  * A removed key is a revocation, recorded in ``revoked_proxy_keys``. No writer — the
    deploy-time sync, the blob import, a full-store write from a stale snapshot — may
    put a recorded hash back.
  * A write that exceeds its timeout is cancelled, so its transaction rolls back rather
    than committing after the caller has already reported failure.
  * Other instances learn of a change through :class:`CacheRefresher`: a full reload every
    interval, plus an immediate re-read of one tenant when a host's ``on_change`` hook
    reports that tenant changed (the commercial app carries it over Redis pub/sub).
"""
import asyncio
import concurrent.futures
import copy
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
CREATE TABLE IF NOT EXISTS revoked_proxy_keys (
    key_hash   TEXT        PRIMARY KEY,
    tenant_id  TEXT,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CORE_FIELDS = ("tenant_id", "tier", "admin", "suspended", "created_at")

# Insert a key the table does not have and that was never revoked. ON CONFLICT DO NOTHING
# leaves an existing row (and its console-set suspension / allowlist / contract flags)
# exactly as it is. Mirrored in infra/migrations/sync_proxy_keys.py.
INSERT_NEW_KEY_SQL = (
    "INSERT INTO proxy_keys (key_hash, tenant_id, tier, admin, suspended, created_at, extra) "
    "SELECT $1::text, $2::text, $3::text, $4::boolean, $5::boolean, $6::text, $7::jsonb "
    "WHERE NOT EXISTS (SELECT 1 FROM revoked_proxy_keys WHERE key_hash = $1::text) "
    "ON CONFLICT (key_hash) DO NOTHING"
)


UPSERT_KEY_SQL = (
    "INSERT INTO proxy_keys (key_hash, tenant_id, tier, admin, suspended, created_at, extra) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) "
    "ON CONFLICT (key_hash) DO UPDATE SET tenant_id=EXCLUDED.tenant_id, tier=EXCLUDED.tier, "
    "admin=EXCLUDED.admin, suspended=EXCLUDED.suspended, created_at=EXCLUDED.created_at, "
    "extra=EXCLUDED.extra"
)

# Transaction-scoped advisory lock held by every lifecycle write. Any fixed bigint works;
# only proxy-key writers take it.
KEY_STORE_LOCK_ID = 0x544C4B455953  # "TLKEYS"

_SELECT_ROWS = ("SELECT key_hash, tenant_id, tier, admin, suspended, created_at, extra "
                "FROM proxy_keys")

# Pause before retrying a cache reload that failed (the database did not answer).
_RETRY_SECONDS = 5


def _row_args(key_hash: str, entry) -> tuple:
    if not isinstance(entry, dict):
        # Legacy string-format rows: keep them round-trippable.
        entry = {"tenant_id": str(entry), "tier": "legacy"}
    extra = {k: v for k, v in entry.items() if k not in _CORE_FIELDS}
    return (key_hash, entry.get("tenant_id", "default"), entry.get("tier", "free"),
            bool(entry.get("admin")), bool(entry.get("suspended")), entry.get("created_at"),
            json.dumps(extra))


async def ensure_proxy_keys_schema(pg_pool) -> None:
    if pg_pool is None:
        return
    from cache.pg_pool import may_run_ddl
    if not await may_run_ddl(pg_pool, "proxy_keys"):
        return  # a restricted runtime role: the schema job keeps the tables current
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
        rows = await conn.fetch(_SELECT_ROWS)
    return {r["key_hash"]: _row_to_meta(r) for r in rows}


async def load_tenants(pg_pool, tenant_ids) -> dict:
    """Read just these tenants' current keys: {key_hash: metadata}."""
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_ROWS + " WHERE tenant_id = ANY($1::text[])",
                                list(tenant_ids))
    return {r["key_hash"]: _row_to_meta(r) for r in rows}


async def replace_all(pg_pool, store: dict) -> int:
    """Transactionally replace the full store (exact blob-persist parity, incl. deletes).

    Every hash this write removes is a revocation — revoke, rotate and offboard all end
    here — so it is recorded in ``revoked_proxy_keys`` in the same transaction. A recorded
    hash is never written back, whatever the caller's (possibly stale) snapshot holds.
    Returns the number of keys written.
    """
    hashes = list(store)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO revoked_proxy_keys (key_hash, tenant_id) "
                "SELECT key_hash, tenant_id FROM proxy_keys "
                "WHERE NOT (key_hash = ANY($1::text[])) "
                "ON CONFLICT (key_hash) DO NOTHING",
                hashes)
            revoked = {r["key_hash"] for r in await conn.fetch(
                "SELECT key_hash FROM revoked_proxy_keys WHERE key_hash = ANY($1::text[])",
                hashes)}
            await conn.execute("DELETE FROM proxy_keys")
            written = 0
            for key_hash, entry in store.items():
                if key_hash in revoked:
                    continue
                await conn.execute(
                    "INSERT INTO proxy_keys (key_hash, tenant_id, tier, admin, suspended, created_at, extra) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
                    *_row_args(key_hash, entry))
                written += 1
    if revoked:
        logger.warning("proxy_keys: refused to write back %d revoked key(s)", len(revoked))
    return written


async def transact(pg_pool, mutation):
    """Apply ``mutation(store) -> (changed, result)`` to the CURRENT store, atomically.

    The advisory lock serialises every lifecycle writer across instances, and the store
    is read inside it, so no write starts from a stale snapshot. Only the difference is
    written: added or changed rows are upserted; removed rows are deleted and recorded as
    revocations; a recorded hash is never written back. ``mutation`` runs on the event
    loop, so it must only edit the dict. Returns ``(result, store_after)``.
    """
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", KEY_STORE_LOCK_ID)
            rows = await conn.fetch(_SELECT_ROWS)
            before = {r["key_hash"]: _row_to_meta(r) for r in rows}
            store = copy.deepcopy(before)
            changed, result = mutation(store)
            if changed:
                for key_hash in await _write_diff(conn, before, store):
                    del store[key_hash]  # refused as revoked: not in the table, not cached
    return result, store


async def _write_diff(conn, before: dict, after: dict) -> set:
    """Write ``after`` over ``before`` row by row. Returns the hashes it refused to write
    because they are recorded as revoked."""
    removed = [h for h in before if h not in after]
    if removed:
        await conn.execute(
            "INSERT INTO revoked_proxy_keys (key_hash, tenant_id) "
            "SELECT key_hash, tenant_id FROM proxy_keys WHERE key_hash = ANY($1::text[]) "
            "ON CONFLICT (key_hash) DO NOTHING",
            removed)
        await conn.execute("DELETE FROM proxy_keys WHERE key_hash = ANY($1::text[])", removed)
    upserts = [h for h, meta in after.items() if before.get(h) != meta]
    if not upserts:
        return set()
    revoked = {r["key_hash"] for r in await conn.fetch(
        "SELECT key_hash FROM revoked_proxy_keys WHERE key_hash = ANY($1::text[])",
        upserts)}
    rows = [_row_args(h, after[h]) for h in upserts if h not in revoked]
    if rows:
        await conn.executemany(UPSERT_KEY_SQL, rows)
    if revoked:
        logger.warning("proxy_keys: refused to write back %d revoked key(s)", len(revoked))
    return revoked


async def upsert_keys(pg_pool, store: dict) -> int:
    """Insert the keys of {key_hash: metadata} that proxy_keys does not have yet.

    Insert-only: an existing key keeps its current row, so a sync can never reset a
    suspension, allowlist or contract flag set from the console, and a revoked hash is
    never re-inserted. The deploy-time key-sync job (infra/migrations/sync_proxy_keys.py)
    runs the same statement. Returns the number of keys inserted.
    """
    inserted = 0
    async with pg_pool.acquire() as conn:
        for key_hash, entry in store.items():
            status = await conn.execute(INSERT_NEW_KEY_SQL, *_row_args(key_hash, entry))
            inserted += int(status.split()[-1])  # "INSERT 0 1" | "INSERT 0 0"
    return inserted


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
    # replace_all skips revoked hashes: revocations can empty the table, and the old blob
    # still holds every key it ever had.
    written = await replace_all(pg_pool, blob)
    logger.info("proxy_keys: imported %d key(s) from the blob store", written)
    return written


def _running_on(loop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def _run_on_loop(coro, loop, timeout_seconds: float):
    """Run ``coro`` on ``loop`` from a worker thread and wait for it. On timeout the
    coroutine is cancelled, so an open transaction rolls back — never commits later,
    after the caller has already reported the write as failed."""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout_seconds)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise


def make_backend(pg_pool, loop, timeout_seconds: float = 10.0):
    """Build the sync (load_fn, persist_fn) pair for install_key_store_backend().

    ``loop`` is the running event loop captured at startup; worker threads submit
    coroutines to it and block on the result. On the loop thread itself load returns
    ``None`` (keep cache) — the refresher below is what keeps validation fresh there.
    """

    def load_fn() -> Optional[dict]:
        if _running_on(loop):
            return None
        return _run_on_loop(load_all(pg_pool), loop, timeout_seconds)

    def persist_fn(store: dict) -> None:
        if _running_on(loop):
            raise RuntimeError("proxy_keys persist must run on a worker thread")
        _run_on_loop(replace_all(pg_pool, store), loop, timeout_seconds)

    return load_fn, persist_fn


def make_transact(pg_pool, loop, timeout_seconds: float = 10.0):
    """Build the sync ``transact_fn(mutation)`` for install_key_store_backend(): runs
    :func:`transact` on ``loop`` from a worker thread, rolled back on timeout."""

    def transact_fn(mutation):
        if _running_on(loop):
            raise RuntimeError("proxy_keys writes must run on a worker thread")
        return _run_on_loop(transact(pg_pool, mutation), loop, timeout_seconds)

    return transact_fn


class CacheRefresher:
    """Keeps api_key_manager's validate cache in step with proxy_keys.

    :meth:`run` reloads every key each ``interval_seconds``. That is what makes a key
    minted on another instance validate here (the blob backends relied on reload-on-miss
    instead, which the loop-thread guard disables for the PG backend). :meth:`request`
    re-reads one tenant now, when an instance has just changed that tenant's keys, so a
    suspension or revocation does not wait out the interval. One task applies every
    update in turn: a full reload that read the table before a change is always followed
    by that change's tenant re-read, never overtaken by it.
    """

    def __init__(self, pg_pool, interval_seconds: int = 30, key_manager=None):
        if key_manager is None:
            from auth import api_key_manager as key_manager
        self._pool = pg_pool
        self._km = key_manager
        self._interval = max(5, interval_seconds)
        self._full = True  # the first pass loads every key
        self._tenants: set = set()
        self._wake = asyncio.Event()

    def request(self, tenant_id: Optional[str] = None) -> None:
        """Re-read ``tenant_id``'s keys now, or every key when ``None``. Call it on the
        loop that runs :meth:`run`."""
        if tenant_id is None:
            self._full = True
        else:
            self._tenants.add(tenant_id)
        self._wake.set()

    async def run(self) -> None:
        while True:
            if not (self._full or self._tenants):
                try:
                    await asyncio.wait_for(self._wake.wait(), self._interval)
                except asyncio.TimeoutError:
                    self._full = True
            self._wake.clear()
            full, tenants = self._full, self._tenants
            self._full, self._tenants = False, set()
            try:
                if full:
                    self._km.replace_cache(await load_all(self._pool))
                else:
                    self._km.replace_tenants_in_cache(
                        tenants, await load_tenants(self._pool, tenants))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("proxy_keys cache refresh failed: %s", exc)
                self._full = True  # nothing was applied: reload everything on the retry
                await asyncio.sleep(_RETRY_SECONDS)


async def run_cache_refresher(pg_pool, interval_seconds: int = 30) -> None:
    """Background task: reload every key each ``interval_seconds``. Use
    :class:`CacheRefresher` directly to also re-read a tenant the moment it changes."""
    await CacheRefresher(pg_pool, interval_seconds).run()
