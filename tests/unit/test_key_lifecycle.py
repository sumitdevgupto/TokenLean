"""WS24/WS25 — key lifecycle engine: rotate + backend seam + extra metadata (core)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

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
    akm.reset_key_store_backend()
    yield store
    akm.reset_key_store_backend()


def test_rotate_swaps_atomically(temp_store):
    old_raw, _h, _m = akm.create_key("CARD-PRD-01", "enterprise")
    assert akm.validate_proxy_key(old_raw)[0] is True

    new_raw, _kh, meta, revoked = akm.rotate_tenant_keys("CARD-PRD-01")
    assert revoked == 1 and new_raw != old_raw
    assert meta["tier"] == "enterprise"              # tier inherited
    assert akm.validate_proxy_key(old_raw)[0] is False   # old key dead
    ok, tid, _ = akm.validate_proxy_key(new_raw)
    assert ok is True and tid == "CARD-PRD-01"           # new key live
    # Exactly one key remains in the store — no zero-key or dual-key window persisted.
    assert len(json.loads(temp_store.read_text())) == 1


def test_rotate_preserves_suspension_and_extras(temp_store):
    akm.create_key("CARD-PRD-01", "free", extra={"owner_domain": "cardinal.com"})
    akm.set_suspended("CARD-PRD-01", True)
    _raw, _kh, meta, _n = akm.rotate_tenant_keys("CARD-PRD-01")
    assert meta.get("suspended") is True             # rotation is not a self-unsuspend
    assert meta.get("owner_domain") == "cardinal.com"  # WS25 allowlist survives rotation


def test_rotate_without_keys_raises(temp_store):
    with pytest.raises(ValueError):
        akm.rotate_tenant_keys("GHOST-PRD-01")


def test_create_key_extra_metadata_cannot_override_reserved(temp_store):
    _raw, _kh, meta = akm.create_key(
        "NOVA-STG-01", "free",
        extra={"owner_domain": "nova.test", "tenant_id": "EVIL", "admin": True})
    assert meta["tenant_id"] == "NOVA-STG-01"        # reserved fields win
    assert "admin" not in meta
    assert meta["owner_domain"] == "nova.test"


def test_backend_seam_load_and_persist(temp_store):
    # Installed backend replaces both read and write paths of the store.
    backing = {}

    def load_fn():
        return dict(backing)

    def persist_fn(store):
        backing.clear()
        backing.update(store)

    akm.install_key_store_backend(load_fn, persist_fn, name="test")
    raw, key_hash, _meta = akm.create_key("SHOP-STG-01", "free")
    assert key_hash in backing                        # write went to the backend
    assert akm.validate_proxy_key(raw)[0] is True     # cache refreshed from the write
    akm.reset_key_store_backend()


def test_backend_load_none_keeps_cache(temp_store):
    # load_fn returning None (event-loop-thread guard) must keep the current cache.
    raw, _kh, _m = akm.create_key("BUIL-STG-01", "free")
    akm.install_key_store_backend(lambda: None, lambda s: None, name="loopguard")
    akm._CACHE_LOADED_AT = 0.0  # force a reload attempt
    assert akm.validate_proxy_key(raw)[0] is True     # cache retained despite None load
    akm.reset_key_store_backend()
