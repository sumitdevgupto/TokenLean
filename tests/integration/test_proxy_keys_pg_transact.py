"""Postgres key-store writes: one serialised transaction per change, row-level, and
rolled back when they time out.

Each lifecycle write used to load the whole store in one round-trip and rewrite the whole
table in another, with only a process-local lock between them. A second instance holding
a stale snapshot therefore silently undid a suspension, allowlist change or new key made
on the first. Every write also re-inserted all N rows, and a write that timed out still
committed afterwards.

Two "instances" are simulated in one process (tests/integration/pg_key_instances.py).
Real Postgres only (advisory locks, xmin): set TEST_PG_DSN to a THROWAWAY database — the
tests drop these tables. Skipped otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import concurrent.futures
import threading
import time

import pytest

pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

from auth import pg_key_store  # noqa: E402
from tests.integration.pg_key_instances import KeyStoreInstance, reset_tables  # noqa: E402


@pytest.fixture
def instances():
    asyncio.run(reset_tables())
    a, b = KeyStoreInstance("a"), KeyStoreInstance("b")
    yield a, b
    a.close()
    b.close()


def _tenant_rows(rows, tenant):
    return {h: m for h, m in rows.items() if m["tenant_id"] == tenant}


def test_a_suspension_survives_another_instance_writing_after_a_stale_read(instances):
    a, b = instances
    a.km.create_key("X")
    # Force the interleaving: instance B reads the store, A suspends X and commits, then B
    # writes. B's read is held open only on the old load-then-write path.
    b_has_read, a_committed = threading.Event(), threading.Event()
    real_load = b.km._BACKEND_LOAD

    def load_then_wait():
        store = real_load()
        b_has_read.set()
        a_committed.wait(5)
        return store

    b.km._BACKEND_LOAD = load_then_wait
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        created = pool.submit(b.km.create_key, "Y")
        b_has_read.wait(1)
        a.km.set_suspended("X", True)
        a_committed.set()
        created.result(30)
    rows = a.rows()
    assert all(m.get("suspended") for m in _tenant_rows(rows, "X").values())
    assert _tenant_rows(rows, "Y")


def test_two_instances_changing_the_same_tenant_keep_both_changes(instances):
    a, b = instances
    a.km.create_key("X")
    # B reads X's row and pauses before writing it; A then suspends X. Only the store lock
    # makes A wait for B — without it B's write, built from the row it read, drops A's
    # suspension (a row-level diff alone does not help when both change the same row).
    b_has_read, a_done = threading.Event(), threading.Event()
    real_transact = b.km._BACKEND_TRANSACT

    def transact_pausing_after_the_read(mutation):
        def paused(store):
            b_has_read.set()
            a_done.wait(1)
            return mutation(store)
        return real_transact(paused)

    b.km._BACKEND_TRANSACT = transact_pausing_after_the_read
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        allowlisted = pool.submit(b.km.set_ip_allowlist, "X", ["10.0.0.0/8"])
        assert b_has_read.wait(5)
        a.km.set_suspended("X", True)
        a_done.set()
        allowlisted.result(30)
    (meta,) = _tenant_rows(a.rows(), "X").values()
    assert meta.get("suspended") is True and meta.get("ip_allowlist") == ["10.0.0.0/8"]


def test_concurrent_signups_on_two_instances_lose_no_key(instances):
    a, b = instances
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futures = [pool.submit((a if i % 2 else b).km.create_key, f"T{i}") for i in range(16)]
        created = {f.result(60)[1] for f in futures}
    assert set(a.rows()) == created


def test_a_lifecycle_write_rewrites_only_the_rows_it_changes(instances):
    a, _ = instances
    seed = {f"h{i:03d}": {"tenant_id": f"T{i % 50}", "tier": "free"} for i in range(500)}
    a.run(pg_key_store.replace_all(a.pool, seed))

    async def xmins():
        async with a.pool.acquire() as conn:
            return {r["key_hash"]: r["v"] for r in
                    await conn.fetch("SELECT key_hash, xmin::text AS v FROM proxy_keys")}

    before = a.run(xmins())
    a.km.set_suspended("T7", True)
    after = a.run(xmins())
    rewritten = {h for h in before if after.get(h) != before[h]}
    assert rewritten == set(_tenant_rows(seed, "T7"))


def test_a_write_that_times_out_is_rolled_back(instances):
    a, _ = instances
    a.km.create_key("X")
    if not hasattr(pg_key_store, "make_transact"):
        pytest.fail("no transactional write path: a timed-out write still commits")
    transact_fn = pg_key_store.make_transact(a.pool, a.loop, timeout_seconds=0.3)

    def slow_suspend(store):
        time.sleep(1.0)  # holds the instance's event loop past the 0.3 s timeout
        for meta in store.values():
            meta["suspended"] = True
        return True, None

    with pytest.raises(TimeoutError):
        transact_fn(slow_suspend)
    time.sleep(1.5)  # let the cancelled transaction unwind
    assert not any(m.get("suspended") for m in a.rows().values())


def test_a_write_from_the_event_loop_thread_is_refused(instances):
    a, _ = instances
    transact_fn = pg_key_store.make_transact(a.pool, a.loop)

    async def write_on_the_loop():  # waiting here would block the loop the write needs
        return transact_fn(lambda store: (False, None))

    with pytest.raises(RuntimeError, match="worker thread"):
        a.run(write_on_the_loop())


def test_rotating_a_suspended_tenant_keeps_it_suspended_and_records_the_old_key(instances):
    a, _ = instances
    _, old_hash, _ = a.km.create_key("X")
    a.km.set_suspended("X", True)
    _, new_hash, meta, revoked = a.km.rotate_tenant_keys("X")
    rows = a.rows()
    assert set(_tenant_rows(rows, "X")) == {new_hash} and revoked == 1
    assert rows[new_hash].get("suspended") is True and meta.get("suspended") is True

    async def recorded():
        async with a.pool.acquire() as conn:
            return {r["key_hash"] for r in await conn.fetch("SELECT key_hash FROM revoked_proxy_keys")}

    assert old_hash in a.run(recorded())


def test_a_revoked_key_is_never_written_back(instances):
    a, _ = instances
    _, key_hash, _ = a.km.create_key("X", raw_key="tok-reissued")
    a.km.delete_tenant_keys("X")
    a.km.create_key("X", raw_key="tok-reissued")
    assert key_hash not in a.rows() and key_hash not in a.km._KEY_CACHE


def test_this_instance_sees_its_own_write_immediately(instances):
    a, _ = instances
    _, key_hash, _ = a.km.create_key("X")
    a.km.set_suspended("X", True)
    assert a.km._KEY_CACHE[key_hash].get("suspended") is True
