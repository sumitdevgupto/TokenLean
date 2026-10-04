"""CacheRefresher (no database): the validate cache stays in step with proxy_keys.

A full reload runs every interval; request(tenant) re-reads one tenant at once. One task
applies both in turn, so a full reload that read the table before a change can never land
after that change's tenant re-read. The two reads are replaced by an in-memory table.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import asyncio
import copy

import pytest

from auth import api_key_manager as akm
from auth import pg_key_store


class _Table:
    """In-memory stand-in for proxy_keys. A full read can be held open after it has read."""

    def __init__(self, rows):
        self.rows = rows
        self.full_reads = 0
        self.tenant_reads = []
        self.full_read_started = asyncio.Event()
        self.hold_full_read = None
        self.fail_next_tenant_read = False

    async def load_all(self, pool):
        self.full_reads += 1
        snapshot = copy.deepcopy(self.rows)  # the table is read here
        self.full_read_started.set()
        if self.hold_full_read is not None:
            await self.hold_full_read.wait()
        return snapshot

    async def load_tenants(self, pool, tenant_ids):
        self.tenant_reads.append(set(tenant_ids))
        if self.fail_next_tenant_read:
            self.fail_next_tenant_read = False
            raise ConnectionError("database unavailable")
        return {h: copy.deepcopy(m) for h, m in self.rows.items() if m["tenant_id"] in tenant_ids}


@pytest.fixture
def table(monkeypatch):
    t = _Table({"x1": {"tenant_id": "X", "tier": "free"}, "y1": {"tenant_id": "Y", "tier": "free"}})
    monkeypatch.setattr(pg_key_store, "load_all", t.load_all)
    monkeypatch.setattr(pg_key_store, "load_tenants", t.load_tenants)
    monkeypatch.setattr(akm, "_KEY_CACHE", {})
    monkeypatch.setattr(akm, "_CACHE_LOADED_AT", 0.0)
    return t


@pytest.fixture
async def refresher(table):
    r = pg_key_store.CacheRefresher(None, interval_seconds=3600, key_manager=akm)
    task = asyncio.create_task(r.run())
    await _until(lambda: table.full_reads == 1 and akm._KEY_CACHE)  # the first pass loads all
    yield r
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def test_a_requested_tenant_is_re_read_without_waiting_for_the_interval(table, refresher):
    table.rows["x1"]["suspended"] = True  # X changes in the database
    refresher.request("X")
    await _until(lambda: akm._KEY_CACHE["x1"].get("suspended"))
    assert table.tenant_reads == [{"X"}] and table.full_reads == 1


async def test_a_full_reload_read_before_a_change_cannot_land_after_it(table, refresher):
    table.hold_full_read = asyncio.Event()
    table.full_read_started.clear()
    refresher.request()                     # a full reload reads the table...
    await table.full_read_started.wait()
    table.rows["x1"]["suspended"] = True    # ...then X is suspended and announced...
    refresher.request("X")
    table.hold_full_read.set()              # ...and the full reload lands after that
    await _until(lambda: table.full_reads == 2 and table.tenant_reads == [{"X"}])
    await asyncio.sleep(0.05)
    assert akm._KEY_CACHE["x1"].get("suspended") is True


async def test_a_failed_re_read_is_retried_as_a_full_reload(table, refresher, monkeypatch):
    monkeypatch.setattr(pg_key_store, "_RETRY_SECONDS", 0)
    table.fail_next_tenant_read = True
    table.rows["x1"]["suspended"] = True
    refresher.request("X")
    await _until(lambda: table.full_reads == 2)
    await _until(lambda: akm._KEY_CACHE["x1"].get("suspended"))
