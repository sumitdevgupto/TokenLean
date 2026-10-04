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
    # /metrics lists tenant ids with their token and cost figures. With no token set it was
    # open to anyone who could reach the port; it now refuses unless the explicit local-dev
    # flag METRICS_ALLOW_UNAUTHENTICATED is on.
    def test_metrics_refused_when_token_unset(self):
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", ""), \
                patch.object(main, "_METRICS_ALLOW_UNAUTHENTICATED", False):
            r = _client.get("/metrics")
        assert r.status_code == 403
        assert "METRICS_SCRAPE_TOKEN" in r.json()["detail"]
        assert "token_opt_" not in r.text

    def test_metrics_open_when_token_unset_with_the_local_dev_flag(self):
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", ""), \
                patch.object(main, "_METRICS_ALLOW_UNAUTHENTICATED", True):
            assert _client.get("/metrics").status_code == 200

    def test_a_token_still_applies_with_the_flag_on(self):
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", "s3cret"), \
                patch.object(main, "_METRICS_ALLOW_UNAUTHENTICATED", True):
            assert _client.get("/metrics").status_code == 401

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("TRUE", True), (" 1 ", True), ("yes", True),
        ("", False), ("false", False), ("0", False), ("no", False), ("ture", False)])
    def test_the_flag_reads_like_the_other_switches(self, monkeypatch, value, expected):
        monkeypatch.setenv("METRICS_ALLOW_UNAUTHENTICATED", value)
        assert main._env_flag("METRICS_ALLOW_UNAUTHENTICATED") is expected

    def test_the_flag_is_off_when_unset(self, monkeypatch):
        monkeypatch.delenv("METRICS_ALLOW_UNAUTHENTICATED", raising=False)
        assert main._env_flag("METRICS_ALLOW_UNAUTHENTICATED") is False

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


# ── A key record with an empty tenant is malformed: refused, never "no filter" ─
class TestKeysWithoutATenant:
    _REQ = SimpleNamespace(headers={"Authorization": "Bearer tok-x"},
                           client=SimpleNamespace(host="1.2.3.4"))

    @pytest.mark.parametrize("tenant", ["", None])
    async def test_a_key_whose_tenant_is_empty_is_refused(self, tenant):
        meta = {"tenant_id": tenant, "tier": "free"}
        with patch.object(main, "validate_proxy_key", return_value=(True, tenant, meta)), \
             patch.object(main, "get_config", return_value={}):
            with pytest.raises(HTTPException) as exc:
                await main._authenticate(self._REQ)
        assert exc.value.status_code == 403

    def test_the_export_refuses_a_non_admin_caller_without_a_tenant(self, monkeypatch):
        async def _fake(_request):
            return "u", "tok-x", {"tenant_id": ""}
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        with patch.object(main, "_authenticate", _fake):
            r = _client.post("/admin/usage-export", json={}, headers={"Authorization": "Bearer x"})
        assert r.status_code == 403


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

    async def test_a_tenants_own_list_is_enforced_with_the_global_flag_off(self):
        # The console set CIDRs for this tenant and answered keys_changed: they were never
        # checked while ip_allowlist.enabled (the template default) was off.
        meta = {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"]}
        cfg = {"ip_allowlist": {"enabled": False, "trust_x_forwarded_for": True}}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value=cfg):
            with pytest.raises(HTTPException) as exc:
                await main._authenticate(self._req("8.8.8.8"))
            assert exc.value.status_code == 403
            user_id, _k, _m = await main._authenticate(self._req("10.1.2.3"))
        assert user_id == "acme"

    async def test_global_cidrs_need_the_global_flag(self):
        meta = {"tenant_id": "acme"}
        cfg = {"ip_allowlist": {"enabled": False, "global_cidrs": ["203.0.113.0/24"]}}
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
