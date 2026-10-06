"""Billing, the security audit log and per-tenant settings get wired even when the database is
down at start.

Startup used to try once: after a database blip the instance served every request unbilled,
unaudited and without the tenant settings made in the portal, until it was restarted. Now a
self-hosted proxy serves meanwhile, retries in the background and says so on /health, and a
managed one (MANAGED_DEPLOY=true) waits at startup, before the startup hooks, so it serves
nothing without them. The database is a fake whose failures each test sets.
"""
import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main

MAIN_PY = Path(main.__file__)
ALL_PARTS = ["billing", "audit", "tenant_config"]


class FakeDatabase:
    """Stands in for get_pg_pool, the two schema steps and the retention loop."""

    def __init__(self):
        self.pool = object()
        self.fail_connects = 0      # this many connection attempts fail
        self.fail_billing = 0       # this many usage_events schema steps fail
        self.fail_audit = 0
        self.connects = 0
        self.billing_runs = 0
        self.audit_runs = 0
        self.retention_pools = []

    async def get_pg_pool(self, dsn):
        self.connects += 1
        if self.connects <= self.fail_connects:
            raise OSError("could not connect to db.internal:5432 as tl_admin")
        return self.pool

    async def ensure_usage_events_schema(self, pool):
        assert pool is self.pool
        self.billing_runs += 1
        if self.billing_runs <= self.fail_billing:
            raise RuntimeError("usage_events schema step failed")

    async def ensure_audit_schema(self, pool):
        assert pool is self.pool
        self.audit_runs += 1
        if self.audit_runs <= self.fail_audit:
            raise RuntimeError("audit_events schema step failed")

    async def run_retention_loop(self, get_pool, get_config):
        self.retention_pools.append(get_pool())


@pytest.fixture
async def db(monkeypatch):
    import audit.log
    import billing.models
    import cache.pg_pool
    import retention

    fake = FakeDatabase()
    monkeypatch.setattr(cache.pg_pool, "get_pg_pool", fake.get_pg_pool)
    monkeypatch.setattr(billing.models, "ensure_usage_events_schema",
                        fake.ensure_usage_events_schema)
    monkeypatch.setattr(audit.log, "ensure_audit_schema", fake.ensure_audit_schema)
    monkeypatch.setattr(retention, "run_retention_loop", fake.run_retention_loop)
    for name in ("_db_pool", "_usage_meter", "_audit_logger", "_db_retry_task",
                 "_retention_task"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setattr(main, "_db_expected", False)
    monkeypatch.setattr(main._pipeline._tenant_config_loader, "_db_pool", None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://tl@db.internal/tl")
    monkeypatch.delenv("MANAGED_DEPLOY", raising=False)
    yield fake
    tasks = [t for t in (main._db_retry_task, main._retention_task) if t is not None]
    if main._db_retry_task is not None:
        main._db_retry_task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def sleeps(monkeypatch):
    """Records each retry delay; the sleep itself only yields to the loop."""
    delays = []
    real_sleep = asyncio.sleep

    async def sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return delays


class HeldRetries(list):
    """The background retry's delays, in order. Each retry also waits for release(), so what a
    test sees after startup does not depend on how far the retry got before the test looked."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    def release(self):
        self.gate.set()


@pytest.fixture
def held(monkeypatch):
    """`sleeps` for a self-hosted proxy's background retry, which is held until the test
    releases it."""
    retries = HeldRetries()
    real_sleep = asyncio.sleep

    async def sleep(delay, *args, **kwargs):
        retries.append(delay)
        await retries.gate.wait()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return retries


# The fixtures patch asyncio.sleep; the loop turns in _start need the real one.
_real_sleep = asyncio.sleep


async def _start():
    await asyncio.wait_for(main._wire_database_at_startup(), 5)
    # Python 3.11's wait_for gives a task that startup created a few event-loop turns before it
    # returns, and 3.12's none (CI runs 3.11, like the images). Turn the loop here, so a test
    # holds on both.
    for _ in range(5):
        await _real_sleep(0)


async def _retried(held):
    held.release()
    await asyncio.wait_for(main._db_retry_task, 5)


def _all_wired(db) -> bool:
    return (main._usage_meter is not None and main._audit_logger is not None
            and main._pipeline._tenant_config_loader._db_pool is db.pool)


OK = {"status": "ok", "version": "1.0.0"}


class TestAReachableDatabase:

    async def test_every_part_is_wired_at_startup(self, db):
        await _start()
        assert _all_wired(db)
        assert main._db_retry_task is None
        await main._retention_task
        assert db.retention_pools == [db.pool]
        assert main._db_not_wired() == []
        assert await main.health() == OK

    async def test_without_database_url_nothing_is_tried(self, db, monkeypatch):
        monkeypatch.delenv("DATABASE_URL")
        await _start()
        assert db.connects == 0 and main._db_retry_task is None
        assert main._usage_meter is None
        assert await main.health() == OK


class TestSelfHostedOutageAtStart:

    async def test_it_serves_at_once_and_reports_what_is_missing(self, db, held):
        db.fail_connects = 3
        await _start()
        assert db.connects == 1
        assert main._db_retry_task is not None
        assert main._db_not_wired() == ALL_PARTS
        assert await main.health() == {"status": "degraded", "version": "1.0.0",
                                       "not_wired": ALL_PARTS}

    async def test_the_background_retry_wires_it_when_the_database_is_back(self, db, held):
        db.fail_connects = 3
        await _start()
        await _retried(held)
        assert db.connects == 4
        assert held == [1.0, 2.0, 4.0]
        assert _all_wired(db)
        assert await main.health() == OK
        await main._retention_task
        assert db.retention_pools == [db.pool]

    @pytest.mark.parametrize("part", ["billing", "audit"])
    async def test_a_failing_schema_step_leaves_the_other_parts_wired(self, db, held, part):
        setattr(db, f"fail_{part}", 1)
        await _start()
        assert main._db_not_wired() == [part]
        assert (await main.health())["not_wired"] == [part]
        await _retried(held)
        assert _all_wired(db)
        # Only the failed step runs again: one connection, one retention loop.
        assert (db.billing_runs, db.audit_runs) == ((2, 1) if part == "billing" else (1, 2))
        assert db.connects == 1
        await main._retention_task
        assert db.retention_pools == [db.pool]

    async def test_the_error_goes_to_the_log_and_not_to_health(self, db, held, caplog):
        db.fail_connects = 3
        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            await _start()
        assert "billing, audit, tenant_config not wired yet" in caplog.text
        assert "db.internal:5432 as tl_admin" in caplog.text
        assert "Serving without billing, audit, tenant_config" in caplog.text
        body = json.dumps(await main.health())
        assert "db.internal" not in body and "tl_admin" not in body

    @pytest.mark.parametrize("value", ["false", "0", ""])
    async def test_other_managed_deploy_values_do_not_wait(self, db, held, monkeypatch, value):
        monkeypatch.setenv("MANAGED_DEPLOY", value)
        db.fail_connects = 3
        await _start()
        assert db.connects == 1 and main._db_retry_task is not None


def test_degraded_is_still_http_200(monkeypatch):
    for name in ("_db_pool", "_usage_meter", "_audit_logger"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setattr(main, "_db_expected", True)
    r = TestClient(main.app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "version": "1.0.0", "not_wired": ALL_PARTS}


class TestManagedOutageAtStart:

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    async def test_startup_returns_only_once_every_part_is_wired(self, db, sleeps,
                                                                 monkeypatch, value):
        monkeypatch.setenv("MANAGED_DEPLOY", value)
        db.fail_connects = 6
        await _start()
        assert db.connects == 7
        assert _all_wired(db)
        assert main._db_retry_task is None
        assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]

    async def test_it_also_waits_for_a_failing_schema_step(self, db, sleeps, monkeypatch):
        monkeypatch.setenv("MANAGED_DEPLOY", "true")
        db.fail_audit = 2
        await _start()
        assert _all_wired(db) and db.audit_runs == 3
        assert main._db_retry_task is None

    async def test_a_startup_hook_gets_the_pool_without_connecting_again(self, monkeypatch):
        import cache.pg_pool as pg_pool

        async def connect(*args, **kwargs):
            raise AssertionError("a second pool was created")

        pool = object()
        monkeypatch.setattr(pg_pool, "_pool", pool)
        monkeypatch.setattr(pg_pool, "_pool_dsn", "postgresql://tl@db.internal/tl")
        monkeypatch.setattr(pg_pool.asyncpg, "create_pool", connect)
        assert await pg_pool.get_pg_pool("postgresql://tl@db.internal/tl") is pool

    def test_startup_wires_the_database_before_the_startup_hooks(self):
        """The hooks (the commercial layer) connect once: in a managed deploy they must find
        the database already there."""
        tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        lifespan = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
        wires = [n.lineno for n in ast.walk(lifespan)
                 if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
                 and getattr(n.value.func, "id", "") == "_wire_database_at_startup"]
        hooks = [n.lineno for n in ast.walk(lifespan)
                 if isinstance(n, ast.For) and getattr(n.iter, "id", "") == "_startup_hooks"]
        assert len(wires) == 1 and len(hooks) == 1
        assert wires[0] < hooks[0]
