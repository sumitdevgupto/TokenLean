"""Postgres key store: a key change on one instance reaches the other instances at once.

Each instance validates keys from an in-process cache that was refreshed only by a full
reload every PROXY_KEYS_REFRESH_SECONDS (30 s), so a key suspended or revoked on one
instance kept working on the others until then. Now every lifecycle write names its
tenant to the on_change hook, and each instance's CacheRefresher re-reads that tenant at
once. The host carries the announcement between instances (the managed service uses Redis
pub/sub); here it is a direct call, and the refresh interval is an hour so only
announcements count.

Real Postgres only: set TEST_PG_DSN to a THROWAWAY database — the tests drop these tables.
Skipped otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio

import pytest

pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

from tests.integration.pg_key_instances import KeyStoreInstance, eventually, reset_tables  # noqa: E402


@pytest.fixture
def linked():
    asyncio.run(reset_tables())
    a = KeyStoreInstance("a", refresh_interval=3600)
    b = KeyStoreInstance("b", refresh_interval=3600)
    # The transport: each instance's announcement reaches the other's refresher.
    a.install(on_change=lambda tenant: b.call(b.refresher.request, tenant))
    b.install(on_change=lambda tenant: a.call(a.refresher.request, tenant))
    yield a, b
    a.close()
    b.close()


def test_a_suspension_on_one_instance_is_enforced_on_the_other_at_once(linked):
    a, b = linked
    raw, _, _ = a.km.create_key("X")
    assert eventually(lambda: b.validate(raw)[0])  # the new key reaches B at once too
    a.km.set_suspended("X", True)
    assert eventually(lambda: b.km.is_suspended(b.validate(raw)[2]))


def test_a_revoked_key_stops_validating_on_the_other_instance_at_once(linked):
    a, b = linked
    raw, _, _ = a.km.create_key("X")
    assert eventually(lambda: b.validate(raw)[0])
    a.km.delete_tenant_keys("X")
    assert eventually(lambda: not b.validate(raw)[0])


def test_a_rotation_retires_the_old_key_on_the_other_instance(linked):
    a, b = linked
    old, _, _ = a.km.create_key("X")
    assert eventually(lambda: b.validate(old)[0])
    new, _, _, _ = a.km.rotate_tenant_keys("X")
    assert eventually(lambda: b.validate(new)[0] and not b.validate(old)[0])


def test_an_allowlist_change_reaches_the_other_instance(linked):
    a, b = linked
    raw, _, _ = a.km.create_key("X")
    assert eventually(lambda: b.validate(raw)[0])
    a.km.set_ip_allowlist("X", ["10.0.0.0/8"])
    assert eventually(lambda: b.km.get_ip_allowlist(b.validate(raw)[2]) == ["10.0.0.0/8"])
