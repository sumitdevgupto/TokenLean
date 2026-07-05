"""Unit tests for the key-store lifecycle write engine (create/suspend/delete/list).

Core/OSS engine (unwired) — the commercial console (api/admin.py) is what exposes it.
Drives a real temp local-keys.json store so create→validate→suspend→delete round-trips.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest

from auth import api_key_manager as akm


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = tmp_path / "local-keys.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", str(store))
    monkeypatch.setattr(akm, "_KEY_CACHE", {})
    monkeypatch.setattr(akm, "_CACHE_LOADED_AT", 0.0)
    return store


def test_create_key_persists_hash_only_and_validates(temp_store):
    raw, key_hash, meta = akm.create_key("acme", tier="pro")
    assert raw.startswith("tok-")
    assert meta["tenant_id"] == "acme" and meta["tier"] == "pro"
    # only the hash is persisted — the raw key never touches disk
    text = temp_store.read_text()
    assert key_hash in text and raw not in text
    # new key validates immediately (write refreshes the in-process cache)
    ok, tid, _m = akm.validate_proxy_key(raw)
    assert ok and tid == "acme"


def test_create_key_requires_tenant(temp_store):
    with pytest.raises(ValueError):
        akm.create_key("   ")


def test_create_admin_key_flagged(temp_store):
    raw, _h, meta = akm.create_key("ops", tier="enterprise", admin=True)
    assert meta.get("admin") is True
    _ok, _tid, m = akm.validate_proxy_key(raw)
    assert akm.is_admin_key(m) is True


def test_create_rejects_duplicate_raw_key(temp_store):
    akm.create_key("acme", raw_key="tok-fixed-abc")
    with pytest.raises(ValueError):
        akm.create_key("acme", raw_key="tok-fixed-abc")


def test_suspend_enforced_then_lifted(temp_store):
    raw, _h, _m = akm.create_key("acme", tier="pro")
    assert akm.set_suspended("acme", True) == 1
    ok, _tid, meta = akm.validate_proxy_key(raw)
    assert ok is True                       # still authenticates...
    assert akm.is_suspended(meta) is True    # ...but flagged → main.py returns 403
    assert akm.set_suspended("acme", True) == 0    # idempotent no-op
    assert akm.set_suspended("acme", False) == 1   # lifted
    _ok, _tid, meta2 = akm.validate_proxy_key(raw)
    assert akm.is_suspended(meta2) is False


def test_delete_tenant_keys_revokes_all_and_spares_siblings(temp_store):
    raw1, _h1, _ = akm.create_key("acme")
    raw2, _h2, _ = akm.create_key("acme")
    akm.create_key("other")
    assert akm.delete_tenant_keys("acme") == 2
    assert akm.validate_proxy_key(raw1)[0] is False
    assert akm.validate_proxy_key(raw2)[0] is False
    assert any(t["tenant_id"] == "other" for t in akm.list_tenants())


def test_delete_unknown_tenant_returns_zero(temp_store):
    assert akm.delete_tenant_keys("ghost") == 0


def test_list_tenants_never_leaks_key_material(temp_store):
    akm.create_key("acme", tier="pro")
    akm.create_key("acme", tier="pro")
    akm.create_key("beta", tier="basic", admin=True)
    akm.set_suspended("beta", True)
    tenants = {t["tenant_id"]: t for t in akm.list_tenants()}
    assert set(tenants) == {"acme", "beta"}
    assert tenants["acme"]["key_count"] == 2
    assert tenants["beta"]["admin"] is True and tenants["beta"]["suspended"] is True
    # only safe aggregate fields — no hash, no raw key
    for t in tenants.values():
        assert set(t) == {"tenant_id", "tier", "admin", "suspended", "key_count", "created_at"}


def test_atomic_write_leaves_no_tmp_file(temp_store):
    akm.create_key("acme")
    assert not os.path.exists(str(temp_store) + ".tmp")
