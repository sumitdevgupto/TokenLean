"""Adversarial / negative tests for the hardened /ingest-doc webhook (data-safety).

The webhook is the write-side gate: it must reject forged OIDC tokens, notifications for
buckets that don't belong to any tenant, and malformed payloads — so a document can never
be ingested into a tenant it doesn't belong to.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import base64
import json
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main

_client = TestClient(main.app)


class TestPayloadParsing:
    def test_flat_payload(self):
        assert main._parse_ingest_payload({"bucket": "b", "name": "o"}) == ("b", "o")

    def test_pubsub_envelope_via_attributes(self):
        payload = {"message": {"attributes": {"bucketId": "b", "objectId": "o"}}}
        assert main._parse_ingest_payload(payload) == ("b", "o")

    def test_pubsub_envelope_via_base64_data(self):
        data = base64.b64encode(json.dumps({"bucket": "b", "name": "o"}).encode()).decode()
        assert main._parse_ingest_payload({"message": {"data": data}}) == ("b", "o")

    def test_garbage_data_yields_empty(self):
        b, o = main._parse_ingest_payload({"message": {"data": "!!!not-base64!!!"}})
        assert (b, o) == ("", "")


class TestOidcGate:
    def test_disabled_by_default_is_noop(self, monkeypatch):
        monkeypatch.delenv("INGEST_REQUIRE_OIDC", raising=False)
        # Should not raise even with no Authorization header.
        req = _FakeReq(headers={})
        main._verify_ingest_oidc(req)

    def test_enabled_missing_token_401(self, monkeypatch):
        monkeypatch.setenv("INGEST_REQUIRE_OIDC", "true")
        with pytest.raises(HTTPException) as exc:
            main._verify_ingest_oidc(_FakeReq(headers={}))
        assert exc.value.status_code == 401

    def test_enabled_forged_token_401(self, monkeypatch):
        monkeypatch.setenv("INGEST_REQUIRE_OIDC", "true")
        req = _FakeReq(headers={"Authorization": "Bearer forged.jwt.token"})
        # google verify raises → 401 (library not resolving a real token).
        with pytest.raises(HTTPException) as exc:
            main._verify_ingest_oidc(req)
        assert exc.value.status_code == 401


class _FakeReq:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.asyncio
class TestWebhookIsolation:
    async def _post(self, body):
        return _client.post("/ingest-doc", json=body)

    async def test_unknown_bucket_rejected_403(self, monkeypatch):
        monkeypatch.delenv("INGEST_REQUIRE_OIDC", raising=False)
        with patch.object(main, "_ingest_tenant_registry", AsyncMock(return_value=(True, ["NOVA-STG-01"]))), \
             patch.object(main, "trigger_doc_ingestion", AsyncMock(return_value=True)) as trig:
            resp = await self._post({"bucket": "token-opt-docs-evil", "name": "x.pdf"})
        assert resp.status_code == 403
        trig.assert_not_awaited()  # never ingested

    async def test_known_bucket_threads_derived_tenant(self, monkeypatch):
        monkeypatch.delenv("INGEST_REQUIRE_OIDC", raising=False)
        with patch.object(main, "_ingest_tenant_registry", AsyncMock(return_value=(True, ["NOVA-STG-01"]))), \
             patch.object(main, "trigger_doc_ingestion", AsyncMock(return_value=True)) as trig:
            resp = await self._post({"bucket": "token-opt-docs-nova-stg-01", "name": "docs/a.pdf"})
        assert resp.status_code == 200
        # Ingested for the REVERSE-DERIVED tenant, not a client-supplied one.
        trig.assert_awaited_once()
        assert trig.await_args.kwargs.get("tenant_id") == "NOVA-STG-01"

    async def test_empty_but_configured_registry_fails_closed_403(self, monkeypatch):
        """THE fail-open fix: multi-tenant mode (registry configured) with an empty tenant
        list (fresh deploy, no signups yet) must 403 any bucket, NOT fall open to default."""
        monkeypatch.delenv("INGEST_REQUIRE_OIDC", raising=False)
        with patch.object(main, "_ingest_tenant_registry", AsyncMock(return_value=(True, []))), \
             patch.object(main, "trigger_doc_ingestion", AsyncMock(return_value=True)) as trig:
            resp = await self._post({"bucket": "token-opt-docs-nova-stg-01", "name": "docs/a.pdf"})
        assert resp.status_code == 403
        trig.assert_not_awaited()  # never ingested as "default"

    async def test_unconfigured_registry_uses_default(self, monkeypatch):
        """Single-tenant/local (registry NOT configured, DATABASE_URL unset) keeps working:
        the doc is ingested as tenant_id='default'."""
        monkeypatch.delenv("INGEST_REQUIRE_OIDC", raising=False)
        with patch.object(main, "_ingest_tenant_registry", AsyncMock(return_value=(False, []))), \
             patch.object(main, "trigger_doc_ingestion", AsyncMock(return_value=True)) as trig:
            resp = await self._post({"bucket": "some-bucket", "name": "docs/a.pdf"})
        assert resp.status_code == 200
        trig.assert_awaited_once()
        assert trig.await_args.kwargs.get("tenant_id") == "default"

    async def test_missing_fields_400(self, monkeypatch):
        monkeypatch.delenv("INGEST_REQUIRE_OIDC", raising=False)
        with patch.object(main, "_ingest_tenant_registry", AsyncMock(return_value=(False, []))):
            resp = await self._post({"bucket": "", "name": ""})
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestIngestRegistryResolution:
    """_ingest_tenant_registry must distinguish 'not configured' from 'configured-but-empty'."""

    def _reset_cache(self):
        main._INGEST_REGISTRY_CACHE.update(configured=False, tenants=[], ts=0.0, valid=False)

    async def test_no_database_url_is_unconfigured(self, monkeypatch):
        self._reset_cache()
        monkeypatch.delenv("DATABASE_URL", raising=False)
        configured, tenants = await main._ingest_tenant_registry()
        assert configured is False and tenants == []

    async def test_db_error_stays_configured_and_empty(self, monkeypatch):
        """DATABASE_URL set but the query fails (e.g. portal_users missing) → configured=True,
        empty list → caller fails closed instead of the webhook 500ing."""
        self._reset_cache()
        monkeypatch.setenv("DATABASE_URL", "postgres://x")
        with patch("cache.pg_pool.get_pg_pool", AsyncMock(side_effect=Exception("relation portal_users does not exist"))):
            configured, tenants = await main._ingest_tenant_registry()
        assert configured is True and tenants == []
