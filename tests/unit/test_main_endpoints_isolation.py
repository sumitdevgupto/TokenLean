"""H1/H2 — admin-endpoint authorization and /metrics gating (main.py)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


# ── pure helpers ─────────────────────────────────────────────────────────────
class TestCallerHelpers:
    def test_caller_tenant_id_from_metadata(self):
        assert main._caller_tenant_id({"tenant_id": "nova-med"}) == "nova-med"

    def test_caller_tenant_id_legacy_is_default(self):
        assert main._caller_tenant_id(None) == "default"

    def test_require_admin_blocks_non_admin(self):
        with pytest.raises(HTTPException) as exc:
            main._require_admin({"tenant_id": "nova-med"}, "x")
        assert exc.value.status_code == 403

    def test_require_admin_allows_admin(self):
        # Should not raise.
        main._require_admin({"tenant_id": "ops", "admin": True}, "x")


# Plain TestClient (no `with`) so the app's startup/shutdown lifespan — which
# tears down the shared redis pool and would collide with other tests — does NOT
# run. These endpoints need no lifespan state.
_client = TestClient(main.app)


# ── H2: /metrics scrape-token gate ───────────────────────────────────────────
class TestMetricsGate:
    def test_metrics_open_when_token_unset(self):
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", ""):
            assert _client.get("/metrics").status_code == 200

    def test_metrics_rejects_missing_token(self):
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", "s3cret"):
            assert _client.get("/metrics").status_code == 401

    def test_metrics_accepts_correct_token(self):
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", "s3cret"):
            r = _client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
            assert r.status_code == 200


# ── H2: suspended keys are rejected at _authenticate (403) ───────────────────
class TestSuspendedKeyGate:
    _REQ = SimpleNamespace(headers={"Authorization": "Bearer tok-x"})

    async def test_suspended_key_rejected_403(self):
        meta = {"tenant_id": "acme", "tier": "enterprise", "suspended": True}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)):
            with pytest.raises(HTTPException) as exc:
                await main._authenticate(self._REQ)
        assert exc.value.status_code == 403

    async def test_non_suspended_key_passes(self):
        meta = {"tenant_id": "acme", "tier": "enterprise", "suspended": False}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value={}):
            user_id, api_key, returned = await main._authenticate(self._REQ)
        assert user_id == "acme"
        assert returned == meta


# ── Contract gate: contract_inactive keys are rejected at _authenticate (403) ─
class TestContractGate:
    _REQ = SimpleNamespace(headers={"Authorization": "Bearer tok-x"},
                           client=SimpleNamespace(host="1.2.3.4"))

    async def test_contract_inactive_rejected_403(self):
        meta = {"tenant_id": "acme", "tier": "enterprise", "contract_inactive": True}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value={}):
            with pytest.raises(HTTPException) as exc:
                await main._authenticate(self._REQ)
        assert exc.value.status_code == 403

    async def test_active_contract_passes(self):
        meta = {"tenant_id": "acme", "tier": "enterprise"}  # absent flag ⇒ active
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value={}):
            user_id, _api_key, _returned = await main._authenticate(self._REQ)
        assert user_id == "acme"


# ── IP allowlist gate at _authenticate ────────────────────────────────────────
class TestIpAllowlistGate:
    def _req(self, xff):
        return SimpleNamespace(headers={"Authorization": "Bearer tok-x", "x-forwarded-for": xff},
                               client=SimpleNamespace(host="9.9.9.9"))

    _CFG = {"ip_allowlist": {"enabled": True, "trust_x_forwarded_for": True,
                             "global_cidrs": ["203.0.113.0/24"]}}

    async def test_ip_outside_allowlist_rejected_403(self):
        meta = {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"]}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value=self._CFG):
            with pytest.raises(HTTPException) as exc:
                await main._authenticate(self._req("8.8.8.8"))
        assert exc.value.status_code == 403

    async def test_ip_in_tenant_allowlist_passes(self):
        meta = {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"]}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value=self._CFG):
            user_id, _k, _m = await main._authenticate(self._req("10.1.2.3"))
        assert user_id == "acme"

    async def test_ip_in_global_allowlist_passes(self):
        meta = {"tenant_id": "acme"}  # no per-tenant list → bound to global
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value=self._CFG):
            user_id, _k, _m = await main._authenticate(self._req("203.0.113.9"))
        assert user_id == "acme"

    async def test_disabled_allowlist_is_noop(self):
        meta = {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"]}
        cfg = {"ip_allowlist": {"enabled": False}}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value=cfg):
            user_id, _k, _m = await main._authenticate(self._req("8.8.8.8"))
        assert user_id == "acme"


# ── H1: admin endpoints require the admin scope ──────────────────────────────
class TestAdminEndpointAuthz:
    def _auth(self, metadata):
        # Patch _authenticate to return (user_id, api_key, tenant_metadata).
        async def _fake(_request):
            return "u", "tok-x", metadata
        return patch.object(main, "_authenticate", _fake)

    def test_tool_governance_forbidden_for_non_admin(self):
        with self._auth({"tenant_id": "nova-med"}):
            r = _client.get("/admin/tool-governance", headers={"Authorization": "Bearer x"})
            assert r.status_code == 403

    def test_budget_status_forbidden_for_non_admin(self):
        with self._auth({"tenant_id": "nova-med"}):
            r = _client.get("/admin/budget-status", headers={"Authorization": "Bearer x"})
            assert r.status_code == 403
