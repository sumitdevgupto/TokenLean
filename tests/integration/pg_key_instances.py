"""Several proxy "instances" in one process, for Postgres key-store tests.

Each instance is an independent copy of api_key_manager with its own event loop thread
and asyncpg pool, so a test can reproduce what separate Cloud Run instances do. Real
Postgres only: callers skip unless TEST_PG_DSN names a THROWAWAY database, whose key
tables reset_tables() drops.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import concurrent.futures
import importlib.util
import threading
import time
from pathlib import Path

import asyncpg

from auth import pg_key_store  # noqa: E402

DSN = os.environ.get("TEST_PG_DSN")
AKM_PATH = Path(__file__).resolve().parents[2] / "src" / "proxy" / "auth" / "api_key_manager.py"


async def reset_tables():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS proxy_keys, revoked_proxy_keys")
        await conn.execute(pg_key_store.PROXY_KEYS_DDL)
    finally:
        await conn.close()


class KeyStoreInstance:
    """One proxy instance: its own api_key_manager state, event loop and pool, with the
    Postgres backend installed. With ``refresh_interval`` it also runs a CacheRefresher."""

    def __init__(self, name, refresh_interval=None):
        self.name = name
        spec = importlib.util.spec_from_file_location(f"akm_instance_{name}", AKM_PATH)
        self.km = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.km)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self._background = []
        self._closers = []

        async def _pool():  # built ON this instance's loop, which the pool binds to
            return await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        self.pool = self.run(_pool())
        self.install()
        self.refresher = None
        if refresh_interval is not None:
            self.refresher = pg_key_store.CacheRefresher(
                self.pool, refresh_interval, key_manager=self.km)
            self.start(self.refresher.run())

    def install(self, on_change=None):
        load_fn, persist_fn = pg_key_store.make_backend(self.pool, self.loop)
        self.km.install_key_store_backend(
            load_fn, persist_fn, name=self.name,
            transact_fn=pg_key_store.make_transact(self.pool, self.loop), on_change=on_change)

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)

    def start(self, coro):
        """Run ``coro`` in the background on this instance's loop until close(); returns
        its future."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        self._background.append(fut)
        return fut

    def on_close(self, coro_fn):
        """Await ``coro_fn()`` on this instance's loop during close(), after the background
        tasks stop (e.g. to close a client that lives on this loop)."""
        self._closers.append(coro_fn)

    def call(self, fn, *args):
        """Call ``fn`` on this instance's loop thread; safe from any thread."""
        self.loop.call_soon_threadsafe(fn, *args)

    def validate(self, raw_key):
        """validate_proxy_key as the proxy calls it: on the event loop, where the Postgres
        backend never blocks on a database read and answers from the cache alone."""
        async def call():
            return self.km.validate_proxy_key(raw_key)
        return self.run(call())

    def rows(self):
        return self.run(pg_key_store.load_all(self.pool))

    def close(self):
        for fut in self._background:
            fut.cancel()
        concurrent.futures.wait(self._background, timeout=5)
        for coro_fn in self._closers:
            self.run(coro_fn())
        self.run(self.pool.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def eventually(predicate, timeout=2.0):
    """Poll ``predicate`` until it is true or ``timeout`` seconds pass; return its value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
