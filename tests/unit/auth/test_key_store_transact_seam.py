"""The key store's transactional write seam (no database).

A backend that installs a transact hook (the Postgres store) must receive every lifecycle
write as ONE call it can run atomically — never the old load-then-persist pair — and the
in-process cache must reflect the result. Every successful write is then announced to the
on_change hook with its tenant, so other instances can re-read that tenant at once. Blob
backends are unaffected (their tests are in test_key_lifecycle.py and
test_contract_and_ip_allowlist.py).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import logging

import pytest

from auth import api_key_manager as akm


@pytest.fixture
def backend():
    """A fake transactional backend over an in-memory store."""
    state = {"store": {}, "transacts": 0, "announced": []}

    def load_fn():
        raise AssertionError("a lifecycle write read the store outside the transaction")

    def persist_fn(store):
        raise AssertionError("a lifecycle write persisted the store outside the transaction")

    def transact_fn(mutation):
        state["transacts"] += 1
        working = {h: dict(m) for h, m in state["store"].items()}
        changed, result = mutation(working)
        if changed:
            state["store"] = working
        return result, working

    akm.install_key_store_backend(load_fn, persist_fn, name="fake", transact_fn=transact_fn,
                                  on_change=state["announced"].append)
    yield state
    akm.reset_key_store_backend()


def test_every_lifecycle_write_is_one_transaction(backend):
    _, key_hash, _ = akm.create_key("ACME")
    assert akm.set_suspended("ACME", True) == 1
    assert akm.set_contract_active("ACME", False) == 1
    assert akm.set_ip_allowlist("ACME", ["10.0.0.0/8"]) == 1
    _, new_hash, meta, revoked = akm.rotate_tenant_keys("ACME")
    assert akm.delete_tenant_keys("ACME") == 1
    assert backend["transacts"] == 6
    assert revoked == 1 and meta.get("suspended") is True and new_hash != key_hash
    assert backend["store"] == {}


def test_the_cache_reflects_the_transaction_result(backend):
    _, key_hash, _ = akm.create_key("ACME")
    akm.set_suspended("ACME", True)
    assert akm._KEY_CACHE[key_hash].get("suspended") is True


def test_a_failing_change_propagates_and_writes_nothing(backend):
    with pytest.raises(ValueError):
        akm.rotate_tenant_keys("NOBODY")
    assert backend["store"] == {}


def test_every_lifecycle_write_announces_its_tenant(backend):
    akm.create_key(" ACME ")
    akm.set_suspended("ACME", True)
    akm.set_contract_active("ACME", False)
    akm.set_ip_allowlist("ACME", [])  # changes nothing, and is announced all the same
    akm.rotate_tenant_keys("ACME")
    akm.delete_tenant_keys("ACME")
    assert backend["announced"] == ["ACME"] * 6


def test_a_failed_write_announces_nothing(backend):
    with pytest.raises(ValueError):
        akm.rotate_tenant_keys("NOBODY")
    assert backend["announced"] == []


def test_a_failing_announcement_does_not_fail_the_write(backend, caplog):
    def unreachable(tenant_id):
        raise ConnectionError("announcement channel down")

    akm._BACKEND_ON_CHANGE = unreachable
    with caplog.at_level(logging.WARNING):
        _, key_hash, _ = akm.create_key("ACME")
    assert key_hash in backend["store"]
    assert "announcement failed tenant=ACME" in caplog.text


def test_replacing_a_tenant_in_the_cache_leaves_other_tenants_alone(monkeypatch):
    monkeypatch.setattr(akm, "_KEY_CACHE", {
        "x1": {"tenant_id": "X"}, "x2": {"tenant_id": "X"},
        "y1": {"tenant_id": "Y"}, "legacy": "someone"})
    akm.replace_tenants_in_cache({"X"}, {"x3": {"tenant_id": "X", "suspended": True}})
    assert akm._KEY_CACHE == {"x3": {"tenant_id": "X", "suspended": True},
                              "y1": {"tenant_id": "Y"}, "legacy": "someone"}


def test_reset_removes_the_transact_and_change_hooks(backend):
    akm.reset_key_store_backend()
    assert akm._BACKEND_TRANSACT is None and akm._BACKEND_ON_CHANGE is None
