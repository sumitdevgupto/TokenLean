"""Real Redis over TLS: the proxy's pool and the fine-tune job reach a Redis that serves only TLS
and requires a password, trusting only the CA they are given, and nothing else gets an answer.

Needs TEST_REDIS_TLS_URL (rediss://host:port/0 of a THROWAWAY Redis serving only TLS, as the
Redis VM's startup script configures it), TEST_REDIS_TLS_CA (the PEM of the CA that signed its
certificate) and TEST_REDIS_TLS_PASSWORD.
"""
import datetime
import importlib.util
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src" / "proxy"))

import pytest  # noqa: E402

URL = os.environ.get("TEST_REDIS_TLS_URL")
CA = os.environ.get("TEST_REDIS_TLS_CA")
PASSWORD = os.environ.get("TEST_REDIS_TLS_PASSWORD")

pytestmark = pytest.mark.skipif(not (URL and CA and PASSWORD),
                                reason="set TEST_REDIS_TLS_URL, TEST_REDIS_TLS_CA and "
                                       "TEST_REDIS_TLS_PASSWORD for a throwaway TLS Redis")


def _env(monkeypatch, **values):
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


async def _ping(monkeypatch, url, ca, password):
    import cache.redis_pool as redis_pool
    _env(monkeypatch, REDIS_CA_CERT=ca, REDIS_PASSWORD=password, REDIS_SOCKET_TIMEOUT="3",
         REDIS_CONNECT_TIMEOUT="3")
    monkeypatch.setattr(redis_pool, "_pool", None)
    redis_pool.init_pool(url)
    try:
        return await redis_pool.get_redis().ping()
    finally:
        await redis_pool.close_pool()


def _another_ca() -> str:
    """A CA that signed nothing this Redis serves."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "some other CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


async def test_the_proxy_reaches_it_with_the_ca_and_the_password(monkeypatch):
    assert await _ping(monkeypatch, URL, CA, PASSWORD) is True


async def test_a_certificate_the_ca_did_not_sign_is_refused(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError
    with pytest.raises(RedisConnectionError, match="(?i)certificate"):
        await _ping(monkeypatch, URL, _another_ca(), PASSWORD)


async def test_without_the_password_it_is_refused(monkeypatch):
    from redis.exceptions import AuthenticationError, ResponseError
    with pytest.raises((AuthenticationError, ResponseError)):
        await _ping(monkeypatch, URL, CA, None)


async def test_plaintext_gets_no_answer(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    with pytest.raises((RedisConnectionError, RedisTimeoutError)):
        await _ping(monkeypatch, "redis://" + URL[len("rediss://"):], None, PASSWORD)


async def test_the_finetune_job_records_its_runs_there(monkeypatch):
    _env(monkeypatch, TENANT_ID="NOVA-STG-01", DOMAIN="support", REDIS_URL=URL,
         REDIS_CA_CERT=CA, REDIS_PASSWORD=PASSWORD)
    spec = importlib.util.spec_from_file_location(
        "_ftpipe_tls_live", _ROOT / "src" / "finetune-pipeline" / "pipeline.py")
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
    pipeline.FineTunePipeline()._track_job("job-tls-live", "RUNNING", {})

    import cache.redis_pool as redis_pool
    monkeypatch.setattr(redis_pool, "_pool", None)
    redis_pool.init_pool(URL)
    try:
        client = redis_pool.get_redis()
        key = "t:NOVA-STG-01:tok_opt:finetune:job-tls-live"
        assert await client.hget(key, "status") == "RUNNING"
        await client.delete(key, "t:NOVA-STG-01:tok_opt:finetune:domain:support")
    finally:
        await redis_pool.close_pool()
