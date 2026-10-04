"""G05's semantic tier (L2) against a REAL Postgres with pgvector.

The expiry filter and the purge are SQL, so only a real server shows that a row past its
expires_at (or without one) is not served, and that the store's purge deletes only this
tenant's expired rows, one batch at a time.

TEST_PG_DSN must point at a THROWAWAY pgvector server, as a superuser: the tests create a
database and drop it afterwards. Skipped otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, urlunsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway pgvector Postgres (superuser)")

import middleware.g05_cache as g05  # noqa: E402
from cache.pg_pool import tenant_conn  # noqa: E402

DB = "g05_l2_test"
VEC = [1.0] + [0.0] * 383            # every row and every query embed here: similarity 1
VEC_STR = "[" + ",".join(str(x) for x in VEC) + "]"
LIVE, EXPIRED, NO_EXPIRY = timedelta(hours=1), timedelta(seconds=-1), None


def _run(coro):
    return asyncio.run(coro)


async def _as(dsn, fn):
    conn = await asyncpg.connect(dsn)
    try:
        return await fn(conn)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def dsn():
    base = os.environ["TEST_PG_DSN"]
    p = urlsplit(base)
    url = urlunsplit((p.scheme, p.netloc, f"/{DB}", p.query, ""))

    async def drop(conn):
        await conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")

    async def create(conn):
        await drop(conn)
        await conn.execute(f"CREATE DATABASE {DB}")

    _run(_as(base, create))
    _run(_as(url, lambda c: c.execute("CREATE EXTENSION IF NOT EXISTS vector")))
    yield url
    _run(_as(base, drop))


@pytest.fixture
def fresh(dsn, monkeypatch):
    """An empty cache_l2, created by G05's own schema step; no purge interval running. The
    background index build is held back: the tests that want the index build it themselves."""
    monkeypatch.setattr(g05, "_cache_l2_schema_ready", False)
    monkeypatch.setattr(g05, "_l2_next_purge", {})
    monkeypatch.setattr(g05, "_spawn_l2_index_build", lambda pool: None)
    monkeypatch.setattr(g05, "_iterative_scan", True)

    async def setup():
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            await g05._ensure_cache_l2_schema(pool)
            async with pool.acquire() as conn:
                await conn.execute("TRUNCATE cache_l2")
        finally:
            await pool.close()

    _run(setup())
    return dsn


async def _with_pool(url, fn):
    """Run fn(pool) with G05 pointed at the test database and the embedding stubbed."""
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    try:
        with patch.dict(os.environ, {"DATABASE_URL": url}), \
                patch("cache.pg_pool.get_pg_pool", new=AsyncMock(return_value=pool)), \
                patch.object(g05, "_embed", new=AsyncMock(return_value=VEC)):
            return await fn(pool)
    finally:
        await pool.close()


async def _insert(pool, offset, *, tenant="acme", answer="x", n=1):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cache_l2 (query_hash, embedding, response_json, expires_at, tenant_id,"
            " model_scope) SELECT md5(random()::text || g::text), $1::vector, $2,"
            " NOW() + $3::interval, $4, '' FROM generate_series(1, $5) g",
            VEC_STR, json.dumps({"answer": answer}), offset, tenant, n)


async def _count(pool, tenant, expired_only=False):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM cache_l2 WHERE tenant_id = $1"
            " AND (NOT $2 OR expires_at <= NOW())", tenant, expired_only)


def _ctx(make_ctx, tenant="acme"):
    ctx = make_ctx([{"role": "user", "content": "What changed in the last release?"}])
    ctx.tenant_id = tenant
    return ctx


def test_an_expired_row_is_not_served(fresh, make_ctx):
    async def scenario(pool):
        await _insert(pool, EXPIRED, answer="stale")
        before = await g05._l2_lookup(_ctx(make_ctx), 0.9)
        await _insert(pool, LIVE, answer="fresh")
        after = await g05._l2_lookup(_ctx(make_ctx), 0.9)
        return before, after

    before, after = _run(_with_pool(fresh, scenario))
    assert before == (None, 0.0)
    assert after[0] == {"answer": "fresh"}


def test_a_row_without_an_expiry_is_not_served(fresh, make_ctx):
    async def scenario(pool):
        await _insert(pool, NO_EXPIRY, answer="age unknown")
        return await g05._l2_lookup(_ctx(make_ctx), 0.9)

    assert _run(_with_pool(fresh, scenario)) == (None, 0.0)


def test_the_purge_deletes_only_this_tenants_expired_rows_a_batch_at_a_time(fresh):
    batch = g05._L2_PURGE_BATCH

    async def scenario(pool):
        await _insert(pool, EXPIRED, n=batch + 200)
        await _insert(pool, NO_EXPIRY, n=3)
        await _insert(pool, LIVE, n=5)
        await _insert(pool, EXPIRED, tenant="globex", n=7)
        left = []
        for _ in range(2):             # a full batch lets the very next store purge again
            async with tenant_conn(pool, "acme") as conn:
                await g05._purge_expired_l2(conn, "acme")
            left.append(await _count(pool, "acme"))
        await _insert(pool, EXPIRED, n=1)
        async with tenant_conn(pool, "acme") as conn:   # last batch was partial: wait
            await g05._purge_expired_l2(conn, "acme")
        return left, await _count(pool, "acme"), await _count(pool, "globex")

    left, after_wait, globex = _run(_with_pool(fresh, scenario))
    assert left == [203 + 5, 5]
    assert after_wait == 6
    assert globex == 7


def test_the_store_writes_its_row_and_purges_the_tenants_expired_ones(fresh, make_ctx):
    async def scenario(pool):
        await _insert(pool, EXPIRED, answer="stale")
        await _insert(pool, EXPIRED, tenant="globex")
        ctx = _ctx(make_ctx)
        await g05._l2_store(ctx, {"answer": "stored"}, 3600)
        served = await g05._l2_lookup(_ctx(make_ctx), 0.9)
        expired_left = await _count(pool, "acme", expired_only=True)
        return served, expired_left, await _count(pool, "globex")

    served, expired_left, globex = _run(_with_pool(fresh, scenario))
    assert served[0] == {"answer": "stored"}
    assert expired_left == 0
    assert globex == 1


# ── The vector index (every L2 lookup used to scan all of the tenant's rows) ──
def _near(seed, spread):
    """A unit vector at about `spread` from VEC, deterministic per seed."""
    import random
    rnd = random.Random(seed)
    v = [x + rnd.uniform(-spread, spread) for x in VEC]
    norm = sum(x * x for x in v) ** 0.5
    return "[" + ",".join(f"{x / norm:.6f}" for x in v) + "]"


async def _insert_vectors(pool, tenant, vectors, answer):
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO cache_l2 (query_hash, embedding, response_json, expires_at, tenant_id,"
            " model_scope) VALUES (md5(random()::text), $1::vector, $2, NOW() + interval '1 hour',"
            " $3, '')", [(v, json.dumps({"answer": answer}), tenant) for v in vectors])


async def _index_valid(pool):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname = 'idx_cache_l2_embedding'")


def test_the_vector_index_is_built_and_can_serve_the_lookup(fresh):
    async def scenario(pool):
        await _insert_vectors(pool, "acme", [_near(i, 0.01) for i in range(50)], "x")
        built = await g05._build_l2_vector_index(pool)
        async with pool.acquire() as conn:
            await conn.execute("SET enable_seqscan = off")   # only an ordered path will do:
            await conn.execute("SET enable_sort = off")      # the lookup's shape must fit it
            plan = "\n".join(r[0] for r in await conn.fetch(
                "EXPLAIN " + g05._L2_LOOKUP_SQL, VEC_STR, 0.9, "acme", ""))
        return built, await _index_valid(pool), plan

    built, valid, plan = _run(_with_pool(fresh, scenario))
    assert built is True and valid is True
    assert "idx_cache_l2_embedding" in plan, plan


def test_an_index_an_earlier_build_left_invalid_is_rebuilt(fresh):
    async def scenario(pool):
        await _insert_vectors(pool, "acme", [_near(1, 0.01)], "x")
        await g05._build_l2_vector_index(pool)
        async with pool.acquire() as conn:            # what a died CONCURRENTLY build leaves
            await conn.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid ="
                               " 'idx_cache_l2_embedding'::regclass")
        before = await _index_valid(pool)
        rebuilt = await g05._build_l2_vector_index(pool)
        return before, rebuilt, await _index_valid(pool)

    assert _run(_with_pool(fresh, scenario)) == (False, True, True)


def test_a_small_tenant_among_many_close_rows_still_gets_its_answer(fresh, make_ctx):
    """With the index forced, the nearest candidates are all another tenant's; the WHERE drops
    them. pgvector's iterative scan keeps going until this tenant's row turns up."""
    async def version(pool):
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")

    async def scenario(pool):
        if tuple(int(x) for x in (await version(pool)).split(".")[:2]) < (0, 8):
            pytest.skip("pgvector before 0.8 has no iterative index scans")
        await _insert_vectors(pool, "globex", [_near(i, 0.002) for i in range(3000)], "theirs")
        await _insert_vectors(pool, "acme", [_near(99999, 0.02)], "ours")
        await g05._build_l2_vector_index(pool)
        forced = await asyncpg.create_pool(fresh, min_size=1, max_size=1, server_settings={
            "enable_seqscan": "off", "enable_sort": "off", "enable_bitmapscan": "off"})
        try:
            with patch("cache.pg_pool.get_pg_pool", new=AsyncMock(return_value=forced)):
                served = await g05._l2_lookup(_ctx(make_ctx, "acme"), 0.9)
                g05._iterative_scan = False                  # the control: no iterative scan
                async with forced.acquire() as conn:
                    await conn.execute("RESET hnsw.iterative_scan")
                missed = await g05._l2_lookup(_ctx(make_ctx, "acme"), 0.9)
        finally:
            await forced.close()
        return served, missed

    served, missed = _run(_with_pool(fresh, scenario))
    assert served[0] == {"answer": "ours"}
    assert missed == (None, 0.0)                 # without it, the tenant's row is never reached


def test_a_row_without_an_embedding_is_skipped(fresh, make_ctx):
    async def scenario(pool):
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cache_l2 (query_hash, embedding, response_json, expires_at, tenant_id,"
                " model_scope) VALUES ('no-vector', NULL, '{}', NOW() + interval '1 hour', 'acme', '')")
        return await g05._l2_lookup(_ctx(make_ctx), 0.9)

    assert _run(_with_pool(fresh, scenario)) == (None, 0.0)

