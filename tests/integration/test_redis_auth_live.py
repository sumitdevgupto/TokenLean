"""Real Redis: a client with REDIS_PASSWORD works whether or not Redis requires it yet.

That is what lets the deploy give the proxy and the fine-tune Job the password before
REDIS_AUTH_ENFORCE makes Redis require it, with no outage in between. Needs TEST_REDIS_URL naming
a THROWAWAY Redis: the test changes its password for a moment (it restores it).
"""
import os
import sys
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

TEST_REDIS = os.environ.get("TEST_REDIS_URL")


async def _ping_through_the_proxy_pool(monkeypatch, url, password):
    import cache.redis_pool as redis_pool
    monkeypatch.setenv("REDIS_PASSWORD", password)
    monkeypatch.setattr(redis_pool, "_pool", None)
    redis_pool.init_pool(url)
    try:
        return await redis_pool.get_redis().ping()
    finally:
        await redis_pool.close_pool()


@pytest.mark.skipif(not TEST_REDIS, reason="set TEST_REDIS_URL to a throwaway Redis")
async def test_a_client_with_the_password_works_before_and_after_redis_requires_it(monkeypatch):
    import redis.asyncio as aioredis
    from redis.exceptions import AuthenticationError, ResponseError

    parts = urlsplit(TEST_REDIS)
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    bare = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))   # no credentials
    # One connection throughout: it stays logged in while the password changes under it.
    admin = aioredis.Redis.from_url(TEST_REDIS, single_connection_client=True)
    original = (await admin.config_get("requirepass"))["requirepass"]
    try:
        await admin.config_set("requirepass", "")   # a Redis with no password yet
        assert await _ping_through_the_proxy_pool(monkeypatch, bare, "pw-shipped-early") is True

        await admin.config_set("requirepass", "pw-shipped-early")   # now it requires it
        assert await _ping_through_the_proxy_pool(monkeypatch, bare, "pw-shipped-early") is True
        stranger = aioredis.Redis.from_url(bare)
        with pytest.raises((AuthenticationError, ResponseError)):
            await stranger.ping()                 # NOAUTH: without the password it is refused
        await stranger.aclose()
    finally:
        await admin.config_set("requirepass", original)
        await admin.aclose()
