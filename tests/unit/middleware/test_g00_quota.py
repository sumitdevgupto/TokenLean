"""WS23 — G00 monthly quota gate + per-tenant/tier rate-limit resolution (core)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded


class _Ctx:
    def __init__(self, tenant_id="CARD-PRD-01", tier="free", config=None):
        self.tenant_id = tenant_id
        self.pricing_tier = tier
        self.redis_prefix = f"t:{tenant_id}:"
        self.request_id = "req-1"
        self.config = config or {}


class _Redis:
    def __init__(self, value=None, fail=False):
        self.value = value
        self.fail = fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.value


def _cfg(included=100, enabled=True, grace=10, exempt=None):
    return {
        "rate_limit": {"quota": {
            "enabled": enabled, "grace_pct": grace,
            "exempt_tenants": exempt or ["admin", "default"],
        }},
        "billing": {"rate_card": {"free": {"included_requests": included}}},
    }


async def test_quota_disabled_never_checks():
    ctx = _Ctx(config=_cfg(enabled=False))
    await G00RateLimit()._check_quota(ctx, _Redis(fail=True), ctx.config["rate_limit"])


async def test_quota_under_cap_passes():
    ctx = _Ctx(config=_cfg(included=100))
    await G00RateLimit()._check_quota(ctx, _Redis(value="50"), ctx.config["rate_limit"])


async def test_quota_over_cap_raises_quota_exceeded():
    ctx = _Ctx(config=_cfg(included=100, grace=10))
    with pytest.raises(RateLimitExceeded) as ei:
        # cap = 100 * 1.10 = 110 → 110 used hits the ceiling
        await G00RateLimit()._check_quota(ctx, _Redis(value="110"), ctx.config["rate_limit"])
    assert ei.value.limit_type == "quota_exceeded"
    assert "CARD-PRD-01" in ei.value.scope


async def test_quota_grace_band_allows_between_cap_and_grace():
    ctx = _Ctx(config=_cfg(included=100, grace=10))
    await G00RateLimit()._check_quota(ctx, _Redis(value="105"), ctx.config["rate_limit"])


async def test_quota_exempt_tenant_skipped():
    ctx = _Ctx(tenant_id="admin", config=_cfg(included=1))
    await G00RateLimit()._check_quota(ctx, _Redis(value="999"), ctx.config["rate_limit"])


async def test_quota_zero_included_means_unlimited():
    ctx = _Ctx(config=_cfg(included=0))
    await G00RateLimit()._check_quota(ctx, _Redis(value="999999"), ctx.config["rate_limit"])


async def test_quota_fails_open_on_redis_error():
    ctx = _Ctx(config=_cfg(included=10))
    await G00RateLimit()._check_quota(ctx, _Redis(fail=True), ctx.config["rate_limit"])


def test_limits_resolution_order():
    g = G00RateLimit()
    g._default_limits = {"requests_per_minute": 60, "requests_per_hour": 1000}
    g._per_user_limits = {"vip@a.test": {"requests_per_minute": 999}}
    g._per_team_limits = {"team-x": {"requests_per_minute": 500}}
    rl_cfg = {
        "per_tenant": {"CARD-PRD-01": {"requests_per_minute": 5}},
        "tiers": {"free": {"requests_per_minute": 30}},
    }
    # per_user beats everything
    assert g._get_limits_for_scope("vip@a.test", "team-x", "CARD-PRD-01", "free", rl_cfg)[
        "requests_per_minute"] == 999
    # per_team next
    assert g._get_limits_for_scope("u", "team-x", "CARD-PRD-01", "free", rl_cfg)[
        "requests_per_minute"] == 500
    # per_tenant next (merged over defaults)
    lim = g._get_limits_for_scope("u", "t", "CARD-PRD-01", "free", rl_cfg)
    assert lim["requests_per_minute"] == 5 and lim["requests_per_hour"] == 1000
    # tier map next
    assert g._get_limits_for_scope("u", "t", "OTHER-PRD-01", "free", rl_cfg)[
        "requests_per_minute"] == 30
    # default fallback
    assert g._get_limits_for_scope("u", "t", "OTHER-PRD-01", "enterprise", rl_cfg)[
        "requests_per_minute"] == 60


def test_quota_key_is_tenant_prefixed_and_monthly():
    key = G00RateLimit.quota_key("t:CARD-PRD-01:", now=0)  # epoch → 197001
    assert key == "t:CARD-PRD-01:quota:197001"
