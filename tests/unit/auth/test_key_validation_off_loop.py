"""Proxy-key validation never makes the event loop wait on Secret Manager.

On the blob backends a lookup could reload the key cache (cold, past its TTL, or a throttled
reload on a miss) with a synchronous Secret Manager call made from the async request path:
the whole worker stalled for the RPC, for its full timeout during an outage, and a stream of
random keys forced one every 5 s. A lookup that may reload now runs in a worker thread,
concurrent reloads share one fetch, and a failed reload is not retried for the throttle
interval (the stale cache answers meanwhile). An installed backend (Postgres) is validated on
the loop as before: its load never blocks there and its refresher keeps the cache warm.
"""
import asyncio
import hashlib
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from auth import api_key_manager as akm

KEY = "tok-test-key"
HASH = hashlib.sha256(KEY.encode()).hexdigest()
STORE = json.dumps({HASH: {"tenant_id": "acme"}})


class _Fetches:
    """A stand-in _fetch_secret that records, per call, whether it ran on a thread with a
    running event loop, and can be slow or fail."""

    def __init__(self, payload=STORE, delay=0.0, fail=False):
        self.payload, self.delay, self.fail = payload, delay, fail
        self.on_loop = []

    def __call__(self, name):
        try:
            asyncio.get_running_loop()
            self.on_loop.append(True)
        except RuntimeError:
            self.on_loop.append(False)
        if self.delay:
            time.sleep(self.delay)
        return None if self.fail else self.payload


class _Clock:
    """A strictly increasing stand-in for time.monotonic (Windows' ticks every ~15 ms, so
    two real readings in a row can be equal and hide an ordering bug)."""

    def __init__(self, start=1_000.0):
        self._t, self._lock = start, threading.Lock()

    def __call__(self):
        with self._lock:
            self._t += 0.001
            return self._t


@pytest.fixture(autouse=True)
def _blob_backend(monkeypatch):
    monkeypatch.setattr(akm, "time", SimpleNamespace(monotonic=_Clock()))
    akm.reset_key_store_backend()
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", "")
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    akm.replace_cache({})
    akm._CACHE_LOADED_AT = 0.0
    akm._last_forced_reload = 0.0
    akm._last_load_attempt = 0.0
    yield
    akm.reset_key_store_backend()


async def test_a_cold_cache_is_loaded_off_the_loop():
    fetch = _Fetches()
    with patch.object(akm, "_fetch_secret", fetch):
        valid, tenant, _ = await main._validate_key(KEY)
    assert (valid, tenant) == (True, "acme")
    assert fetch.on_loop == [False]


def _stale(store):
    akm.replace_cache(store)
    akm._CACHE_LOADED_AT = akm.time.monotonic() - akm._CACHE_TTL_SECONDS - 1


async def test_a_stale_cache_is_reloaded_off_the_loop():
    _stale({HASH: {"tenant_id": "acme"}})
    fetch = _Fetches()
    with patch.object(akm, "_fetch_secret", fetch):
        assert (await main._validate_key(KEY))[0] is True
    assert fetch.on_loop == [False]


async def test_a_miss_reloads_off_the_loop_and_finds_a_new_key():
    akm.replace_cache({"someone-else": "u"})
    fetch = _Fetches()
    with patch.object(akm, "_fetch_secret", fetch):
        assert (await main._validate_key(KEY))[:2] == (True, "acme")
    assert fetch.on_loop == [False]


async def test_a_cached_fresh_key_needs_no_thread():
    akm.replace_cache({HASH: {"tenant_id": "acme"}})
    with patch("main.asyncio.to_thread", side_effect=AssertionError("thread hop")):
        assert (await main._validate_key(KEY))[:2] == (True, "acme")


async def test_concurrent_misses_share_one_fetch():
    akm.replace_cache({"someone-else": "u"})
    fetch = _Fetches(delay=0.05)
    with patch.object(akm, "_fetch_secret", fetch):
        results = await asyncio.gather(*(main._validate_key(KEY) for _ in range(5)))
    assert all(r[0] for r in results)
    assert len(fetch.on_loop) == 1


async def test_a_failed_reload_is_not_retried_within_the_interval():
    # Secret Manager down, cache past its TTL: one attempt, then the stale cache answers.
    _stale({HASH: {"tenant_id": "acme"}})
    fetch = _Fetches(fail=True)
    with patch.object(akm, "_fetch_secret", fetch):
        for _ in range(3):
            assert (await main._validate_key(KEY))[:2] == (True, "acme")
    assert len(fetch.on_loop) == 1


async def test_concurrent_lookups_on_a_stale_cache_share_one_fetch():
    _stale({HASH: {"tenant_id": "acme"}})
    fetch = _Fetches(delay=0.05)
    with patch.object(akm, "_fetch_secret", fetch):
        results = await asyncio.gather(*(main._validate_key(KEY) for _ in range(5)))
    assert all(r[0] for r in results)
    assert len(fetch.on_loop) == 1


async def test_concurrent_lookups_on_a_cold_cache_share_one_fetch():
    # A lookup that starts while the first load is still in flight waits for it and uses
    # it, rather than loading again once the lock is free.
    fetch = _Fetches(delay=0.05)
    with patch.object(akm, "_fetch_secret", fetch):
        results = await asyncio.gather(*(main._validate_key(KEY) for _ in range(5)))
    assert all(r[0] for r in results)
    assert len(fetch.on_loop) == 1


def test_a_reload_attempted_after_a_lookup_began_is_not_repeated_for_it():
    # It may have failed (the store is down): retrying it back to back would make every
    # queued lookup wait out another timeout.
    akm.replace_cache({})
    akm._last_load_attempt = akm.time.monotonic()          # after the lookup below began
    fetch = _Fetches()
    with patch.object(akm, "_fetch_secret", fetch):
        akm._reload(akm._last_load_attempt - 1, lambda: True)
    assert fetch.on_loop == []


async def test_misses_reload_at_most_once_per_interval():
    # A stream of random keys forced a Secret Manager read every time the throttle allowed.
    akm.replace_cache({"someone-else": "u"})
    fetch = _Fetches(payload=json.dumps({"someone-else": "u"}))
    with patch.object(akm, "_fetch_secret", fetch):
        for i in range(3):
            assert (await main._validate_key(f"random-{i}"))[0] is False
    assert len(fetch.on_loop) == 1


async def test_an_installed_backend_is_validated_on_the_loop():
    loads = []

    def load_fn():
        loads.append(threading.current_thread() is threading.main_thread())
        return None                         # the PG backend's answer on the loop thread

    akm.install_key_store_backend(load_fn, lambda store: None, name="test")
    akm.replace_cache({"someone-else": "u"})
    with patch("main.asyncio.to_thread", side_effect=AssertionError("thread hop")):
        assert (await main._validate_key(KEY))[0] is False
    assert loads and all(loads)


async def test_authenticate_uses_the_off_loop_path():
    fetch = _Fetches()
    request = SimpleNamespace(headers={"Authorization": f"Bearer {KEY}"}, query_params={},
                              state=SimpleNamespace(), client=SimpleNamespace(host="203.0.113.9"))
    with patch.object(akm, "_fetch_secret", fetch):
        user_id, _, meta = await main._authenticate(request)
    assert user_id == "acme" and fetch.on_loop == [False]
