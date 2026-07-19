"""Unit tests for the provider resilience layer (circuit breaker, cooldown, failover).

Pure/clock-injected — no Redis, no litellm. Covers the breaker state machine, the
store's TTL + tenant-isolation semantics, and the ``call_with_resilience``
orchestration. The named "review invariant" tests each pin a bug found in the
Branch-1 code review (2026-07-10): zero-attempt blackhole, cross-tenant breaker
poisoning, fallback-error chain aborts, narrow retryable-status set, frozen
breaker config, and unsafe error reprs.
"""
import asyncio

import pytest

from providers.resilience import (
    Attempt,
    AllTargetsFailedError,
    BreakerState,
    CallTarget,
    CircuitBreaker,
    ResilienceConfig,
    ResilienceStore,
    call_with_resilience,
    describe_error,
    is_rate_limit_error,
    is_retryable_error,
    note_provider_outcome,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def _noop_sleep(_):
    return None


# ── error classification ──────────────────────────────────────────────────────

class _Err(Exception):
    def __init__(self, status=None):
        super().__init__("boom")
        if status is not None:
            self.status_code = status


@pytest.mark.parametrize("status,expected", [
    (429, True), (500, True), (502, True), (503, True), (504, True), (408, True),
    # Review invariant (C4): the whole 5xx family is retryable, incl. gateway/CDN codes.
    (501, True), (507, True), (508, True), (520, True), (522, True), (524, True),
    (400, False), (401, False), (403, False), (404, False), (422, False),
])
def test_is_retryable_by_status(status, expected):
    assert is_retryable_error(_Err(status)) is expected


@pytest.mark.parametrize("name,expected", [
    ("RateLimitError", True), ("APIConnectionError", True), ("Timeout", True),
    ("InternalServerError", True), ("ValueError", False), ("AuthenticationError", False),
])
def test_is_retryable_by_class_name(name, expected):
    exc = type(name, (Exception,), {})()
    assert is_retryable_error(exc) is expected


def test_rate_limit_classification():
    assert is_rate_limit_error(_Err(429)) is True
    assert is_rate_limit_error(_Err(503)) is False
    assert is_rate_limit_error(type("RateLimitError", (Exception,), {})()) is True


def test_describe_error_is_safe_not_repr():
    """Review invariant (S2): attempt trails must never embed raw exception reprs
    (litellm reprs can carry base_url/api-key/prompt payloads)."""
    class LeakyError(Exception):
        def __repr__(self):
            return "LeakyError(api_key='sk-SECRET', base_url='https://internal')"
    e = LeakyError()
    e.status_code = 502
    desc = describe_error(e)
    assert "SECRET" not in desc and "internal" not in desc
    assert desc == "LeakyError(status=502)"


# ── CircuitBreaker state machine ──────────────────────────────────────────────

def test_breaker_trips_open_after_threshold():
    clk = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=30, _clock=clk)
    assert cb.allow_request() and cb.state is BreakerState.CLOSED
    cb.record_failure(); cb.record_failure()
    assert cb.state is BreakerState.CLOSED  # not yet at threshold
    cb.record_failure()
    assert cb.state is BreakerState.OPEN
    assert cb.allow_request() is False       # open → requests blocked


def test_breaker_half_open_probe_then_close():
    clk = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, _clock=clk)
    cb.record_failure()
    assert cb.state is BreakerState.OPEN
    assert cb.allow_request() is False
    clk.advance(30)
    assert cb.allow_request() is True        # cooldown elapsed → one probe
    assert cb.state is BreakerState.HALF_OPEN
    cb.record_success()
    assert cb.state is BreakerState.CLOSED and cb.failures == 0


def test_breaker_half_open_probe_failure_reopens():
    clk = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, _clock=clk)
    cb.record_failure()
    clk.advance(10)
    assert cb.allow_request() is True and cb.state is BreakerState.HALF_OPEN
    cb.record_failure()                      # probe fails
    assert cb.state is BreakerState.OPEN
    assert cb.allow_request() is False       # cooldown restarted


def test_breaker_success_resets_failures():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=5)
    cb.record_failure(); cb.record_failure()
    cb.record_success()
    cb.record_failure(); cb.record_failure()
    assert cb.state is BreakerState.CLOSED   # count reset, never reached 3


def test_breaker_peek_state_reflects_elapsed_cooldown_without_mutation():
    """Review invariant (S4): metrics reads must show HALF_OPEN once cooldown has
    elapsed, and must not fabricate a probe (no state mutation)."""
    clk = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, _clock=clk)
    cb.record_failure()
    assert cb.peek_state() is BreakerState.OPEN
    clk.advance(10)
    assert cb.peek_state() is BreakerState.HALF_OPEN
    assert cb.state is BreakerState.OPEN     # unchanged — peek didn't mutate


def test_breaker_configure_refreshes_tunables():
    """Review invariant (C5): hot-reload/per-provider overrides must apply to an
    existing breaker, not freeze at first touch."""
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    cfg5 = _cfg(failure_threshold=5, cooldown_seconds=30)
    store.allow_provider("openai", cfg5)     # creates breaker at 5/30
    cfg2 = _cfg(failure_threshold=2, cooldown_seconds=99)
    store.record_provider_failure("openai", cfg2)
    state = store.record_provider_failure("openai", cfg2)
    assert state is BreakerState.OPEN        # new threshold (2) applied, not frozen 5


# ── ResilienceStore: cooldown TTL + tenant isolation ──────────────────────────

def _cfg(**kw):
    base = dict(enabled=True, num_retries=1, failure_threshold=2,
                cooldown_seconds=30, retry_base_delay=0)
    base.update(kw)
    return ResilienceConfig(**base)


def test_cooldown_expires_after_ttl():
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    store.set_cooldown("tenantA:", "openai", ttl=30)
    assert store.in_cooldown("tenantA:", "openai") is True
    clk.advance(29)
    assert store.in_cooldown("tenantA:", "openai") is True
    clk.advance(2)
    assert store.in_cooldown("tenantA:", "openai") is False


def test_cooldown_is_tenant_isolated():
    store = ResilienceStore(clock=FakeClock())
    store.set_cooldown("tenantA:", "openai", ttl=30)
    # A different tenant's prefix must NOT see tenant A's cooldown (BYOK keys differ).
    assert store.in_cooldown("tenantA:", "openai") is True
    assert store.in_cooldown("tenantB:", "openai") is False


def test_breaker_state_shared_per_provider_in_store():
    store = ResilienceStore(clock=FakeClock())
    cfg = _cfg(failure_threshold=2)
    assert store.allow_provider("openai", cfg) is True
    store.record_provider_failure("openai", cfg)
    state = store.record_provider_failure("openai", cfg)
    assert state is BreakerState.OPEN
    assert store.allow_provider("openai", cfg) is False
    # Different provider has its own independent breaker.
    assert store.allow_provider("anthropic", cfg) is True


def test_peek_provider_state_never_creates_breaker():
    store = ResilienceStore(clock=FakeClock())
    assert store.peek_provider_state("never-called") is BreakerState.CLOSED
    assert "never-called" not in store._breakers


# ── call_with_resilience orchestration ────────────────────────────────────────

def _target(provider, model, result=None, exc=None, has_key=True, calls=None):
    async def invoke():
        if calls is not None:
            calls.append((provider, model))
        if exc is not None:
            raise exc
        return result
    return CallTarget(model=model, provider=provider, invoke=invoke, has_key=has_key)


def test_single_target_success():
    store = ResilienceStore(clock=FakeClock())
    sink = []
    winner = {}
    tgt = _target("openai", "gpt-4o-mini", result={"ok": True})
    out = asyncio.run(call_with_resilience(
        [tgt], store, _cfg(), attempts_sink=sink,
        on_success=lambda t: winner.update(p=t.provider), sleep=_noop_sleep,
    ))
    assert out == {"ok": True}
    assert sink == [Attempt("openai", "gpt-4o-mini", "success")]
    assert winner["p"] == "openai"


def test_single_target_nonretryable_raises_original():
    store = ResilienceStore(clock=FakeClock())
    boom = _Err(400)
    tgt = _target("openai", "gpt-4o-mini", exc=boom)
    with pytest.raises(_Err) as ei:
        asyncio.run(call_with_resilience([tgt], store, _cfg(), sleep=_noop_sleep))
    assert ei.value is boom  # exact passthrough, no wrapping


def test_single_target_retryable_retries_then_raises_original():
    store = ResilienceStore(clock=FakeClock())
    calls = []
    boom = _Err(503)
    tgt = _target("openai", "gpt-4o-mini", exc=boom, calls=calls)
    with pytest.raises(_Err):
        asyncio.run(call_with_resilience(
            [tgt], store, _cfg(num_retries=2), sleep=_noop_sleep,
        ))
    assert len(calls) == 3  # initial + 2 retries


def test_failover_to_second_target():
    store = ResilienceStore(clock=FakeClock())
    sink, calls, winner = [], [], {}
    t1 = _target("openai", "gpt-4o-mini", exc=_Err(503), calls=calls)
    t2 = _target("anthropic", "claude-3-5-haiku", result={"ok": 2}, calls=calls)
    out = asyncio.run(call_with_resilience(
        [t1, t2], store, _cfg(num_retries=0), attempts_sink=sink,
        on_success=lambda t: winner.update(p=t.provider, m=t.model), sleep=_noop_sleep,
    ))
    assert out == {"ok": 2}
    assert [a.outcome for a in sink] == ["error", "success"]
    assert winner == {"p": "anthropic", "m": "claude-3-5-haiku"}
    # 503 is a provider-health signal: it feeds the breaker, NOT the tenant cooldown.
    assert store.in_cooldown("", "openai") is False


def test_skip_target_without_key():
    store = ResilienceStore(clock=FakeClock())
    sink = []
    t1 = _target("anthropic", "claude", has_key=False)
    t2 = _target("openai", "gpt-4o-mini", result={"ok": 1})
    out = asyncio.run(call_with_resilience(
        [t1, t2], store, _cfg(), attempts_sink=sink, sleep=_noop_sleep,
    ))
    assert out == {"ok": 1}
    assert sink[0] == Attempt("anthropic", "claude", "skipped_no_key")


def test_skip_open_breaker_target_then_failover():
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    cfg = _cfg(failure_threshold=1)
    store.record_provider_failure("openai", cfg)
    assert store.breaker_state("openai", cfg) is BreakerState.OPEN
    sink = []
    t1 = _target("openai", "gpt-4o-mini", result={"never": True})
    t2 = _target("anthropic", "claude", result={"ok": 3})
    out = asyncio.run(call_with_resilience(
        [t1, t2], store, cfg, attempts_sink=sink, sleep=_noop_sleep,
    ))
    assert out == {"ok": 3}
    assert sink[0] == Attempt("openai", "gpt-4o-mini", "skipped_breaker")


def test_all_targets_failed_raises_aggregate():
    store = ResilienceStore(clock=FakeClock())
    t1 = _target("openai", "m1", exc=_Err(503))
    t2 = _target("anthropic", "m2", exc=_Err(500))
    with pytest.raises(AllTargetsFailedError) as ei:
        asyncio.run(call_with_resilience(
            [t1, t2], store, _cfg(num_retries=0), sleep=_noop_sleep,
        ))
    assert len(ei.value.attempts) == 2
    assert ei.value.last_error is not None


def test_disabled_config_is_single_attempt_no_breaker():
    store = ResilienceStore(clock=FakeClock())
    calls = []
    cfg = ResilienceConfig(enabled=False, num_retries=3)
    tgt = _target("openai", "gpt-4o-mini", exc=_Err(503), calls=calls)
    with pytest.raises(_Err):
        asyncio.run(call_with_resilience([tgt], store, cfg, sleep=_noop_sleep))
    assert len(calls) == 1  # no retries when disabled
    assert store.breaker_state("openai", cfg) is BreakerState.CLOSED


def test_config_resolve_merges_provider_override():
    config = {
        "resilience": {"enabled": True, "num_retries": 1, "cooldown_seconds": 30},
        "providers": [
            {"name": "openai", "resilience": {"num_retries": 4, "failure_threshold": 9}},
        ],
    }
    glob = ResilienceConfig.resolve(config)
    assert glob.enabled is True and glob.num_retries == 1
    ovr = ResilienceConfig.resolve(config, provider="openai")
    assert ovr.num_retries == 4 and ovr.failure_threshold == 9
    assert ovr.cooldown_seconds == 30  # inherited from global


# ── Review invariants (Branch-1 code review, 2026-07-10) ─────────────────────

def test_fail_open_single_target_in_cooldown_is_still_attempted():
    """Review invariant (C1 blackhole): a cooldown/breaker gate must NEVER produce a
    zero-attempt failure. With one target and an active cooldown, the target is
    attempted anyway (fail-open) and its real result returned."""
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    store.set_cooldown("t:", "openai", ttl=30)
    calls, sink = [], []
    tgt = _target("openai", "gpt-4o-mini", result={"ok": "recovered"}, calls=calls)
    out = asyncio.run(call_with_resilience(
        [tgt], store, _cfg(), redis_prefix="t:", attempts_sink=sink, sleep=_noop_sleep,
    ))
    assert out == {"ok": "recovered"}
    assert len(calls) == 1               # attempted despite the cooldown
    assert sink[-1].outcome == "success"


def test_fail_open_single_target_breaker_open_returns_real_error():
    """Fail-open with a still-failing provider: the client sees the PROVIDER's real
    error (e.g. 429), never a fabricated zero-attempt 502."""
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    cfg = _cfg(failure_threshold=1)
    store.record_provider_failure("openai", cfg)   # breaker OPEN
    boom = _Err(429)
    tgt = _target("openai", "gpt-4o-mini", exc=boom)
    with pytest.raises(_Err) as ei:
        asyncio.run(call_with_resilience(
            [tgt], store, _cfg(num_retries=0, failure_threshold=1), sleep=_noop_sleep,
        ))
    assert ei.value is boom              # original 429, not AllTargetsFailedError


def test_rate_limit_does_not_feed_global_breaker():
    """Review invariant (C2 cross-tenant poisoning): a tenant's 429s set THAT
    tenant's cooldown only — the global provider breaker must stay CLOSED so other
    tenants with healthy keys are unaffected."""
    store = ResilienceStore(clock=FakeClock())
    cfg = _cfg(num_retries=0, failure_threshold=2)
    for _ in range(5):  # well past the threshold
        tgt = _target("openai", "gpt-4o-mini", exc=_Err(429))
        with pytest.raises(_Err):
            asyncio.run(call_with_resilience(
                [tgt], store, cfg, redis_prefix="tenantA:", sleep=_noop_sleep,
            ))
    assert store.breaker_state("openai", cfg) is BreakerState.CLOSED  # not poisoned
    assert store.in_cooldown("tenantA:", "openai") is True            # tenant-scoped
    assert store.in_cooldown("tenantB:", "openai") is False


def test_provider_5xx_feeds_breaker_not_cooldown():
    store = ResilienceStore(clock=FakeClock())
    cfg = _cfg(num_retries=0, failure_threshold=1)
    tgt = _target("openai", "gpt-4o-mini", exc=_Err(500))
    with pytest.raises(_Err):
        asyncio.run(call_with_resilience(
            [tgt], store, cfg, redis_prefix="tenantA:", sleep=_noop_sleep,
        ))
    assert store.breaker_state("openai", cfg) is BreakerState.OPEN
    assert store.in_cooldown("tenantA:", "openai") is False


def test_fallback_nonretryable_error_continues_chain():
    """Review invariant (C3): a fallback's revoked key (401) must not abort the
    chain nor replace the primary's error — record it and try the next fallback."""
    store = ResilienceStore(clock=FakeClock())
    sink = []
    t1 = _target("openai", "gpt-4o-mini", exc=_Err(503))          # primary transient
    t2 = _target("anthropic", "claude", exc=_Err(401))            # fallback bad key
    t3 = _target("gemini", "gemini-2.5-flash", result={"ok": 3})  # healthy fallback
    out = asyncio.run(call_with_resilience(
        [t1, t2, t3], store, _cfg(num_retries=0), attempts_sink=sink, sleep=_noop_sleep,
    ))
    assert out == {"ok": 3}
    assert [a.outcome for a in sink] == ["error", "error", "success"]


def test_primary_nonretryable_still_fails_fast():
    """Counterpart to C3: a caller/config error on the PRIMARY (400 bad request)
    would fail on every provider — fail fast, no failover."""
    store = ResilienceStore(clock=FakeClock())
    calls = []
    boom = _Err(400)
    t1 = _target("openai", "gpt-4o-mini", exc=boom, calls=calls)
    t2 = _target("anthropic", "claude", result={"never": True}, calls=calls)
    with pytest.raises(_Err) as ei:
        asyncio.run(call_with_resilience(
            [t1, t2], store, _cfg(num_retries=0), sleep=_noop_sleep,
        ))
    assert ei.value is boom
    assert calls == [("openai", "gpt-4o-mini")]   # fallback never attempted


def test_gateway_5xx_family_triggers_failover():
    """Review invariant (C4): Cloudflare-style 52x must fail over, not fail fast."""
    store = ResilienceStore(clock=FakeClock())
    t1 = _target("openai", "gpt-4o-mini", exc=_Err(522))
    t2 = _target("anthropic", "claude", result={"ok": "served"})
    out = asyncio.run(call_with_resilience(
        [t1, t2], store, _cfg(num_retries=0), sleep=_noop_sleep,
    ))
    assert out == {"ok": "served"}


def test_all_targets_failed_message_is_safe():
    """Review invariant (S2): the aggregate error message must never embed raw
    exception reprs (which can leak base_url / api-key material)."""
    class LeakyError(Exception):
        def __repr__(self):
            return "LeakyError(api_key='sk-SECRET')"
    leaky = LeakyError()
    leaky.status_code = 502
    store = ResilienceStore(clock=FakeClock())
    t1 = _target("openai", "m1", exc=leaky)
    t2 = _target("anthropic", "m2", exc=_Err(500))
    with pytest.raises(AllTargetsFailedError) as ei:
        asyncio.run(call_with_resilience(
            [t1, t2], store, _cfg(num_retries=0), sleep=_noop_sleep,
        ))
    assert "SECRET" not in str(ei.value)
    assert "LeakyError(status=502)" in str(ei.value)


def test_note_provider_outcome_feeds_breaker_from_external_paths():
    """G06 cascade / G13 batch / G10 summary calls feed the breaker via
    note_provider_outcome — success closes, 5xx counts, 429 is ignored."""
    from providers.resilience import set_resilience_store
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    set_resilience_store(store)
    try:
        config = {"resilience": {"enabled": True, "failure_threshold": 2}}
        note_provider_outcome("openai", _Err(500), config)
        note_provider_outcome("openai", _Err(429), config)   # ignored (tenant signal)
        cfg = ResilienceConfig.resolve(config, "openai")
        assert store.breaker_state("openai", cfg) is BreakerState.CLOSED  # 1 failure < 2
        note_provider_outcome("openai", _Err(502), config)
        assert store.breaker_state("openai", cfg) is BreakerState.OPEN
        note_provider_outcome("openai", None, config)         # success closes
        assert store.breaker_state("openai", cfg) is BreakerState.CLOSED
        # Disabled config is a no-op.
        note_provider_outcome("openai", _Err(500), {"resilience": {"enabled": False}})
        assert store.breaker_state("openai", cfg) is BreakerState.CLOSED
    finally:
        set_resilience_store(ResilienceStore())


# ── Per-model lockout (item #3) ───────────────────────────────────────────────

def test_config_resolve_model_lockout_defaults_and_override():
    # Default off; lock duration defaults to cooldown_seconds when unset.
    base = ResilienceConfig.resolve({"resilience": {"enabled": True, "cooldown_seconds": 45}})
    assert base.model_lockout is False
    assert base.model_failure_threshold == 3
    assert base.model_lockout_seconds == 45
    over = ResilienceConfig.resolve({"resilience": {
        "enabled": True, "model_lockout": True,
        "model_failure_threshold": 2, "model_lockout_seconds": 12}})
    assert over.model_lockout is True
    assert over.model_failure_threshold == 2
    assert over.model_lockout_seconds == 12


def test_store_model_lockout_trips_and_is_model_scoped():
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    cfg = _cfg(model_lockout=True, model_failure_threshold=2, model_lockout_seconds=30)
    assert store.allow_model("openai", "gpt-4o", cfg) is True
    store.record_model_failure("openai", "gpt-4o", cfg)
    state = store.record_model_failure("openai", "gpt-4o", cfg)
    assert state is BreakerState.OPEN
    assert store.allow_model("openai", "gpt-4o", cfg) is False       # gpt-4o locked
    # A DIFFERENT model on the SAME provider is unaffected — this is the whole point.
    assert store.allow_model("openai", "gpt-4o-mini", cfg) is True


def test_store_model_lockout_probe_and_reset():
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    cfg = _cfg(model_lockout=True, model_failure_threshold=1, model_lockout_seconds=20)
    store.record_model_failure("openai", "gpt-4o", cfg)
    assert store.allow_model("openai", "gpt-4o", cfg) is False
    clk.advance(20)
    assert store.allow_model("openai", "gpt-4o", cfg) is True        # cooldown → one probe
    store.record_model_success("openai", "gpt-4o", cfg)
    assert store.allow_model("openai", "gpt-4o", cfg) is True        # closed


def test_peek_model_state_never_creates_breaker():
    store = ResilienceStore(clock=FakeClock())
    assert store.peek_model_state("openai", "never") is BreakerState.CLOSED
    assert store.has_model_breaker("openai", "never") is False


def test_locked_model_skipped_then_failover_to_other_model():
    """A locked model is skipped with outcome 'skipped_model_lockout' and failover
    routes to another model (same provider) that still serves."""
    clk = FakeClock()
    store = ResilienceStore(clock=clk)
    cfg = _cfg(model_lockout=True, model_failure_threshold=1)
    store.record_model_failure("openai", "gpt-4o", cfg)             # lock gpt-4o
    assert store.allow_model("openai", "gpt-4o", cfg) is False
    sink = []
    t1 = _target("openai", "gpt-4o", result={"never": True})
    t2 = _target("openai", "gpt-4o-mini", result={"ok": 9})
    out = asyncio.run(call_with_resilience(
        [t1, t2], store, cfg, attempts_sink=sink, sleep=_noop_sleep,
    ))
    assert out == {"ok": 9}
    assert sink[0] == Attempt("openai", "gpt-4o", "skipped_model_lockout")
    assert sink[1].outcome == "success"


def test_model_lockout_off_does_not_gate():
    """model_lockout=False → the model gate never fires (byte-identical to breaker-only)."""
    store = ResilienceStore(clock=FakeClock())
    cfg = _cfg(model_lockout=False, model_failure_threshold=1)
    store.record_model_failure("openai", "gpt-4o", cfg)            # lock recorded but unused
    sink = []
    t1 = _target("openai", "gpt-4o", result={"ok": 1})
    out = asyncio.run(call_with_resilience([t1], store, cfg, attempts_sink=sink, sleep=_noop_sleep))
    assert out == {"ok": 1}
    assert sink[0].outcome == "success"                           # NOT skipped


def test_locked_model_failopen_when_no_alternative():
    """Fail-open: a locked model with no viable alternative is still attempted."""
    store = ResilienceStore(clock=FakeClock())
    cfg = _cfg(model_lockout=True, model_failure_threshold=1)
    store.record_model_failure("openai", "gpt-4o", cfg)
    sink = []
    t1 = _target("openai", "gpt-4o", result={"ok": 1})
    out = asyncio.run(call_with_resilience([t1], store, cfg, attempts_sink=sink, sleep=_noop_sleep))
    assert out == {"ok": 1}                                        # attempted, not blackholed
    assert sink[0].outcome == "success"


def test_model_failures_lock_model_while_fallback_keeps_provider_open():
    """The elegance: model failures lock the bad model (lower threshold) but the
    fallback model's success resets the provider breaker, so the provider stays live."""
    store = ResilienceStore(clock=FakeClock())
    # model locks at 2 model-failures; provider breaker would need 5.
    cfg = _cfg(model_lockout=True, model_failure_threshold=2, failure_threshold=5, num_retries=0)
    t1 = _target("openai", "gpt-4o", exc=_Err(503))
    t2 = _target("openai", "gpt-4o-mini", result={"ok": 1})
    for _ in range(3):
        asyncio.run(call_with_resilience([t1, t2], store, cfg, sleep=_noop_sleep))
    # gpt-4o is locked out…
    assert store.allow_model("openai", "gpt-4o", cfg) is False
    # …but the provider breaker never opened (fallback successes reset it).
    assert store.breaker_state("openai", cfg) is BreakerState.CLOSED
    assert store.allow_model("openai", "gpt-4o-mini", cfg) is True
