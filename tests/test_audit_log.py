"""F1-T: Tests for AuditLogger."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


class _MockConn:
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _MockPool:
    def __init__(self):
        self.conn = _MockConn()

    def acquire(self):
        return _AcquireCtx(self.conn)


class _Savings:
    def __init__(self):
        self.step_savings = []
        self.baseline_tokens = 100
        self.final_tokens_sent = 60


class _Ctx:
    def __init__(self, tenant_id="tenant-a", request_id="req-001"):
        self.tenant_id = tenant_id
        self.request_id = request_id
        self.user_id = "user-123"
        self.otel_span = None
        self.savings = _Savings()
        self.config = {}


class TestAuditLoggerNoPool:
    def test_no_pool_is_noop(self):
        from audit.log import AuditLogger
        logger = AuditLogger(db_pool=None)
        ctx = _Ctx()
        asyncio.run(logger.log(ctx, {}))

    def test_no_pool_does_not_raise(self):
        from audit.log import AuditLogger
        logger = AuditLogger(db_pool=None)
        ctx = _Ctx()
        asyncio.run(logger.log(ctx, {"usage": {}}))


class TestAuditLoggerInsert:
    def _logger(self):
        from audit.log import AuditLogger
        pool = _MockPool()
        logger = AuditLogger(db_pool=pool)
        return logger, pool

    def test_insert_called_once_per_request(self):
        logger, pool = self._logger()
        ctx = _Ctx()
        asyncio.run(logger.log(ctx, {}))
        assert len(pool.conn.executed) == 1

    def test_insert_uses_correct_tenant_id(self):
        logger, pool = self._logger()
        ctx = _Ctx(tenant_id="acme-corp")
        asyncio.run(logger.log(ctx, {}))
        query, args = pool.conn.executed[0]
        assert "audit_events" in query
        assert "acme-corp" in args

    def test_insert_uses_correct_request_id(self):
        logger, pool = self._logger()
        ctx = _Ctx(request_id="req-xyz-999")
        asyncio.run(logger.log(ctx, {}))
        _, args = pool.conn.executed[0]
        assert "req-xyz-999" in args

    def test_insert_calculates_tokens_saved(self):
        logger, pool = self._logger()
        ctx = _Ctx()
        ctx.savings.baseline_tokens = 200
        ctx.savings.final_tokens_sent = 120
        asyncio.run(logger.log(ctx, {}))
        _, args = pool.conn.executed[0]
        assert 80 in args

    def test_exception_does_not_propagate(self):
        from audit.log import AuditLogger

        class _BadPool:
            def acquire(self):
                raise RuntimeError("db connection refused")

        logger = AuditLogger(db_pool=_BadPool())
        ctx = _Ctx()
        asyncio.run(logger.log(ctx, {}))  # should not raise
