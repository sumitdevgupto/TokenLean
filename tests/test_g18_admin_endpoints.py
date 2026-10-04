"""
G18 Observability - Admin Endpoint Tests

Tests the admin endpoints:
- POST /admin/alert-webhook (Alertmanager integration)
- POST /admin/usage-export (Usage-records export over Postgres usage_events)
- GET /admin/budget-status is not served (it read Redis keys nothing wrote)
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestAlertWebhook:
    """Test POST /admin/alert-webhook endpoint."""

    @pytest.fixture
    def alertmanager_payload(self):
        """Sample Alertmanager webhook payload."""
        return {
            "version": "4",
            "groupKey": "{team='backend', feature='api'}",
            "status": "firing",
            "receiver": "token-proxy",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "TokenBudgetExceeded",
                        "team": "backend",
                        "feature": "api",
                        "severity": "warning",
                    },
                    "annotations": {
                        "summary": "Token budget exceeded for team backend",
                        "description": "Daily token usage exceeded threshold",
                    },
                    "startsAt": "2026-06-08T10:00:00Z",
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_alert_webhook_accepts_valid_payload(self, client, alertmanager_payload):
        """Test that alert webhook accepts valid Alertmanager payload."""
        response = await client.post(
            "/admin/alert-webhook",
            json=alertmanager_payload,
            headers={"Authorization": "Bearer admin-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["alerts_count"] == 1
        assert "alert_id" in data

    @pytest.mark.asyncio
    async def test_alert_webhook_stores_in_redis(self, client, alertmanager_payload):
        """Test that alerts are stored in Redis with TTL."""
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            response = await client.post(
                "/admin/alert-webhook",
                json=alertmanager_payload,
                headers={"Authorization": "Bearer admin-key"},
            )

        assert response.status_code == 200
        # Verify Redis setex was called with 30-day TTL
        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args[0]
        assert call_args[0].startswith("tok_opt:alert:")
        assert call_args[1] == 30 * 86400  # 30 days

    @pytest.mark.asyncio
    async def test_alert_webhook_handles_multiple_alerts(self, client):
        """Test handling multiple alerts in one payload."""
        payload = {
            "version": "4",
            "status": "firing",
            "alerts": [
                {"status": "firing", "labels": {"alertname": "Alert1"}, "annotations": {}},
                {"status": "firing", "labels": {"alertname": "Alert2"}, "annotations": {}},
                {"status": "resolved", "labels": {"alertname": "Alert3"}, "annotations": {}},
            ],
        }

        response = await client.post(
            "/admin/alert-webhook",
            json=payload,
            headers={"Authorization": "Bearer admin-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["alerts_count"] == 3

    @pytest.mark.asyncio
    async def test_alert_webhook_requires_auth(self, client, alertmanager_payload):
        """Test that alert webhook requires authentication."""
        response = await client.post(
            "/admin/alert-webhook",
            json=alertmanager_payload,
            # No Authorization header
        )

        assert response.status_code == 401

    # Alertmanager posted with no credentials and the webhook takes only an admin key, so
    # every alert was refused. It now presents its own token (ALERT_WEBHOOK_TOKEN), which
    # opens this endpoint and nothing else.
    @pytest.mark.asyncio
    async def test_the_alertmanager_token_is_accepted(self, client, alertmanager_payload):
        import main
        mock_redis = AsyncMock()
        with patch.object(main, "_ALERT_WEBHOOK_TOKEN", "am-token-123"), \
             patch("cache.redis_pool.get_redis", return_value=mock_redis):
            response = await client.post("/admin/alert-webhook", json=alertmanager_payload,
                                         headers={"Authorization": "Bearer am-token-123"})
        assert response.status_code == 200 and response.json()["alerts_count"] == 1
        stored = json.loads(mock_redis.setex.call_args[0][2])
        assert stored["processed_by"] == "alertmanager"

    @pytest.mark.asyncio
    async def test_a_wrong_or_unset_token_is_refused(self, client, alertmanager_payload):
        import main
        with patch.object(main, "_ALERT_WEBHOOK_TOKEN", "am-token-123"):
            wrong = await client.post("/admin/alert-webhook", json=alertmanager_payload,
                                      headers={"Authorization": "Bearer am-token-999"})
        with patch.object(main, "_ALERT_WEBHOOK_TOKEN", ""):
            empty = await client.post("/admin/alert-webhook", json=alertmanager_payload,
                                      headers={"Authorization": "Bearer "})
        assert wrong.status_code == 401 and empty.status_code == 401

    @pytest.mark.asyncio
    async def test_the_token_opens_no_other_admin_route(self, client):
        import main
        with patch.object(main, "_ALERT_WEBHOOK_TOKEN", "am-token-123"):
            response = await client.post("/admin/usage-export", json={},
                                         headers={"Authorization": "Bearer am-token-123"})
        assert response.status_code == 401


class TestBudgetStatus:
    """GET /admin/budget-status summed Redis keys nothing in the proxy wrote, so it reported
    zero consumption for every team and feature however much was spent. It is not served;
    per-tenant spend is in /admin/usage-export and the spend cap."""

    @pytest.mark.asyncio
    async def test_budget_status_is_not_served(self, client):
        response = await client.get("/admin/budget-status",
                                    headers={"Authorization": "Bearer admin-key"})
        assert response.status_code == 404


class _FakeConn:
    """Minimal asyncpg-connection stand-in returning canned rows from fetch()."""

    def __init__(self, rows):
        self._rows = rows
        self.fetched_args = None

    async def fetch(self, sql, *args):
        self.fetched_args = (sql, args)
        return self._rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def acquire(self):
        return _FakeAcquire(self.conn)


class TestUsageExport:
    """Test POST /admin/usage-export endpoint (Postgres usage_events)."""

    @pytest.mark.asyncio
    async def test_usage_export_generates_jsonl(self, client, monkeypatch):
        """Export streams JSONL rows from usage_events with serialisable types."""
        import datetime

        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        rows = [{
            "tenant_id": "nova-med",
            "request_id": "req-1",
            "timestamp": datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            "baseline_tokens": 100,
            "optimised_tokens": 60,
            "tokens_saved": 40,
            "cost_saved_usd": 0.0123,
            "groups_applied": ["G01", "G05"],
            "pricing_tier": "free",
            "model": "gpt-4o",
            "routed_model": "gpt-4o-mini",
        }]

        async def _fake_get_pool(_dsn):
            return _FakePool(rows)

        with patch("cache.pg_pool.get_pg_pool", _fake_get_pool):
            response = await client.post(
                "/admin/usage-export",
                json={"start_date": "2024-06-01", "end_date": "2024-06-08"},
                headers={"Authorization": "Bearer admin-key"},
            )

        assert response.status_code == 200
        assert "json" in response.headers.get("content-type", "").lower()
        first = json.loads(response.text.strip().splitlines()[0])
        assert first["tenant_id"] == "nova-med"
        assert first["tokens_saved"] == 40
        # TIMESTAMPTZ → ISO string, NUMERIC → float (both JSON-native)
        assert isinstance(first["timestamp"], str)
        assert isinstance(first["cost_saved_usd"], float)

    @pytest.mark.asyncio
    async def test_usage_export_filters_by_tenant(self, client, monkeypatch):
        """Tenant filter is pushed into the SQL WHERE clause."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        pool = _FakePool([])

        async def _fake_get_pool(_dsn):
            return pool

        with patch("cache.pg_pool.get_pg_pool", _fake_get_pool):
            response = await client.post(
                "/admin/usage-export",
                json={"start_date": "2024-06-01", "end_date": "2024-06-08", "tenant_id": "nova-med"},
                headers={"Authorization": "Bearer admin-key"},
            )

        assert response.status_code == 200
        sql, args = pool.conn.fetched_args
        assert "tenant_id = $3" in sql
        assert "nova-med" in args

    @pytest.mark.asyncio
    async def test_a_non_admin_export_is_always_its_own_tenant(self, client, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        pool = _FakePool([])

        async def _fake_get_pool(_dsn):
            return pool

        with patch("cache.pg_pool.get_pg_pool", _fake_get_pool):
            response = await client.post(
                "/admin/usage-export", json={"tenant_id": "nova-med"},
                headers={"Authorization": "Bearer test-key"})     # a legacy, non-admin key
        assert response.status_code == 200
        sql, args = pool.conn.fetched_args
        assert "tenant_id = $3" in sql and args[-1] == "default" and "nova-med" not in args

    @pytest.mark.asyncio
    async def test_usage_export_requires_auth(self, client):
        """Test that usage export requires authentication."""
        response = await client.post("/admin/usage-export", json={})

        assert response.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
