"""The monthly spend counter the spend cap reads (G00 ``_check_spend``). One helper adds to it,
for a served request (main) and for a batched one when its answer arrives (G13), so both use
one key and one expiry."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from types import SimpleNamespace

import pytest

from middleware import g00_rate_limit
from middleware.g00_rate_limit import G00RateLimit, add_to_spend, counter_prefix


class _Redis:
    def __init__(self, total=0.0, fail=False):
        self.total = total
        self.fail = fail
        self.calls = []

    async def incrbyfloat(self, key, amount):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append(("incrbyfloat", key, amount))
        self.total += amount
        return self.total

    async def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(g00_rate_limit, "_get_redis", lambda: fake)
    return fake


async def test_the_month_s_first_cost_starts_the_counter_and_its_expiry(redis):
    await add_to_spend("t:acme:", 0.25)
    key = G00RateLimit.spend_key("t:acme:")
    assert redis.calls == [("incrbyfloat", key, 0.25), ("expire", key, 40 * 86400)]


async def test_a_later_cost_only_adds(redis):
    redis.total = 1.0
    await add_to_spend("t:acme:", 0.25)
    assert redis.calls == [("incrbyfloat", G00RateLimit.spend_key("t:acme:"), 0.25)]


@pytest.mark.parametrize("cost", [0.0, -0.5, float("nan")])
async def test_no_cost_adds_nothing(redis, cost):
    await add_to_spend("t:acme:", cost)
    assert redis.calls == []


async def test_a_redis_failure_is_not_raised(monkeypatch):
    monkeypatch.setattr(g00_rate_limit, "_get_redis", lambda: _Redis(fail=True))
    try:
        await add_to_spend("t:acme:", 0.25)
    except Exception as exc:
        pytest.fail(f"add_to_spend raised {exc!r}")


def test_the_counters_live_under_the_tenant_s_prefix():
    assert counter_prefix(SimpleNamespace(tenant_id="acme", redis_prefix="t:acme:")) == "t:acme:"
    # The default tenant has no Redis prefix; its counters are namespaced all the same.
    assert counter_prefix(SimpleNamespace(tenant_id="default", redis_prefix="")) == "t:default:"
