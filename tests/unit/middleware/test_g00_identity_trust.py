"""G00 rate limiting keys on the AUTHENTICATED identity, never a client-chosen header.

Regression for the 2026-09-18 bypass (backlog #92): a fresh X-Team or X-User-ID per
request used to get a fresh bucket, so the per-minute/hour limits never bound; naming a
team claimed its per_team limit; and the non-atomic bucket let concurrent requests
overdraw it. X-Team is trusted only from a `gateway` key. The controls (same identity,
no headers) show the limiter itself works.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import asyncio
import pytest

from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded
from tenancy.resolver import apply_caller_identity, resolve_team, team_tag, DEFAULT_TEAM

LIMIT = 3
ATTEMPTS = 10


class _FakeRedis:
    """Stateful bucket store: `eval` ports _BUCKET_LUA (both windows, atomic)."""

    def __init__(self):
        self.store = {}

    async def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        now, cap_m, rate_m, cap_h, rate_h, ttl = (float(a) for a in args[numkeys:])

        def level(key, cap, rate):
            b = self.store.get(key)
            if not b:
                return cap
            return min(cap, float(b["tokens"]) + max(0.0, now - float(b["last_refill"])) * rate)

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


def _ctx(make_ctx, *, gateway=False, principal="acme", auth_meta=None, params=None):
    ctx = make_ctx([{"role": "user", "content": "hi"}], params=params or {})
    ctx.config["rate_limit"] = {"enabled": True,
                                "default": {"requests_per_minute": LIMIT, "requests_per_hour": 1000},
                                "per_user": {}, "per_team": {}, "per_tenant": {}, "tiers": {}}
    ctx.tenant_id = "acme"
    # Stamp identity the way the pipeline does — from _auth_* params + headers.
    ctx.params.setdefault("_auth_tenant_id", principal)
    ctx.params.setdefault("_auth_principal", principal)
    ctx.params["_auth_gateway"] = gateway
    if auth_meta:
        ctx.params.update(auth_meta)
    return ctx


async def _admit(ctxs, redis, concurrent=False):
    gate = G00RateLimit()

    async def one(c):
        try:
            await gate.process_request(c)
            return True
        except RateLimitExceeded:
            return False

    import unittest.mock as m
    with m.patch("middleware.g00_rate_limit._get_redis", return_value=redis):
        if concurrent:
            return sum(await asyncio.gather(*(one(c) for c in ctxs)))
        return sum([await one(c) for c in ctxs])


# ── resolve_team / team_tag (the rule, defined once) ─────────────────────────

def test_resolve_team_ignores_header_for_non_gateway_key():
    assert resolve_team("finance", key_is_gateway=False) == DEFAULT_TEAM
    assert resolve_team("finance", key_is_gateway=True) == "finance"
    assert resolve_team("", key_is_gateway=True) == DEFAULT_TEAM


def test_team_tag_is_key_safe_and_bounded():
    assert team_tag("finance") == "finance"          # legible when safe
    assert team_tag("a" * 10_000).startswith("h-")   # long → digest
    assert ":" not in team_tag("evil:injected")      # cannot add a Redis key segment
    assert len(team_tag("x" * 10_000)) <= 20


def test_apply_caller_identity_prefers_principal_over_user_id():
    class _C:
        params = {"_auth_gateway": True, "_auth_principal": "acme", "_auth_tenant_id": "acme"}
        user_id = "dev@acme.test"
    c = _C()
    apply_caller_identity(c, {"x-team": "finance"})
    assert c.key_principal == "acme"        # not the X-User-ID override
    assert c.is_gateway_key is True
    assert c.team == "finance"


# ── The bypass, through G00 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_control_same_identity_is_limited(make_ctx):
    admitted = await _admit([_ctx(make_ctx) for _ in range(ATTEMPTS)], _FakeRedis())
    assert admitted == LIMIT


@pytest.mark.asyncio
async def test_x_team_rotation_does_not_reset_the_limit_for_non_gateway_key(make_ctx):
    # A non-gateway key: X-Team is ignored, so every request shares one "default" bucket.
    redis = _FakeRedis()
    ctxs = []
    for i in range(ATTEMPTS):
        c = _ctx(make_ctx, gateway=False)
        apply_caller_identity(c, {"x-team": f"team-{i}"})   # attacker rotates the header
        ctxs.append(c)
    assert await _admit(ctxs, redis) == LIMIT


@pytest.mark.asyncio
async def test_x_user_id_rotation_cannot_split_the_bucket(make_ctx):
    # The principal is the key-bound id; a rotating X-User-ID override never reaches it.
    redis = _FakeRedis()
    ctxs = []
    for i in range(ATTEMPTS):
        c = _ctx(make_ctx, principal="acme")
        c.user_id = f"dev{i}@acme.test"      # override differs per request
        apply_caller_identity(c, {})
        ctxs.append(c)
    assert await _admit(ctxs, redis) == LIMIT


@pytest.mark.asyncio
async def test_gateway_key_gets_one_bucket_per_team(make_ctx):
    # Each distinct team is its own bucket ON PURPOSE for a gateway key: N teams × LIMIT.
    redis = _FakeRedis()
    ctxs = []
    for team in ("alpha", "beta", "gamma"):
        for _ in range(ATTEMPTS):
            c = _ctx(make_ctx, gateway=True)
            apply_caller_identity(c, {"x-team": team})
            ctxs.append(c)
    assert await _admit(ctxs, redis) == LIMIT * 3


@pytest.mark.asyncio
async def test_naming_a_per_team_entry_does_not_raise_a_non_gateway_limit(make_ctx):
    redis = _FakeRedis()
    ctxs = []
    for i in range(ATTEMPTS):
        c = _ctx(make_ctx, gateway=False)
        c.config["rate_limit"]["per_team"] = {"vip": {"requests_per_minute": 1000,
                                                      "requests_per_hour": 100000}}
        apply_caller_identity(c, {"x-team": "vip"})   # tries to claim vip's limit
        ctxs.append(c)
    assert await _admit(ctxs, redis) == LIMIT


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overdraw_one_bucket(make_ctx):
    admitted = await _admit([_ctx(make_ctx) for _ in range(ATTEMPTS)],
                            _FakeRedis(), concurrent=True)
    assert admitted == LIMIT


@pytest.mark.asyncio
async def test_bucket_key_is_identical_to_a_headerless_caller(make_ctx):
    """A caller sending no identity headers keeps the pre-2026-09-18 key shape, so its
    existing bucket carries over on deploy."""
    redis = _FakeRedis()
    c = _ctx(make_ctx, principal="acme")
    apply_caller_identity(c, {})
    await _admit([c], redis)
    assert any(k == "tok_opt:rate_limit:minute:acme:acme:default" for k in redis.store)
