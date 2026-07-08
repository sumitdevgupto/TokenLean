"""WS25 — retention engine: purge pass, billing floor clamp, fail-soft (core)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

from retention import run_retention_pass, _MIN_USAGE_DAYS


class _AcquireCtx:
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn
    async def __aexit__(self, *_): return False


class _Conn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "DELETE 7"


class _Pool:
    def __init__(self, conn): self._conn = conn
    def acquire(self): return _AcquireCtx(self._conn)


async def test_pass_noop_without_pool():
    assert await run_retention_pass(None, {"audit_days": 30}) == {}


async def test_pass_purges_configured_tables():
    conn = _Conn()
    out = await run_retention_pass(_Pool(conn), {
        "audit_days": 30, "usage_days": 0, "cache_l2_expired_cleanup": True})
    assert out["audit_events"] == 7 and out["cache_l2"] == 7
    assert "usage_events" not in out          # 0 = keep forever
    sqls = " ".join(s for s, _ in conn.executed)
    assert "audit_events" in sqls and "cache_l2" in sqls and "usage_events" not in sqls


async def test_usage_days_clamped_to_billing_floor():
    conn = _Conn()
    await run_retention_pass(_Pool(conn), {
        "usage_days": 30, "cache_l2_expired_cleanup": False})
    # The DELETE must run with the clamped floor, not the configured 30 days.
    (sql, args), = conn.executed
    assert "usage_events" in sql and args[0] == str(_MIN_USAGE_DAYS)


async def test_pass_survives_table_errors():
    class _BoomConn(_Conn):
        async def execute(self, sql, *args):
            raise RuntimeError("nope")

    out = await run_retention_pass(_Pool(_BoomConn()), {"audit_days": 5})
    assert out == {}  # error swallowed, loop lives on
