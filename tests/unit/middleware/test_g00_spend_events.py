"""G00 spend-cap outbound events (item #4): spend_cap.reached + one-shot budget.threshold."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

import events
from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded


class _Ctx:
    def __init__(self, tenant_id="CARD-PRD-01", tier="free", config=None):
        self.tenant_id = tenant_id
        self.pricing_tier = tier
        self.redis_prefix = f"t:{tenant_id}:"
        self.request_id = "req-1"
        self.config = config or {}


class _Redis:
    def __init__(self, spent="0", set_result=True, fail=False):
        self.spent = spent
        self.set_result = set_result   # what SET NX returns (True=first crossing, None=dup)
        self.fail = fail
        self.sets = []

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.spent

    async def set(self, key, val, nx=False, ex=None):
        self.sets.append((key, val, nx, ex))
        return self.set_result


def _cfg(cap=None, warn=0, grace=0, enabled=True, exempt=None):
    conf = {
        "rate_limit": {"spend_cap": {
            "enabled": enabled, "grace_pct": grace, "warn_pct": warn,
            "exempt_tenants": exempt or ["admin", "default"],
        }},
        "billing": {"rate_card": {"free": {}}},
    }
    if cap is not None:
        conf["limits"] = {"monthly_spend_cap_usd": cap}
    return conf


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(events, "schedule_event", lambda t, e, p: calls.append((t, e, p)))
    return calls


async def test_spend_cap_block_emits_reached_event(captured):
    ctx = _Ctx(config=_cfg(cap=10, warn=0))
    with pytest.raises(RateLimitExceeded) as ei:
        await G00RateLimit()._check_spend(ctx, _Redis(spent="10"), ctx.config["rate_limit"])
    assert ei.value.limit_type == "spend_cap_exceeded"
    assert [e for _, e, _ in captured] == [events.SPEND_CAP_REACHED]
    _, _, payload = captured[0]
    assert payload["cap_usd"] == 10 and payload["spent_usd"] == 10
    # PII-free — no content, only cap/spend/tier.
    assert set(payload) <= {"cap_usd", "spent_usd", "tier"}


async def test_budget_threshold_emits_once_on_crossing(captured):
    ctx = _Ctx(config=_cfg(cap=100, warn=80))
    r = _Redis(spent="85", set_result=True)     # first crossing → SET NX succeeds
    await G00RateLimit()._check_spend(ctx, r, ctx.config["rate_limit"])
    assert [e for _, e, _ in captured] == [events.BUDGET_THRESHOLD]
    assert r.sets and r.sets[0][2] is True       # used SET NX for the dedup


async def test_budget_threshold_deduped_when_already_warned(captured):
    ctx = _Ctx(config=_cfg(cap=100, warn=80))
    r = _Redis(spent="90", set_result=None)      # SET NX returns None → already warned
    await G00RateLimit()._check_spend(ctx, r, ctx.config["rate_limit"])
    assert captured == []


async def test_no_budget_event_below_threshold(captured):
    ctx = _Ctx(config=_cfg(cap=100, warn=80))
    await G00RateLimit()._check_spend(ctx, _Redis(spent="50"), ctx.config["rate_limit"])
    assert captured == []


async def test_no_budget_event_when_warn_pct_zero(captured):
    ctx = _Ctx(config=_cfg(cap=100, warn=0))
    await G00RateLimit()._check_spend(ctx, _Redis(spent="95"), ctx.config["rate_limit"])
    assert captured == []                         # warn disabled → no event (still under cap)


async def test_exempt_tenant_emits_nothing(captured):
    ctx = _Ctx(tenant_id="admin", config=_cfg(cap=1, warn=50))
    await G00RateLimit()._check_spend(ctx, _Redis(spent="999"), ctx.config["rate_limit"])
    assert captured == []
