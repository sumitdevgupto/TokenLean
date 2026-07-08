"""WS8 — audit engine: log_config_change + ensure_audit_schema (core, ships OSS)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import json
import pytest

from audit.log import AuditLogger, ensure_audit_schema, AUDIT_EVENTS_DDL


class _AcquireCtx:
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn
    async def __aexit__(self, *_): return False


class _Conn:
    def __init__(self, fail_details=False):
        self.calls = []
        self.fail_details = fail_details

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail_details and "details" in sql:
            exc = type("UndefinedColumnError", (Exception,), {})()
            raise exc
        return "INSERT 0 1"


class _Pool:
    def __init__(self, conn): self._conn = conn
    def acquire(self): return _AcquireCtx(self._conn)


async def test_log_config_change_inserts_with_details():
    conn = _Conn()
    ok = await AuditLogger(_Pool(conn)).log_config_change(
        tenant_id="CARD-PRD-01", actor="owner@cardinal.com",
        action="config.model_prefs_updated",
        details={"changed": {"proxy.default_model": {"from": None, "to": "gpt-4o-mini"}}},
    )
    assert ok is True
    sql, args = conn.calls[0]
    assert "details" in sql and args[0] == "CARD-PRD-01"
    assert args[3] == "config.model_prefs_updated" and args[4] == "owner@cardinal.com"
    assert "gpt-4o-mini" in args[5]  # serialised details JSON


async def test_log_config_change_no_pool_is_noop():
    assert await AuditLogger(None).log_config_change(
        tenant_id="t", actor="a", action="x") is False


async def test_log_config_change_retries_without_details_column():
    # Rollout window: DB predates the details column → retry without it, event kept.
    conn = _Conn(fail_details=True)
    ok = await AuditLogger(_Pool(conn)).log_config_change(
        tenant_id="t1", actor="a", action="provider_key.set",
        details={"provider": "openai", "last4": "abcd"})
    assert ok is True
    assert len(conn.calls) == 2                      # details attempt + fallback
    assert "details" not in conn.calls[1][0]          # fallback insert has no details


async def test_log_config_change_never_raises_on_db_error():
    class _Boom:
        def acquire(self): raise RuntimeError("db down")
    assert await AuditLogger(_Boom()).log_config_change(
        tenant_id="t", actor="a", action="x") is False


async def test_ensure_audit_schema_noop_without_pool():
    await ensure_audit_schema(None)  # must not raise


def test_ddl_contains_details_alter():
    assert "ADD COLUMN IF NOT EXISTS details JSONB" in AUDIT_EVENTS_DDL
