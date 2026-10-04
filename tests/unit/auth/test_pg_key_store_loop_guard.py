"""The Postgres key-store backend's loop-thread guard.

``validate_proxy_key`` calls the installed load on the event loop, where a blocking database
read would stall every request: there ``load`` answers None ("keep the cache") without
touching the database, and the writes refuse. From a worker thread (where every write path
runs) they submit the work to the loop and wait for it, cancelled if it runs past the timeout.
"""
import asyncio

import pytest

from auth import api_key_manager as akm
from auth import pg_key_store


class _UntouchablePool:
    """Fails the test if anything reaches the database on the loop thread."""

    def __getattr__(self, name):
        raise AssertionError(f"the database was touched on the loop thread ({name})")


STORE = {"h1": {"tenant_id": "acme", "tier": "pro"}, "h2": {"tenant_id": "acme"}}


@pytest.fixture(autouse=True)
def _restore_backend():
    yield
    akm.reset_key_store_backend()


def _on_the_loop(call, *args):
    """Run a backend call on the loop thread. Without the guard it submits work to the loop it
    is blocking and waits for it: a deadlock, cut short by the backend's timeout."""
    try:
        return call(*args)
    except TimeoutError:
        pytest.fail("the call blocked the event loop waiting for the database")


async def test_load_on_the_loop_keeps_the_cache_without_touching_the_database():
    load_fn, _ = pg_key_store.make_backend(_UntouchablePool(), asyncio.get_running_loop(),
                                           timeout_seconds=0.1)
    assert _on_the_loop(load_fn) is None


async def test_writes_on_the_loop_are_refused():
    loop = asyncio.get_running_loop()
    _, persist_fn = pg_key_store.make_backend(_UntouchablePool(), loop, timeout_seconds=0.1)
    transact_fn = pg_key_store.make_transact(_UntouchablePool(), loop, timeout_seconds=0.1)
    with pytest.raises(RuntimeError, match="worker thread"):
        _on_the_loop(persist_fn, dict(STORE))
    with pytest.raises(RuntimeError, match="worker thread"):
        _on_the_loop(transact_fn, lambda store: (False, None))


async def test_from_a_worker_thread_load_and_persist_run_on_the_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    ran_on = []

    async def load_all(pool):
        ran_on.append(asyncio.get_running_loop())
        return dict(STORE)

    async def replace_all(pool, store):
        ran_on.append(asyncio.get_running_loop())
        assert store == STORE

    monkeypatch.setattr(pg_key_store, "load_all", load_all)
    monkeypatch.setattr(pg_key_store, "replace_all", replace_all)
    load_fn, persist_fn = pg_key_store.make_backend(object(), loop)
    assert await asyncio.to_thread(load_fn) == STORE
    await asyncio.to_thread(persist_fn, dict(STORE))
    assert ran_on == [loop, loop]


async def test_a_write_past_its_timeout_is_cancelled(monkeypatch):
    # Never left to commit after the caller has already reported the write as failed.
    cancelled = asyncio.Event()

    async def replace_all(pool, store):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(pg_key_store, "replace_all", replace_all)
    _, persist_fn = pg_key_store.make_backend(object(), asyncio.get_running_loop(),
                                              timeout_seconds=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(persist_fn, dict(STORE))
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
    except TimeoutError:
        pytest.fail("the write was left running after its timeout")


async def test_list_tenants_refuses_on_the_loop_and_answers_from_a_worker(monkeypatch):
    # The admin overview used to call it on the loop: the guard's None became a 500. On the
    # loop it must refuse (never a silently empty list); from a worker it reads the store.
    async def load_all(pool):
        return dict(STORE)

    monkeypatch.setattr(pg_key_store, "load_all", load_all)
    load_fn, persist_fn = pg_key_store.make_backend(object(), asyncio.get_running_loop(),
                                                    timeout_seconds=0.1)
    akm.install_key_store_backend(load_fn, persist_fn, name="postgres-test")
    with pytest.raises(RuntimeError, match="no data for a write path"):
        _on_the_loop(akm.list_tenants)
    tenants = await asyncio.to_thread(akm.list_tenants)
    assert [t["tenant_id"] for t in tenants] == ["acme"]
    assert tenants[0]["key_count"] == 2
