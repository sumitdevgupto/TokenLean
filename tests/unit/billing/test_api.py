"""Tests for the billing FastAPI router (C4-T)."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_row(**kwargs):
    """Return a dict that mimics an asyncpg Record."""
    return kwargs


def _make_pool(rows):
    """Return a mock asyncpg pool whose conn.fetch returns *rows*."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))
    return pool, conn


# ── import guard ─────────────────────────────────────────────────────────────

pytest.importorskip("fastapi", reason="fastapi not installed")
from fastapi.testclient import TestClient
from fastapi import FastAPI
from billing.api import create_billing_router


# ── fixtures ─────────────────────────────────────────────────────────────────

USAGE_ROWS = [
    _make_row(
        tenant_id="tenant-a",
        request_id="req-1",
        timestamp="2026-06-01T12:00:00",
        baseline_tokens=1000,
        optimised_tokens=600,
        tokens_saved=400,
        cost_saved_usd=0.002,
        groups_applied=["G01", "G05"],
        pricing_tier="enterprise",
    )
]

MONTHLY_ROWS = [
    _make_row(
        month="2026-06-01T00:00:00",
        total_tokens_saved=8000,
        total_cost_saved_usd=0.04,
        request_count=20,
    )
]

SAVINGS_ROWS = [
    _make_row(group_id="G01", tokens_saved=5000, cost_saved_usd=0.025, requests=12),
    _make_row(group_id="G05", tokens_saved=3000, cost_saved_usd=0.015, requests=12),
]


def _app(pool):
    app = FastAPI()
    router = create_billing_router(db_pool=pool)
    app.include_router(router)
    return app


# ── tests: /usage ─────────────────────────────────────────────────────────────

class TestGetUsage:
    def test_returns_200_with_rows(self):
        pool, conn = _make_pool(USAGE_ROWS)
        client = TestClient(_app(pool))
        resp = client.get("/api/v1/tenants/tenant-a/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["tenant_id"] == "tenant-a"
        assert data[0]["tokens_saved"] == 400

    def test_returns_404_when_no_rows(self):
        pool, conn = _make_pool([])
        client = TestClient(_app(pool))
        resp = client.get("/api/v1/tenants/ghost/usage")
        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    def test_passes_tenant_id_to_query(self):
        pool, conn = _make_pool(USAGE_ROWS)
        client = TestClient(_app(pool))
        client.get("/api/v1/tenants/tenant-a/usage")
        call_args = conn.fetch.call_args
        # First positional arg is the SQL, second is the tenant_id param
        assert "tenant-a" in call_args.args

    def test_503_when_no_db_pool(self):
        app = FastAPI()
        router = create_billing_router(db_pool=None)
        app.include_router(router)
        client = TestClient(app)
        resp = client.get("/api/v1/tenants/tenant-a/usage")
        assert resp.status_code == 503


# ── tests: /usage/monthly ─────────────────────────────────────────────────────

class TestGetUsageMonthly:
    def test_returns_200_with_aggregated_data(self):
        pool, conn = _make_pool(MONTHLY_ROWS)
        client = TestClient(_app(pool))
        resp = client.get("/api/v1/tenants/tenant-a/usage/monthly")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["total_tokens_saved"] == 8000

    def test_empty_result_returns_empty_list(self):
        pool, conn = _make_pool([])
        client = TestClient(_app(pool))
        resp = client.get("/api/v1/tenants/tenant-a/usage/monthly")
        assert resp.status_code == 200
        assert resp.json() == []


# ── tests: /savings-report ────────────────────────────────────────────────────

class TestGetSavingsReport:
    def test_returns_savings_by_group(self):
        pool, conn = _make_pool(SAVINGS_ROWS)
        client = TestClient(_app(pool))
        resp = client.get("/api/v1/tenants/tenant-a/savings-report")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        groups = {row["group_id"] for row in data}
        assert "G01" in groups

    def test_empty_savings_returns_empty_list(self):
        pool, conn = _make_pool([])
        client = TestClient(_app(pool))
        resp = client.get("/api/v1/tenants/tenant-a/savings-report")
        assert resp.status_code == 200
        assert resp.json() == []
