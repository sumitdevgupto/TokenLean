"""Revoked proxy keys stay revoked, and the deploy-time key sync never changes a key.

Every commercial deploy runs infra/migrations/sync_proxy_keys.py against the proxy_keys
table. Its old ON CONFLICT ... DO UPDATE reset console suspensions and IP allowlists for
every tenant in local-keys.json, and — because a revoke deletes the row and left no record
— re-inserted revoked hashes. Revocations are now recorded in revoked_proxy_keys, and no
writer (sync, blob import, a stale full-store write) may put a recorded hash back.

Needs a real Postgres for ON CONFLICT / NOT EXISTS semantics: set TEST_PG_DSN to a
THROWAWAY database (the tests drop and recreate these tables). Skipped otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")
DSN = os.environ.get("TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_PG_DSN to a throwaway Postgres")

from auth import pg_key_store  # noqa: E402

SYNC_JOB = Path(__file__).resolve().parents[2] / "infra" / "migrations" / "sync_proxy_keys.py"


def _sync_job():
    spec = importlib.util.spec_from_file_location("sync_proxy_keys", SYNC_JOB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest_asyncio.fixture
async def pool():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS proxy_keys, revoked_proxy_keys")
    await pg_key_store.ensure_proxy_keys_schema(pool)
    yield pool
    await pool.close()


async def _deploy_sync(pool, local_keys):
    """What every commercial deploy does: the sync job's DDL, then its write."""
    job = _sync_job()
    async with pool.acquire() as conn:
        await conn.execute(job.PROXY_KEYS_DDL)
        return await job._upsert(conn, local_keys)


async def _revoked(pool):
    async with pool.acquire() as conn:
        return {r["key_hash"] for r in await conn.fetch("SELECT key_hash FROM revoked_proxy_keys")}


async def test_a_deploy_sync_keeps_a_console_suspension_and_allowlist(pool):
    await pg_key_store.replace_all(pool, {"h1": {"tenant_id": "ACME", "tier": "enterprise"}})
    # Console: suspend the tenant and restrict its source IPs.
    await pg_key_store.replace_all(pool, {"h1": {"tenant_id": "ACME", "tier": "enterprise",
                                                 "suspended": True, "ip_allowlist": ["10.0.0.0/8"]}})
    await _deploy_sync(pool, {"h1": {"tenant_id": "ACME", "tier": "enterprise"}})
    meta = (await pg_key_store.load_all(pool))["h1"]
    assert meta.get("suspended") is True
    assert meta.get("ip_allowlist") == ["10.0.0.0/8"]


async def test_a_deploy_sync_never_brings_back_a_revoked_key(pool):
    await pg_key_store.replace_all(pool, {"admin-h": {"tenant_id": "OPS", "admin": True},
                                          "h2": {"tenant_id": "ACME"}})
    await pg_key_store.replace_all(pool, {"h2": {"tenant_id": "ACME"}})  # revoke the admin key
    await _deploy_sync(pool, {"admin-h": {"tenant_id": "OPS", "admin": True},
                              "h2": {"tenant_id": "ACME"}})
    assert set(await pg_key_store.load_all(pool)) == {"h2"}


async def test_a_rotated_tenants_old_key_stays_gone_after_a_sync(pool):
    await pg_key_store.replace_all(pool, {"old": {"tenant_id": "ACME"}})
    await pg_key_store.replace_all(pool, {"new": {"tenant_id": "ACME"}})  # rotation
    await _deploy_sync(pool, {"old": {"tenant_id": "ACME"}})
    assert set(await pg_key_store.load_all(pool)) == {"new"}


async def test_a_deploy_sync_still_adds_a_genuinely_new_key(pool):
    await pg_key_store.replace_all(pool, {"h1": {"tenant_id": "ACME"}})
    await _deploy_sync(pool, {"h1": {"tenant_id": "ACME"}, "h9": {"tenant_id": "HARNESS"}})
    assert set(await pg_key_store.load_all(pool)) == {"h1", "h9"}


async def test_replace_all_records_exactly_the_hashes_it_removes(pool):
    await pg_key_store.replace_all(pool, {"a": {"tenant_id": "T"}, "b": {"tenant_id": "T"},
                                          "c": {"tenant_id": "T"}})
    await pg_key_store.replace_all(pool, {"b": {"tenant_id": "T"}, "c": {"tenant_id": "T"},
                                          "d": {"tenant_id": "T"}})
    assert await _revoked(pool) == {"a"}


async def test_a_stale_full_store_write_cannot_resurrect_a_revoked_key(pool):
    # Instance B loaded {x, y} before instance A revoked x, then writes its stale view.
    await pg_key_store.replace_all(pool, {"x": {"tenant_id": "X"}, "y": {"tenant_id": "Y"}})
    await pg_key_store.replace_all(pool, {"y": {"tenant_id": "Y"}})
    await pg_key_store.replace_all(pool, {"x": {"tenant_id": "X"}, "y": {"tenant_id": "Y"},
                                          "z": {"tenant_id": "Z"}})
    assert set(await pg_key_store.load_all(pool)) == {"y", "z"}


async def test_the_blob_import_skips_revoked_hashes(pool, monkeypatch):
    # Revocations can empty the table; the next startup must not re-import revoked keys.
    await pg_key_store.replace_all(pool, {"a": {"tenant_id": "A"}})
    await pg_key_store.replace_all(pool, {})
    from auth import api_key_manager as km
    monkeypatch.setattr(km, "_load_full_store",
                        lambda: {"a": {"tenant_id": "A"}, "b": {"tenant_id": "B"}})
    await pg_key_store.import_blob_store_once(pool)
    assert set(await pg_key_store.load_all(pool)) == {"b"}


async def test_upsert_keys_behaves_like_the_sync_job(pool):
    await pg_key_store.replace_all(pool, {"h1": {"tenant_id": "ACME"}, "gone": {"tenant_id": "ACME"}})
    await pg_key_store.replace_all(pool, {"h1": {"tenant_id": "ACME", "suspended": True}})
    await pg_key_store.upsert_keys(pool, {"h1": {"tenant_id": "ACME"}, "gone": {"tenant_id": "ACME"},
                                          "h3": {"tenant_id": "NEW"}})
    store = await pg_key_store.load_all(pool)
    assert set(store) == {"h1", "h3"}
    assert store["h1"].get("suspended") is True
