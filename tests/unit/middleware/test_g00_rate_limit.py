"""Unit tests for G00 — Rate Limiting (token bucket).

The bucket is consumed by ONE atomic Lua script over both windows (2026-09-18); these
tests drive a stateful fake Redis that ports the script to Python, plus the
read-modify-write fallback used when a Redis refuses EVAL. Identity-trust behaviour
(who can select a bucket / team / limit) lives in test_g00_identity_trust.py.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import time
import pytest
from redis.exceptions import ResponseError

from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded, _BUCKET_LUA


class _FakeRedis:
    """In-memory bucket store. `eval` ports _BUCKET_LUA; hgetall/hset/expire back the
    fallback path. `no_eval=True` makes eval raise ResponseError (scripting disabled)."""

    def __init__(self, seed=None, no_eval=False, fail=False):
        self.store = dict(seed or {})
        self.no_eval = no_eval
        self.fail = fail

    async def eval(self, script, numkeys, *args):
        if self.fail:
            raise ConnectionError("redis down")
        if self.no_eval or script is not _BUCKET_LUA:
            raise ResponseError("This Redis command is not allowed from script")
        keys = args[:numkeys]
        now, cap_m, rate_m, cap_h, rate_h, ttl = (float(a) for a in args[numkeys:])

        def level(key, cap, rate):
            b = self.store.get(key)
            if not b:
                return cap
            tokens, last = float(b.get("tokens", cap)), float(b.get("last_refill", now))
            return min(cap, tokens + max(0.0, now - last) * rate)

        tm, th = level(keys[0], cap_m, rate_m), level(keys[1], cap_h, rate_h)
        verdict = 0
        if tm < 1:
            verdict = 1
        elif th < 1:
            verdict = 2
        else:
            tm, th = tm - 1, th - 1
        self.store[keys[0]] = {"tokens": str(tm), "last_refill": str(now)}
        self.store[keys[1]] = {"tokens": str(th), "last_refill": str(now)}
        return verdict

    async def hgetall(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return dict(self.store.get(key, {}))

    async def hset(self, key, mapping=None, **_):
        self.store.setdefault(key, {}).update({k: str(v) for k, v in (mapping or {}).items()})
        return len(mapping or {})

    async def expire(self, key, ttl):
        return True


def _ctx(make_ctx, principal="test_user", team="default", rpm=60, rph=1000, **rl):
    ctx = make_ctx([{"role": "user", "content": "hi"}])
    ctx.config["rate_limit"] = {
        "enabled": True, "default": {"requests_per_minute": rpm, "requests_per_hour": rph},
        "per_user": rl.get("per_user", {}), "per_team": rl.get("per_team", {}),
        "per_tenant": rl.get("per_tenant", {}), "tiers": rl.get("tiers", {}),
    }
    ctx.key_principal = principal
    ctx.team = team
    ctx.is_gateway_key = rl.get("is_gateway_key", False)
    return ctx


def _patch(monkeypatch, redis):
    monkeypatch.setattr("middleware.g00_rate_limit._get_redis", lambda: redis)


@pytest.mark.asyncio
class TestG00RateLimit:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = _ctx(make_ctx)
        ctx.config["rate_limit"]["enabled"] = False
        ctx = await G00RateLimit().process_request(ctx)
        assert ctx.user_id == "test_user"

    async def test_enabled_default_limits_admits(self, make_ctx, monkeypatch):
        _patch(monkeypatch, _FakeRedis())
        ctx = await G00RateLimit().process_request(_ctx(make_ctx))
        assert ctx.user_id == "test_user"

    async def test_rate_limit_exceeded_minute(self, make_ctx, monkeypatch):
        now = time.time()
        redis = _FakeRedis({f"tok_opt:rate_limit:minute:default:test_user:default":
                            {"tokens": "0", "last_refill": str(now)}})
        _patch(monkeypatch, redis)
        with pytest.raises(RateLimitExceeded) as ei:
            await G00RateLimit().process_request(_ctx(make_ctx))
        assert ei.value.limit_type == "requests_per_minute"
        assert ei.value.retry_after == 60

    async def test_rate_limit_exceeded_hour(self, make_ctx, monkeypatch):
        now = time.time()
        # minute window has tokens, hour window is empty → hour verdict
        redis = _FakeRedis({
            "tok_opt:rate_limit:minute:default:test_user:default": {"tokens": "10", "last_refill": str(now)},
            "tok_opt:rate_limit:hour:default:test_user:default": {"tokens": "0", "last_refill": str(now)},
        })
        _patch(monkeypatch, redis)
        with pytest.raises(RateLimitExceeded) as ei:
            await G00RateLimit().process_request(_ctx(make_ctx))
        assert ei.value.limit_type == "requests_per_hour"
        assert ei.value.retry_after == 3600

    async def test_refused_request_consumes_nothing(self, make_ctx, monkeypatch):
        """A minute-exhausted bucket must not also drain the hour bucket."""
        now = time.time()
        redis = _FakeRedis({
            "tok_opt:rate_limit:minute:default:test_user:default": {"tokens": "0", "last_refill": str(now)},
            "tok_opt:rate_limit:hour:default:test_user:default": {"tokens": "500", "last_refill": str(now)},
        })
        _patch(monkeypatch, redis)
        with pytest.raises(RateLimitExceeded):
            await G00RateLimit().process_request(_ctx(make_ctx))
        assert float(redis.store["tok_opt:rate_limit:hour:default:test_user:default"]["tokens"]) == 500.0

    async def test_per_user_override_admits_beyond_default(self, make_ctx, monkeypatch):
        _patch(monkeypatch, _FakeRedis())
        ctx = _ctx(make_ctx, rpm=1, per_user={"test_user": {"requests_per_minute": 120,
                                                            "requests_per_hour": 2000}})
        # default rpm=1, but the per-user override (120) must apply → admitted twice
        await G00RateLimit().process_request(ctx)
        await G00RateLimit().process_request(_ctx(make_ctx, rpm=1,
                                                  per_user={"test_user": {"requests_per_minute": 120,
                                                                          "requests_per_hour": 2000}}))

    async def test_per_team_applies_for_gateway_team(self, make_ctx, monkeypatch):
        _patch(monkeypatch, _FakeRedis())
        ctx = _ctx(make_ctx, principal="acme", team="premium-team", is_gateway_key=True,
                   per_team={"premium-team": {"requests_per_minute": 300, "requests_per_hour": 5000}})
        ctx = await G00RateLimit().process_request(ctx)
        assert ctx.user_id == "test_user"

    async def test_fail_open_on_redis_error(self, make_ctx, monkeypatch):
        _patch(monkeypatch, _FakeRedis(fail=True))
        ctx = await G00RateLimit().process_request(_ctx(make_ctx))
        assert ctx.user_id == "test_user"  # allowed despite the error

    async def test_eval_refused_falls_back_to_read_modify_write(self, make_ctx, monkeypatch):
        """A Redis with scripting disabled degrades to the per-window path, still enforcing."""
        now = time.time()
        redis = _FakeRedis({"tok_opt:rate_limit:minute:default:test_user:default":
                            {"tokens": "0", "last_refill": str(now)}}, no_eval=True)
        _patch(monkeypatch, redis)
        with pytest.raises(RateLimitExceeded) as ei:
            await G00RateLimit().process_request(_ctx(make_ctx))
        assert ei.value.limit_type == "requests_per_minute"

    async def test_token_bucket_refill(self, make_ctx, monkeypatch):
        now = time.time()
        redis = _FakeRedis({"tok_opt:rate_limit:minute:default:test_user:default":
                            {"tokens": "0.5", "last_refill": str(now - 10)}})  # 10s refill
        _patch(monkeypatch, redis)
        ctx = await G00RateLimit().process_request(_ctx(make_ctx))
        assert ctx.user_id == "test_user"
