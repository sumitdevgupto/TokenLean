"""Unit tests for the admin-access core engines: contract lifecycle + per-tenant IP
allowlist metadata mirroring, and the pure IP-allowlist checker (net/ip_allowlist.py).

Core/OSS engines (unwired) — commercial api/admin.py drives them. Absent flags mean
allowed, so legacy keys predating the feature are never locked out.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from auth import api_key_manager as akm
from net import ip_allowlist as ipa


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = tmp_path / "local-keys.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", str(store))
    monkeypatch.setattr(akm, "_KEY_CACHE", {})
    monkeypatch.setattr(akm, "_CACHE_LOADED_AT", 0.0)
    return store


# ── contract_status mirroring ────────────────────────────────────────────────

def test_absent_flag_means_active(temp_store):
    raw, _h, _m = akm.create_key("acme", tier="enterprise")
    _ok, _tid, meta = akm.validate_proxy_key(raw)
    assert akm.is_contract_inactive(meta) is False  # legacy/new key ⇒ never blocked


def test_set_contract_inactive_then_active(temp_store):
    raw, _h, _m = akm.create_key("acme", tier="enterprise")
    assert akm.set_contract_active("acme", False) == 1
    _ok, _tid, meta = akm.validate_proxy_key(raw)
    assert akm.is_contract_inactive(meta) is True         # → main.py 403
    assert akm.set_contract_active("acme", False) == 0     # idempotent
    assert akm.set_contract_active("acme", True) == 1      # re-activate
    _ok, _tid, meta2 = akm.validate_proxy_key(raw)
    assert akm.is_contract_inactive(meta2) is False


def test_contract_flag_spans_all_tenant_keys(temp_store):
    akm.create_key("acme")
    akm.create_key("acme")
    assert akm.set_contract_active("acme", False) == 2


def test_contract_flag_carries_over_rotation(temp_store):
    akm.create_key("acme", tier="enterprise")
    akm.set_contract_active("acme", False)
    raw2, _h, meta = akm.rotate_tenant_keys("acme")[:3]
    assert meta.get("contract_inactive") is True
    _ok, _tid, m = akm.validate_proxy_key(raw2)
    assert akm.is_contract_inactive(m) is True  # rotation is not a self-reactivate loophole


def test_create_key_extra_cannot_smuggle_contract_or_ip(temp_store):
    _raw, _h, meta = akm.create_key(
        "acme", extra={"contract_inactive": False, "ip_allowlist": ["0.0.0.0/0"], "owner_domain": "x.com"})
    assert "contract_inactive" not in meta and "ip_allowlist" not in meta
    assert meta.get("owner_domain") == "x.com"  # non-reserved extras still pass through


# ── ip_allowlist mirroring ───────────────────────────────────────────────────

def test_set_and_get_ip_allowlist(temp_store):
    raw, _h, _m = akm.create_key("acme")
    assert akm.set_ip_allowlist("acme", ["10.0.0.0/8", "203.0.113.0/24"]) == 1
    _ok, _tid, meta = akm.validate_proxy_key(raw)
    assert akm.get_ip_allowlist(meta) == ["10.0.0.0/8", "203.0.113.0/24"]
    # empty clears it
    assert akm.set_ip_allowlist("acme", []) == 1
    _ok, _tid, meta2 = akm.validate_proxy_key(raw)
    assert akm.get_ip_allowlist(meta2) == []


def test_ip_allowlist_carries_over_rotation(temp_store):
    akm.create_key("acme")
    akm.set_ip_allowlist("acme", ["10.0.0.0/8"])
    _raw, _h, meta = akm.rotate_tenant_keys("acme")[:3]
    assert meta.get("ip_allowlist") == ["10.0.0.0/8"]


def test_get_ip_allowlist_legacy_key_is_empty(temp_store):
    assert akm.get_ip_allowlist(None) == []
    assert akm.get_ip_allowlist("legacy-string") == []


# ── pure checker: net.ip_allowlist.ip_allowed ────────────────────────────────

@pytest.mark.parametrize("ip,g,t,expect", [
    ("1.2.3.4", [], [], True),                                   # unrestricted
    ("203.0.113.9", ["203.0.113.0/24"], [], True),              # global match
    ("10.1.2.3", [], ["10.0.0.0/8"], True),                     # tenant match
    ("8.8.8.8", ["203.0.113.0/24"], ["10.0.0.0/8"], False),     # neither
    ("2001:db8::1", [], ["2001:db8::/32"], True),               # ipv6
    ("2001:db8::1", ["203.0.113.0/24"], [], False),             # no cross-family FP
    ("garbage", ["203.0.113.0/24"], [], False),                 # bad ip, restricted → deny
    ("garbage", [], [], True),                                  # bad ip, unrestricted → allow
])
def test_ip_allowed_matrix(ip, g, t, expect):
    assert ipa.ip_allowed(ip, g, t) is expect


def test_client_ip_from_request_xff():
    class _Req:
        def __init__(self, xff=None, host="9.9.9.9"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": host})()
    assert ipa.client_ip_from_request(_Req("1.1.1.1, 2.2.2.2"), True) == "1.1.1.1"
    assert ipa.client_ip_from_request(_Req(None, "9.9.9.9"), True) == "9.9.9.9"
    assert ipa.client_ip_from_request(_Req("1.1.1.1"), False) == "9.9.9.9"  # xff not trusted


def test_invalid_cidr_never_widens_access():
    # A malformed CIDR in the list is ignored, not treated as allow-all.
    assert ipa.ip_allowed("8.8.8.8", ["not-a-cidr"], []) is False
