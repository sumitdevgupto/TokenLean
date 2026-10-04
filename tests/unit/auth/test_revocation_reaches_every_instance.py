"""A revoked or suspended key stops working on every instance within seconds, on the blob stores.

The local key file and Secret Manager are read into each instance's cache, which was trusted
for KEY_CACHE_TTL_SECONDS (300): a revoke or suspend made through another instance (or the
other uvicorn worker, or scripts/issue-key.sh) left the key working everywhere else for up to
five minutes. Each instance now asks the store whether it changed, at most once per interval
and only while it serves lookups: the key file by its identity (a stat, every second), Secret
Manager by the name of the secret's latest version (a metadata call, every 5 s), and reloads
when it did. An unchanged store is not read again, and a lookup that has to ask goes to a
worker thread, never the event loop. The Postgres store keeps its own refresher.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from auth import api_key_manager as akm

KEY = "tok-revoked-elsewhere"
HASH = hashlib.sha256(KEY.encode()).hexdigest()
ACTIVE = {HASH: {"tenant_id": "acme"}}
REVOKED = {"another-key-hash": {"tenant_id": "beta"}}
SUSPENDED = {HASH: {"tenant_id": "acme", "suspended": True}}


class _Clock:
    """time.monotonic, moved only by the test."""

    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(akm, "time", SimpleNamespace(monotonic=clock))
    akm.reset_key_store_backend()
    akm.replace_cache({})
    akm._CACHE_LOADED_AT = akm._last_forced_reload = akm._last_load_attempt = 0.0
    akm._last_version_check, akm._cache_version = 0.0, None
    yield clock
    akm.reset_key_store_backend()


def _write(path, store):
    """Write the store as another instance does (temp file, then os.replace)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "local-keys.json"
    _write(path, ACTIVE)
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", str(path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    return path


class _SecretManager:
    """Stands in for Secret Manager's client: the keys secret's versions, newest last."""

    def __init__(self, store):
        self.versions = [json.dumps(store)]
        self.gets = []              # per metadata call: whether an event loop was running
        self.accesses = 0
        self.fail = False
        self.delay = 0.0            # seconds each metadata call takes
        self.on_access = None       # runs once, right after the next payload is read

    def add(self, store):
        self.versions.append(json.dumps(store))

    def client(self, *args, **kwargs):
        return self

    def get_secret_version(self, request):
        try:
            asyncio.get_running_loop()
            self.gets.append(True)
        except RuntimeError:
            self.gets.append(False)
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("403 Permission 'secretmanager.versions.get' denied")
        assert request["name"].endswith("/secrets/token-proxy-api-keys/versions/latest")
        return SimpleNamespace(name=f"projects/p/secrets/token-proxy-api-keys/versions/"
                                    f"{len(self.versions)}")

    def access_secret_version(self, request):
        self.accesses += 1
        payload = self.versions[-1]
        if self.on_access:
            self.on_access()
            self.on_access = None
        return SimpleNamespace(payload=SimpleNamespace(data=payload.encode("utf-8")))


@pytest.fixture
def secret_manager(monkeypatch):
    secretmanager = pytest.importorskip("google.cloud.secretmanager")
    sm = _SecretManager(ACTIVE)
    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", sm.client)
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", "")
    monkeypatch.setattr(akm, "_PROXY_KEYS_SECRET", "token-proxy-api-keys")
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    return sm


def _refused():
    """Whether the lookup refuses the key: gone, or suspended (main answers 403 for that)."""
    valid, _, meta = akm.validate_proxy_key(KEY)
    return not valid or akm.is_suspended(meta)


@pytest.mark.parametrize("change", [REVOKED, SUSPENDED], ids=["revoked", "suspended"])
def test_a_change_to_the_key_file_applies_here_within_a_second(clock, key_file, change):
    assert akm.validate_proxy_key(KEY)[0] is True
    _write(key_file, change)                      # another instance or worker writes the file
    clock.t += 0.5
    assert not _refused()                         # not asked yet: the cache answers
    clock.t += 0.6                                # 1.1 s on: far inside the 300 s TTL
    assert _refused()


def test_an_unchanged_key_file_is_not_read_again(clock, key_file, monkeypatch):
    loads = []
    load = akm._load_key_cache
    monkeypatch.setattr(akm, "_load_key_cache", lambda: (loads.append(1), load())[1])
    assert akm.validate_proxy_key(KEY)[0] is True
    for _ in range(5):
        clock.t += 1.5
        assert akm.validate_proxy_key(KEY)[0] is True
    assert len(loads) == 1


@pytest.mark.parametrize("change", [REVOKED, SUSPENDED], ids=["revoked", "suspended"])
def test_a_new_secret_version_applies_here_within_five_seconds(clock, secret_manager, change):
    assert akm.validate_proxy_key(KEY)[0] is True
    secret_manager.add(change)                    # scripts/issue-key.sh, or another instance
    clock.t += 4
    assert not _refused()
    clock.t += 1.1
    assert _refused()


async def test_an_unchanged_secret_costs_one_metadata_call_per_interval_off_the_loop(
        clock, secret_manager):
    assert (await main._validate_key(KEY))[0] is True
    assert secret_manager.accesses == 1
    for _ in range(3):
        clock.t += 5.1
        for _ in range(2):                        # the second lookup is inside the new interval
            assert (await main._validate_key(KEY))[0] is True
    assert secret_manager.accesses == 1           # the payload is not fetched again
    assert secret_manager.gets == [False] * 4     # the load's, then one per interval, in threads


async def test_lookups_waiting_on_a_check_share_its_answer(clock, secret_manager):
    assert (await main._validate_key(KEY))[0] is True
    clock.t += 5.1
    secret_manager.delay = 0.05
    results = await asyncio.gather(*(main._validate_key(KEY) for _ in range(5)))
    assert all(r[0] for r in results)
    assert len(secret_manager.gets) == 2


def test_a_check_made_after_a_lookup_began_is_used_not_repeated(clock, secret_manager):
    assert akm.validate_proxy_key(KEY)[0] is True
    gets = len(secret_manager.gets)
    clock.t += 5.1
    asked_at = clock.t
    akm._last_version_check = asked_at + 0.01     # another lookup's check, made meanwhile
    akm._check_store_version(asked_at)
    assert len(secret_manager.gets) == gets


def test_a_file_write_during_a_load_is_seen_at_the_next_check(clock, key_file, monkeypatch):
    # The write lands just after the keys were read (in place, as an editor writes): the
    # cache must not take the file's new identity for the keys it holds.
    def load_then_write(f):
        data = json.load(f)
        monkeypatch.setattr(akm, "json", json)
        key_file.write_text(json.dumps(REVOKED, indent=2), encoding="utf-8")
        return data

    monkeypatch.setattr(akm, "json", SimpleNamespace(
        load=load_then_write, loads=json.loads, dumps=json.dumps,
        JSONDecodeError=json.JSONDecodeError))
    assert akm.validate_proxy_key(KEY)[0] is True
    clock.t += 1.1
    assert akm.validate_proxy_key(KEY)[0] is False


def test_a_revoke_during_a_load_is_seen_at_the_next_check(clock, secret_manager):
    # The revoke lands just after the keys were read: the cache must not take the new
    # version's name for the keys it holds, or it would never reload them.
    secret_manager.on_access = lambda: secret_manager.add(REVOKED)
    assert akm.validate_proxy_key(KEY)[0] is True
    clock.t += 5.1
    assert akm.validate_proxy_key(KEY)[0] is False


def test_a_rewrite_of_the_same_size_and_time_is_still_seen(clock, key_file):
    # Written as another instance writes it (os.replace), the file is a new one even when its
    # size and timestamp match the old one's.
    other = ("e" if HASH.startswith("f") else "f") * 64
    assert akm.validate_proxy_key(KEY)[0] is True
    before = os.stat(key_file)
    _write(key_file, {other: {"tenant_id": "acme"}})
    os.utime(key_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(key_file)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    clock.t += 1.1
    assert akm.validate_proxy_key(KEY)[0] is False


async def test_between_checks_a_cached_key_needs_no_thread(clock, secret_manager):
    assert (await main._validate_key(KEY))[0] is True
    clock.t += 1
    with patch("main.asyncio.to_thread", side_effect=AssertionError("thread hop")):
        assert (await main._validate_key(KEY))[0] is True


def test_a_failing_check_keeps_the_cache_and_warns_once_per_outage(clock, secret_manager,
                                                                   caplog):
    def warnings():
        return [r for r in caplog.records if "version check" in r.getMessage()]

    assert akm.validate_proxy_key(KEY)[0] is True
    secret_manager.fail = True
    with caplog.at_level(logging.WARNING, logger=akm.logger.name):
        for _ in range(3):
            clock.t += 5.1
            assert akm.validate_proxy_key(KEY)[0] is True
        assert secret_manager.accesses == 1       # no reload for want of an answer
        assert len(secret_manager.gets) == 4      # asked once per interval, not per lookup
        assert [r.levelno for r in warnings()] == [logging.WARNING]
        secret_manager.fail = False               # it answers again, then fails again
        clock.t += 5.1
        assert akm.validate_proxy_key(KEY)[0] is True
        secret_manager.fail = True
        clock.t += 5.1
        assert akm.validate_proxy_key(KEY)[0] is True
        assert len(warnings()) == 2


def test_an_installed_backend_is_left_to_its_own_refresher(clock, secret_manager):
    akm.install_key_store_backend(lambda: dict(ACTIVE), lambda store: None, name="test")
    assert akm.validate_proxy_key(KEY)[0] is True
    clock.t += 60
    assert akm.validate_proxy_key(KEY)[0] is True
    assert secret_manager.gets == []
