"""The proxy logs in to Redis as the `default` user when a password is configured, over TLS when
it is given the CA.

Redis ran with no password and no TLS, so any host in the VPC could read and write every tenant's
keys, or read them on the wire. The password reaches the proxy as REDIS_PASSWORD (mounted from a
secret, never inside the plain REDIS_URL). Sent as the `default` user's, it is accepted by a Redis
that requires it and by one that has no password yet, so the clients get it before the server
starts to require it. The CA that signs Redis's certificate reaches it as REDIS_CA_CERT, with a
rediss:// URL.
"""
import logging

import pytest

import cache.redis_pool as redis_pool

_CA = "-----BEGIN CERTIFICATE-----\nQ0FDRVJU\n-----END CERTIFICATE-----\n"


def _pool(monkeypatch, url, password=None, ca=None):
    for name, value in (("REDIS_PASSWORD", password), ("REDIS_CA_CERT", ca)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(redis_pool, "_pool", None)   # restored afterwards
    return redis_pool.init_pool(url)


def _connection_kwargs(monkeypatch, url, password=None):
    return _pool(monkeypatch, url, password).connection_kwargs


def test_the_password_is_sent_as_the_default_user_s(monkeypatch):
    kwargs = _connection_kwargs(monkeypatch, "redis://10.0.0.3:6379/0", "s3cret")
    assert (kwargs.get("username"), kwargs.get("password")) == ("default", "s3cret")


@pytest.mark.parametrize("password", [None, ""], ids=["unset", "empty"])
def test_without_a_password_none_is_sent(monkeypatch, password):
    kwargs = _connection_kwargs(monkeypatch, "redis://localhost:6379/0", password)
    assert "password" not in kwargs and "username" not in kwargs


def test_credentials_in_the_url_win(monkeypatch):
    # An external Redis (REDIS_URL from .env, e.g. a hosted one) carries its own.
    kwargs = _connection_kwargs(monkeypatch, "rediss://app:from-url@cache.example:6380/0", "s3cret")
    assert (kwargs.get("username"), kwargs.get("password")) == ("app", "from-url")


def test_with_tls_the_proxy_trusts_only_the_ca_it_was_given(monkeypatch):
    from redis.asyncio.connection import SSLConnection
    pool = _pool(monkeypatch, "rediss://10.0.0.3:6378/0", "s3cret", _CA)
    assert issubclass(pool.connection_class, SSLConnection)
    kwargs = pool.connection_kwargs
    assert kwargs["ssl_ca_data"] == _CA and kwargs["ssl_cert_reqs"] == "required"
    # The CA is the deployment's own and signs only its Redis, whose certificate names no host
    # the client dials (Memorystore's, or the VM's), so it is the chain that is checked.
    assert kwargs["ssl_check_hostname"] is False
    assert (kwargs.get("username"), kwargs.get("password")) == ("default", "s3cret")


def test_a_ca_with_a_plaintext_url_is_reported_not_ignored(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="cache.redis_pool"):
        pool = _pool(monkeypatch, "redis://10.0.0.3:6379/0", ca=_CA)
    assert "ssl_ca_data" not in pool.connection_kwargs
    assert "REDIS_CA_CERT" in caplog.text and "not encrypted" in caplog.text


def test_a_hosted_tls_redis_keeps_the_system_trust_store(monkeypatch):
    kwargs = _connection_kwargs(monkeypatch, "rediss://cache.example:6380/0")
    assert "ssl_ca_data" not in kwargs and "ssl_cert_reqs" not in kwargs
