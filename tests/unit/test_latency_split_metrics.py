"""Proxy-vs-LLM latency split — _record_outcome feeds the SLA 'Proxy only' row.

token_opt_request_duration_ms  — end-to-end (pre-existing)
token_opt_llm_duration_ms      — provider LLM call time, observed only when a
                                 provider call actually happened
token_opt_proxy_overhead_ms    — end-to-end minus provider time; cache hits and
                                 bypasses never reach the provider so their full
                                 duration counts as proxy time
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import time
import uuid
from types import SimpleNamespace

from prometheus_client import REGISTRY

import main


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _ctx(tenant_id: str, llm_ms: float) -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, request_id="r1", llm_elapsed_ms=llm_ms)


def _tenant(prefix: str) -> str:
    # Unique per invocation: the default REGISTRY is process-global and
    # accumulates, so a fixed label would break count/sum assertions under
    # pytest-repeat / rerunfailures / same-worker re-runs.
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_llm_call_splits_duration_between_llm_and_proxy():
    tenant = _tenant("lat-split-llm")
    start_ts = time.time() - 1.0  # ~1000ms end-to-end
    main._record_outcome(_ctx(tenant, llm_ms=600.0), start_ts, "200")

    assert _sample("token_opt_llm_duration_ms_count", tenant_id=tenant) == 1
    assert _sample("token_opt_llm_duration_ms_sum", tenant_id=tenant) == 600.0
    overhead = _sample("token_opt_proxy_overhead_ms_sum", tenant_id=tenant, status="200")
    # elapsed(~1000ms+jitter) − llm(600ms) ≈ 400ms. Upper bound must stay < 1000
    # (an unsubtracted overhead would be the full ~1000ms elapsed) while
    # tolerating up to ~500ms of scheduler/GC jitter on loaded CI runners.
    assert 300.0 <= overhead < 900.0


def test_cache_hit_counts_entirely_as_proxy_time():
    tenant = _tenant("lat-split-cache")
    start_ts = time.time() - 0.5  # ~500ms, no provider call
    main._record_outcome(_ctx(tenant, llm_ms=0.0), start_ts, "200")

    # No provider call → LLM histogram untouched, full duration is proxy overhead
    assert _sample("token_opt_llm_duration_ms_count", tenant_id=tenant) == 0
    assert _sample("token_opt_proxy_overhead_ms_count", tenant_id=tenant, status="200") == 1
    overhead = _sample("token_opt_proxy_overhead_ms_sum", tenant_id=tenant, status="200")
    assert 400.0 <= overhead < 1000.0


def test_proxy_overhead_never_negative():
    tenant = _tenant("lat-split-clamp")
    # llm_ms wildly larger than elapsed (clock skew / stream edge case) → clamped to 0
    main._record_outcome(_ctx(tenant, llm_ms=10_000_000.0), time.time(), "200")

    assert _sample("token_opt_proxy_overhead_ms_count", tenant_id=tenant, status="200") == 1
    assert _sample("token_opt_proxy_overhead_ms_sum", tenant_id=tenant, status="200") == 0.0


def test_missing_ctx_defaults_to_zero_llm_time():
    # Failures before the context is built pass ctx=None — must not raise, and
    # the whole elapsed time lands in proxy overhead under the default tenant.
    # Delta-based (not absolute) because the default-tenant series is shared.
    before = _sample("token_opt_proxy_overhead_ms_count", tenant_id="default", status="500")
    main._record_outcome(None, time.time() - 0.1, "500")
    after = _sample("token_opt_proxy_overhead_ms_count", tenant_id="default", status="500")
    assert after == before + 1
