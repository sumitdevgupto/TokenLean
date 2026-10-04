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
    ("::ffff:10.1.2.3", [], ["10.0.0.0/8"], True),              # IPv4-mapped IPv6 is IPv4
    ("invalid", [], ["10.0.0.0/8"], False),                     # client_ip.INVALID → deny
])
def test_ip_allowed_matrix(ip, g, t, expect):
    assert ipa.ip_allowed(ip, g, t) is expect


def test_the_client_address_is_counted_from_the_right():
    # The left-most X-Forwarded-For entry is whatever the client wrote; with one trusted
    # proxy (Cloud Run) the caller is the entry that proxy appended. Parser: test_client_ip.py.
    from net.client_ip import request_client_ip

    class _Req:
        def __init__(self, xff=None, host="9.9.9.9"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": host})()
    one_hop = {"network": {"trusted_proxy_hops": 1}}
    assert request_client_ip(_Req("1.1.1.1, 2.2.2.2"), one_hop) == "2.2.2.2"
    assert request_client_ip(_Req(None, "9.9.9.9"), one_hop) == "9.9.9.9"
    assert request_client_ip(_Req("1.1.1.1"), {"network": {"trusted_proxy_hops": 0}}) == "9.9.9.9"


def test_invalid_cidr_never_widens_access():
    # A malformed CIDR in the list is ignored, not treated as allow-all.
    assert ipa.ip_allowed("8.8.8.8", ["not-a-cidr"], []) is False


# ── the allowlist belongs to the tenant, not to one key ──────────────────────
# Enforcement reads each key's metadata, so a key issued after the CIDRs were set must carry
# them too, and what the console shows must be read from the same place.

def test_a_new_key_carries_the_tenants_allowlist(temp_store):
    akm.create_key("acme")
    akm.set_ip_allowlist("acme", ["10.0.0.0/8"])
    _raw, _h, meta = akm.create_key("acme")
    assert meta.get("ip_allowlist") == ["10.0.0.0/8"]


def test_a_tenants_first_key_is_unrestricted(temp_store):
    akm.create_key("other")
    akm.set_ip_allowlist("other", ["10.0.0.0/8"])
    _raw, _h, meta = akm.create_key("acme")
    assert "ip_allowlist" not in meta


def test_the_console_listing_shows_the_enforced_allowlist(temp_store):
    akm.create_key("acme")
    akm.set_ip_allowlist("acme", ["10.0.0.0/8"])
    akm.create_key("acme")
    akm.create_key("beta")
    rows = {t["tenant_id"]: t for t in akm.list_tenants()}
    assert rows["acme"]["ip_allowlist"] == ["10.0.0.0/8"]
    assert rows["acme"]["ip_allowlist_mixed"] is False
    assert rows["beta"]["ip_allowlist"] == [] and rows["beta"]["ip_allowlist_mixed"] is False
    assert akm.get_tenant_ip_allowlist("acme") == ["10.0.0.0/8"]
    assert akm.get_tenant_ip_allowlist("beta") == []


def test_keys_that_disagree_are_reported_as_mixed(temp_store):
    import json
    store = {"h1": {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"], "created_at": "2026-01-01"},
             "h2": {"tenant_id": "acme", "created_at": "2026-02-01"}}
    temp_store.write_text(json.dumps(store), encoding="utf-8")
    (row,) = [t for t in akm.list_tenants() if t["tenant_id"] == "acme"]
    assert row["ip_allowlist_mixed"] is True


def test_the_newest_list_is_the_tenants_wherever_it_sits_in_the_store(temp_store):
    import json
    store = {"h1": {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"], "created_at": "2026-01-01"},
             "h2": {"tenant_id": "acme", "ip_allowlist": ["192.0.2.0/24"], "created_at": "2026-03-01"},
             "h3": {"tenant_id": "acme", "ip_allowlist": ["198.51.100.0/24"], "created_at": "2026-02-01"},
             "h4": {"tenant_id": "acme", "created_at": "2026-04-01"},
             "h5": {"tenant_id": "beta", "ip_allowlist": ["203.0.113.0/24"], "created_at": "2026-05-01"}}
    temp_store.write_text(json.dumps(store), encoding="utf-8")
    (row,) = [t for t in akm.list_tenants() if t["tenant_id"] == "acme"]
    assert row["ip_allowlist"] == ["192.0.2.0/24"]
    assert akm.get_tenant_ip_allowlist("acme") == ["192.0.2.0/24"]
    _raw, _h, meta = akm.create_key("acme")
    assert meta["ip_allowlist"] == ["192.0.2.0/24"]


def test_rotation_keeps_a_restriction_the_newest_key_lacked(temp_store):
    # Keys issued before new keys inherited the list: the newest one carries none.
    import json
    store = {"h1": {"tenant_id": "acme", "ip_allowlist": ["10.0.0.0/8"], "created_at": "2026-01-01"},
             "h2": {"tenant_id": "acme", "created_at": "2026-02-01"}}
    temp_store.write_text(json.dumps(store), encoding="utf-8")
    _raw, _h, meta = akm.rotate_tenant_keys("acme")[:3]
    assert meta.get("ip_allowlist") == ["10.0.0.0/8"]
