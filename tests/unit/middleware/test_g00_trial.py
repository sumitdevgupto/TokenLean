"""G00 free-trial gate + trial.py derivation.

The trial gate enforces N days AND M served-2xx requests (whichever first),
raising ``RateLimitExceeded(limit_type="trial_expired")`` (main maps → 402). It
fires one-shot ``trial.threshold`` / ``trial.expired`` events (PII-free), is
config-gated (default OFF) with exempt tenants, and fails open on Redis errors
(request dimension only) and malformed state.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timedelta, timezone

import pytest

import events
import trial as trial_mod
from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class _Ctx:
    def __init__(self, tenant_id="CARD-PRD-01", config=None):
        self.tenant_id = tenant_id
        self.pricing_tier = "enterprise"
        self.redis_prefix = f"t:{tenant_id}:"
        self.request_id = "req-1"
        self.config = config or {}


class _Redis:
    """Minimal async Redis: get returns the trial_used counter; set records SETNX."""
    def __init__(self, used=0, set_result=True, fail=False):
        self.used = used
        self.set_result = set_result   # True = first crossing, None = duplicate
        self.fail = fail
        self.sets = []

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return str(self.used)

    async def set(self, key, val, nx=False, ex=None):
        self.sets.append((key, val, nx, ex))
        return self.set_result


def _rl(enabled=True, warn_pcts=None, exempt=None):
    return {"trial": {
        "enabled": enabled,
        "warn_pcts": warn_pcts or [80, 90],
        "exempt_tenants": exempt or ["admin", "default"],
    }}


def _ctx(trial=None, rl=None, tenant_id="CARD-PRD-01"):
    config = {"rate_limit": rl or _rl()}
    if trial is not None:
        config["trial"] = trial
    return _Ctx(tenant_id=tenant_id, config=config)


async def _run(ctx, redis):
    await G00RateLimit()._check_trial(ctx, redis, ctx.config["rate_limit"])


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(events, "schedule_event", lambda t, e, p: calls.append((t, e, p)))
    return calls


# ── gate off / not-applicable → no-op ───────────────────────────────────────────

async def test_disabled_is_noop_even_when_expired(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(30), "days": 14, "generation": 1}
    ctx = _ctx(trial=trial, rl=_rl(enabled=False))
    await _run(ctx, _Redis())              # must NOT raise
    assert captured == []


async def test_no_trial_block_passes(captured):
    ctx = _ctx(trial=None)
    await _run(ctx, _Redis())
    assert captured == []


@pytest.mark.parametrize("status", ["none", "converted"])
async def test_ungated_statuses_pass(captured, status):
    ctx = _ctx(trial={"status": status, "started_at": _iso_days_ago(30), "days": 1})
    await _run(ctx, _Redis(used=10**9))
    assert captured == []


async def test_exempt_tenant_passes_when_expired(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(30), "days": 14, "generation": 1}
    ctx = _ctx(trial=trial, tenant_id="admin")
    await _run(ctx, _Redis())
    assert captured == []


async def test_within_both_limits_passes_no_events(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 1000, "generation": 1}
    await _run(_ctx(trial=trial), _Redis(used=10))
    assert captured == []


# ── expiry → 402 (raise) + one-shot trial.expired ───────────────────────────────

async def test_days_exhausted_raises_and_emits_once(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(15), "days": 14,
             "max_requests": 1000, "generation": 2}
    with pytest.raises(RateLimitExceeded) as ei:
        await _run(_ctx(trial=trial), _Redis(used=10))
    assert ei.value.limit_type == "trial_expired"
    assert "dimension=days" in ei.value.scope
    assert [e for _, e, _ in captured] == [events.TRIAL_EXPIRED]
    _, _, payload = captured[0]
    assert payload["reason"] == "exhausted" and payload["generation"] == 2
    # PII-free: only trial metadata.
    assert set(payload) <= {"reason", "dimension", "days", "max_requests",
                            "requests_used", "generation"}


async def test_expired_event_deduped_but_still_raises(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(15), "days": 14, "generation": 2}
    r = _Redis(set_result=None)            # SETNX returns None → already emitted
    with pytest.raises(RateLimitExceeded):
        await _run(_ctx(trial=trial), r)
    assert captured == []                  # no duplicate event
    assert r.sets and "trialexpired:2" in r.sets[0][0]


async def test_requests_exhausted_raises_dimension_requests(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 5, "generation": 1}
    with pytest.raises(RateLimitExceeded) as ei:
        await _run(_ctx(trial=trial), _Redis(used=5))
    assert "dimension=requests" in ei.value.scope


async def test_days_only_trial_enforced(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(20), "days": 14, "generation": 1}
    with pytest.raises(RateLimitExceeded):
        await _run(_ctx(trial=trial), _Redis(used=10**9))   # no max_requests → days only


async def test_requests_only_trial_enforced(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(999), "max_requests": 3,
             "generation": 1}                                # no days → requests only
    with pytest.raises(RateLimitExceeded) as ei:
        await _run(_ctx(trial=trial), _Redis(used=3))
    assert "dimension=requests" in ei.value.scope


async def test_cancelled_raises(captured):
    trial = {"status": "cancelled", "started_at": _iso_days_ago(1), "days": 14, "generation": 4}
    with pytest.raises(RateLimitExceeded) as ei:
        await _run(_ctx(trial=trial), _Redis())
    assert ei.value.limit_type == "trial_expired" and "reason=cancelled" in ei.value.scope
    assert [e for _, e, _ in captured] == [events.TRIAL_EXPIRED]
    assert captured[0][2]["reason"] == "cancelled"


# ── fail-open ────────────────────────────────────────────────────────────────────

async def test_redis_error_fails_open_on_requests_but_days_still_enforced(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(15), "days": 14,
             "max_requests": 100, "generation": 1}
    with pytest.raises(RateLimitExceeded) as ei:
        await _run(_ctx(trial=trial), _Redis(fail=True))    # get raises → requests skipped
    assert "dimension=days" in ei.value.scope               # days expiry still fires


async def test_redis_error_within_days_passes(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 100, "generation": 1}
    await _run(_ctx(trial=trial), _Redis(fail=True))        # used unknown → treated as 0
    assert captured == []


async def test_malformed_started_at_fails_open(captured):
    trial = {"status": "active", "started_at": "not-a-date", "days": 14,
             "max_requests": 100, "generation": 1}
    await _run(_ctx(trial=trial), _Redis(used=1))           # must NOT raise
    assert captured == []


async def test_malformed_started_at_still_blocks_on_exhausted_requests(captured):
    # Days fails open (no timestamp to compute elapsed), but a request-limited trial
    # must still 402 once its counter is exhausted — a corrupted started_at must not
    # grant unlimited requests.
    trial = {"status": "active", "started_at": "not-a-date", "max_requests": 5,
             "generation": 1}
    with pytest.raises(RateLimitExceeded) as ei:
        await _run(_ctx(trial=trial), _Redis(used=5))
    assert ei.value.limit_type == "trial_expired" and "dimension=requests" in ei.value.scope


# ── warnings (one-shot per generation) ──────────────────────────────────────────

async def test_warn80_fires_once_with_generation_key(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 100, "generation": 7}
    r = _Redis(used=85)                                     # pct_requests = 85 → warn80 only
    await _run(_ctx(trial=trial), r)
    fired = [(e, p["pct"]) for _, e, p in captured]
    assert fired == [(events.TRIAL_THRESHOLD, 80)]
    assert any("trialwarn:7:80" in k for k, *_ in r.sets)   # de-dupe key carries generation


async def test_warn90_crossing_fires_both_thresholds(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 100, "generation": 1}
    await _run(_ctx(trial=trial), _Redis(used=95))          # crosses 80 AND 90 at once
    pcts = sorted(p["pct"] for _, _, p in captured)
    assert pcts == [80, 90]
    assert all(e == events.TRIAL_THRESHOLD for _, e, _ in captured)


async def test_warn_deduped_when_already_warned(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 100, "generation": 1}
    await _run(_ctx(trial=trial), _Redis(used=85, set_result=None))  # SETNX → dup
    assert captured == []


async def test_no_warning_below_threshold(captured):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14,
             "max_requests": 100, "generation": 1}
    await _run(_ctx(trial=trial), _Redis(used=50))
    assert captured == []


# ── trial.py pure derivation ────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def test_summary_none_and_converted_are_ungated():
    assert trial_mod.trial_summary(None, 0, True, _now())["status"] == "none"
    conv = trial_mod.trial_summary({"status": "converted"}, 0, True, _now())
    assert conv["status"] == "converted" and "pct_used" not in conv


def test_summary_active_within_limits():
    trial = {"status": "active", "started_at": _iso_days_ago(7), "days": 14,
             "max_requests": 100, "generation": 1}
    s = trial_mod.trial_summary(trial, 40, True, _now())
    assert s["status"] == "active" and s["warn_level"] == "none"
    assert s["requests_remaining"] == 60
    assert 40 <= s["pct_used"] <= 60            # ~50% days, 40% requests → days drives
    assert s["dimension"] == "days"


def test_summary_derives_expired_from_active():
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14, "max_requests": 10}
    s = trial_mod.trial_summary(trial, 10, True, _now())
    assert s["status"] == "expired" and s["warn_level"] == "expired"
    assert s["dimension"] == "requests" and s["requests_remaining"] == 0


@pytest.mark.parametrize("used,band", [(85, "warn80"), (95, "warn90")])
def test_summary_warning_bands(used, band):
    trial = {"status": "active", "started_at": _iso_days_ago(1), "days": 14, "max_requests": 100}
    assert trial_mod.trial_summary(trial, used, True, _now())["warn_level"] == band


def test_summary_malformed_started_at_is_invalid():
    trial = {"status": "active", "started_at": "nope", "days": 14}
    s = trial_mod.trial_summary(trial, 0, True, _now())
    assert s["valid"] is False and s["status"] == "active"


def test_summary_malformed_started_at_still_enforces_request_dimension():
    # A malformed timestamp fails open on DAYS (no way to compute elapsed time), but
    # the REQUEST dimension needs no timestamp and must keep enforcing — else a
    # corrupted started_at would grant a request-limited trial unlimited requests.
    trial = {"status": "active", "started_at": "not-a-date", "max_requests": 10}
    s = trial_mod.trial_summary(trial, 10, True, _now())
    assert s["valid"] is False
    assert s["status"] == "expired" and s["dimension"] == "requests"
    assert s["requests_remaining"] == 0


def test_summary_malformed_started_at_below_request_limit_stays_active():
    trial = {"status": "active", "started_at": "not-a-date", "max_requests": 10}
    s = trial_mod.trial_summary(trial, 3, True, _now())
    assert s["valid"] is False and s["status"] == "active"
    assert s["requests_remaining"] == 7


# ── main._bump_trial_counter (counting basis + no TTL) ──────────────────────────

class _CounterRedis:
    def __init__(self):
        self.incrs = []
        self.expires = []

    async def incr(self, key):
        self.incrs.append(key)
        return 1

    async def expire(self, key, ttl):
        self.expires.append((key, ttl))


@pytest.mark.asyncio
async def test_bump_trial_counter_only_when_active(monkeypatch):
    import asyncio
    import main
    from types import SimpleNamespace
    fake = _CounterRedis()
    monkeypatch.setattr("cache.redis_pool.get_redis", lambda: fake)

    active = SimpleNamespace(tenant_id="t1", redis_prefix="t:t1:",
                             config={"trial": {"status": "active"}})
    main._bump_trial_counter(active)
    await asyncio.sleep(0)
    assert fake.incrs == ["t:t1:trial_used"]
    assert fake.expires == []                    # NO TTL — trial spans arbitrary time


@pytest.mark.asyncio
async def test_bump_trial_counter_skipped_when_not_active(monkeypatch):
    import asyncio
    import main
    from types import SimpleNamespace
    fake = _CounterRedis()
    monkeypatch.setattr("cache.redis_pool.get_redis", lambda: fake)

    for status in ("converted", "cancelled", None):
        cfg = {"trial": {"status": status}} if status else {}
        main._bump_trial_counter(SimpleNamespace(tenant_id="t1", redis_prefix="t:t1:", config=cfg))
    await asyncio.sleep(0)
    assert fake.incrs == []
