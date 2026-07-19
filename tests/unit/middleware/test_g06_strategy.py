"""G06 routing strategies (item #5) — pick WITHIN a tier's candidate list.

Default `priority` == models[0] (baseline byte-identical). All strategies are
deterministic (request-id hash / per-worker counter / latency EWMA), so these are pure
unit tests with no infra.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

import middleware.g06_routing as g6


class _Ctx:
    def __init__(self, request_id="r1"):
        self.request_id = request_id


@pytest.fixture(autouse=True)
def _reset_strategy_state():
    g6._RR_COUNTERS.clear()
    g6._MODEL_LATENCY_EWMA.clear()
    yield
    g6._RR_COUNTERS.clear()
    g6._MODEL_LATENCY_EWMA.clear()


TIER = ["gpt-4o-mini", "gpt-4o", "gpt-4-5"]


def test_default_priority_is_first_model():
    assert g6._select_from_tier(TIER, {}, _Ctx()) == "gpt-4o-mini"
    assert g6._select_from_tier(TIER, {"strategy": "priority"}, _Ctx()) == "gpt-4o-mini"


def test_cascade_strategy_alias_of_priority():
    assert g6._select_from_tier(TIER, {"strategy": "cascade"}, _Ctx()) == "gpt-4o-mini"


def test_unknown_strategy_falls_back_to_priority():
    assert g6._select_from_tier(TIER, {"strategy": "nonsense"}, _Ctx()) == "gpt-4o-mini"


def test_empty_and_single_model_tiers():
    assert g6._select_from_tier([], {"strategy": "canary", "canary_pct": 100}, _Ctx()) is None
    # A single-model tier always returns that model, regardless of strategy.
    assert g6._select_from_tier(["solo"], {"strategy": "canary", "canary_pct": 100}, _Ctx()) == "solo"


def test_round_robin_rotates_and_wraps():
    cfg = {"strategy": "round_robin"}
    picks = [g6._select_from_tier(TIER, cfg, _Ctx(), "simple") for _ in range(4)]
    assert picks == ["gpt-4o-mini", "gpt-4o", "gpt-4-5", "gpt-4o-mini"]


def test_round_robin_counters_are_per_tier():
    cfg = {"strategy": "round_robin"}
    a1 = g6._select_from_tier(["a1", "a2"], cfg, _Ctx(), "simple")
    b1 = g6._select_from_tier(["b1", "b2"], cfg, _Ctx(), "medium")
    a2 = g6._select_from_tier(["a1", "a2"], cfg, _Ctx(), "simple")
    assert a1 == "a1" and b1 == "b1" and a2 == "a2"   # independent counters


def test_canary_zero_pct_stays_on_incumbent():
    assert g6._select_from_tier(TIER, {"strategy": "canary", "canary_pct": 0}, _Ctx()) == "gpt-4o-mini"


def test_canary_full_pct_goes_to_candidate():
    assert g6._select_from_tier(TIER, {"strategy": "canary", "canary_pct": 100}, _Ctx()) == "gpt-4o"


def test_canary_split_is_deterministic_per_request_id():
    cfg = {"strategy": "canary", "canary_pct": 50}
    # Same request id → same decision every time.
    a = g6._select_from_tier(TIER, cfg, _Ctx("abc"))
    b = g6._select_from_tier(TIER, cfg, _Ctx("abc"))
    assert a == b
    # Across many ids the split lands near the configured percentage.
    hits = sum(1 for i in range(400)
               if g6._select_from_tier(TIER, cfg, _Ctx(f"req-{i}")) == "gpt-4o")
    assert 140 <= hits <= 260   # ~50% with generous tolerance


def test_weighted_respects_weights():
    cfg = {"strategy": "weighted", "strategy_weights": {"gpt-4o-mini": 9, "gpt-4o": 1, "gpt-4-5": 0}}
    counts = {}
    for i in range(1000):
        m = g6._select_from_tier(TIER, cfg, _Ctx(f"r{i}"))
        counts[m] = counts.get(m, 0) + 1
    assert counts.get("gpt-4-5", 0) == 0           # weight 0 → never picked
    assert counts.get("gpt-4o-mini", 0) > counts.get("gpt-4o", 0) * 3  # ~9:1 skew
    assert counts.get("gpt-4o", 0) > 0             # candidate still gets some share


def test_weighted_zero_total_falls_back_to_first():
    cfg = {"strategy": "weighted", "strategy_weights": {"gpt-4o-mini": 0, "gpt-4o": 0, "gpt-4-5": 0}}
    assert g6._select_from_tier(TIER, cfg, _Ctx()) == "gpt-4o-mini"


def test_least_latency_picks_lowest_ewma():
    g6.record_model_latency("gpt-4o-mini", 900)
    g6.record_model_latency("gpt-4o", 120)
    g6.record_model_latency("gpt-4-5", 2000)
    assert g6._select_from_tier(TIER, {"strategy": "least_latency"}, _Ctx()) == "gpt-4o"


def test_least_latency_bootstraps_unmeasured_then_converges():
    # Nothing measured → falls back to the first model.
    assert g6._select_from_tier(TIER, {"strategy": "least_latency"}, _Ctx()) == "gpt-4o-mini"
    # Measure only the first as slow; an unmeasured model (EWMA 0) is preferred next.
    g6.record_model_latency("gpt-4o-mini", 800)
    assert g6._select_from_tier(TIER, {"strategy": "least_latency"}, _Ctx()) == "gpt-4o"


def test_record_model_latency_is_ewma_and_ignores_bad_input():
    g6.record_model_latency("m", 100)
    g6.record_model_latency("m", 200, alpha=0.5)
    assert g6._MODEL_LATENCY_EWMA["m"] == pytest.approx(150.0)
    g6.record_model_latency("m", 0)        # non-positive ignored
    g6.record_model_latency("", 500)       # blank model ignored
    assert g6._MODEL_LATENCY_EWMA["m"] == pytest.approx(150.0)
    assert "" not in g6._MODEL_LATENCY_EWMA
