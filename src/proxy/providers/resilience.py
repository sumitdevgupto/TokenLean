"""Provider resilience — circuit breakers, retries, per-tenant cooldowns, failover.

Turns the proxy from a single-point-of-failure into a gateway that survives an
upstream provider outage or a rate-limited key. Signals are deliberately split by
scope so one tenant can never black out another:

  * **Rate limits (429) are tenant-scoped signals.** A 429 on a tenant's (possibly
    BYOK) key sets a short per-tenant connection cooldown — it never feeds the
    global circuit breaker, so tenant A's exhausted key cannot trip the provider
    for tenant B.
  * **Provider-health failures (5xx / timeouts / connection errors) are global
    signals.** They feed the per-provider circuit breaker; after
    ``failure_threshold`` consecutive failures the provider is tripped OPEN for
    ``cooldown_seconds``, then a HALF_OPEN probe either closes or re-opens it.

**Fail-open guarantee:** a gate (breaker or cooldown) only ever *skips* a target
when another viable target remains (or a real attempt already produced an error).
The last viable target is always attempted — a request is never answered with an
error unless a provider was actually tried (or genuinely has no usable key). This
keeps single-provider deployments byte-equivalent to "no gate at all" for the
client-visible outcome while still failing over fast when fallbacks exist.

State is **in-process per worker** (each uvicorn worker keeps its own breaker and
cooldown maps). A cross-worker store can be swapped in later via
``set_resilience_store``; nothing in this module talks to Redis today.

Config: a top-level ``resilience:`` block for global defaults, with per-provider
overrides under ``providers[].resilience``. When ``enabled`` is false or the block
is absent, ``call_with_resilience`` performs exactly one attempt with no
breaker/retry — byte-identical to the pre-feature behaviour. (The shipped config
template opts in with ``enabled: true`` and retry-only defaults; deployments that
keep their existing config.yaml are unaffected.)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Non-5xx status codes that are transient/retryable. 5xx is retryable wholesale
# (>= 500 — includes 501/507/508 and Cloudflare-style 520-524, matching litellm's
# own retry taxonomy). Auth (401/403) and bad-request (400/404/422) are the
# caller/config's fault — never retried, never fed to the breaker.
_RETRYABLE_NON_5XX = frozenset({408, 409, 425, 429})
# litellm/openai exception class names that are retryable even without a status code.
_RETRYABLE_NAMES = frozenset({
    "RateLimitError", "Timeout", "APITimeoutError", "APIConnectionError",
    "InternalServerError", "ServiceUnavailableError", "APIError",
})


def is_retryable_error(exc: BaseException) -> bool:
    """True if ``exc`` is a transient provider failure (retry / failover).

    Classifies by ``status_code`` when present (litellm exceptions carry one), else by
    class name — so the pure module needs no hard litellm import and stays unit-testable.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status >= 500 or status in _RETRYABLE_NON_5XX
    return type(exc).__name__ in _RETRYABLE_NAMES


def is_rate_limit_error(exc: BaseException) -> bool:
    """True for rate-limit (429) errors — a tenant/key-scoped signal, NOT provider
    health. Feeds the per-tenant cooldown only, never the global breaker."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429
    return type(exc).__name__ == "RateLimitError"


def describe_error(exc: BaseException) -> str:
    """Safe, compact error descriptor for attempt trails and exception messages.

    Deliberately NOT ``repr(exc)`` — litellm exception reprs can embed the full
    request payload, base_url, and key material. Class name + status only.
    """
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}(status={status})" if isinstance(status, int) \
        else type(exc).__name__


class BreakerState(str, Enum):
    CLOSED = "closed"        # normal — requests flow
    OPEN = "open"            # tripped — skipped (when an alternative exists) until cooldown
    HALF_OPEN = "half_open"  # cooldown elapsed — one probe allowed


# G18 gauge encodes state as 0/1/2 for the SLA dashboard.
BREAKER_STATE_CODE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


@dataclass
class CircuitBreaker:
    """Pure per-provider circuit-breaker state machine (no I/O, injectable clock).

    Transitions:
      CLOSED  --(failures reach threshold)-->  OPEN
      OPEN    --(cooldown_seconds elapse)-->   HALF_OPEN   (allow_request lets ONE probe)
      HALF_OPEN --(probe succeeds)-->          CLOSED
      HALF_OPEN --(probe fails)-->             OPEN        (cooldown restarts)
    A success in CLOSED resets the running failure count. ``configure()`` refreshes
    threshold/cooldown from the latest config so hot-reload and per-provider
    overrides apply without a worker restart.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    _clock: Callable[[], float] = time.monotonic

    failures: int = 0
    state: BreakerState = BreakerState.CLOSED
    opened_at: float = 0.0

    def configure(self, failure_threshold: int, cooldown_seconds: float) -> None:
        """Refresh tunables from the latest resolved config (hot-reload safe)."""
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    def allow_request(self) -> bool:
        """Whether a request may proceed now. Advances OPEN→HALF_OPEN when cooldown elapsed."""
        if self.state is BreakerState.OPEN:
            if self._clock() - self.opened_at >= self.cooldown_seconds:
                self.state = BreakerState.HALF_OPEN
                return True  # single probe
            return False
        return True  # CLOSED or HALF_OPEN (probe in flight)

    def peek_state(self) -> BreakerState:
        """Current state for display, reflecting elapsed cooldown as HALF_OPEN
        WITHOUT mutating the machine (metrics must not fabricate probes)."""
        if self.state is BreakerState.OPEN and \
                self._clock() - self.opened_at >= self.cooldown_seconds:
            return BreakerState.HALF_OPEN
        return self.state

    def record_success(self) -> None:
        self.failures = 0
        self.state = BreakerState.CLOSED

    def record_failure(self) -> None:
        if self.state is BreakerState.HALF_OPEN:
            # Probe failed — straight back to OPEN, restart cooldown.
            self.state = BreakerState.OPEN
            self.opened_at = self._clock()
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self.opened_at = self._clock()


@dataclass
class ResilienceConfig:
    """Resolved resilience settings for one request (global defaults + per-provider override)."""

    enabled: bool = False
    num_retries: int = 1               # transient-error retries per target before moving on
    failure_threshold: int = 5         # consecutive provider-health failures that trip the breaker
    cooldown_seconds: float = 30.0     # breaker-open duration AND per-tenant cooldown TTL
    retry_base_delay: float = 0.2      # exponential backoff base (seconds)
    fallbacks: Dict[str, List[str]] = field(default_factory=dict)  # model → [fallback models]

    @classmethod
    def resolve(cls, config: Dict[str, Any], provider: str = "") -> "ResilienceConfig":
        base = dict((config or {}).get("resilience", {}) or {})
        # Per-provider override merges over the global block.
        if provider:
            for p in (config or {}).get("providers", []) or []:
                if p.get("name") == provider:
                    base.update(p.get("resilience", {}) or {})
                    break
        return cls(
            enabled=bool(base.get("enabled", False)),
            num_retries=int(base.get("num_retries", 1)),
            failure_threshold=int(base.get("failure_threshold", 5)),
            cooldown_seconds=float(base.get("cooldown_seconds", 30.0)),
            retry_base_delay=float(base.get("retry_base_delay", 0.2)),
            fallbacks=dict(base.get("fallbacks", {}) or {}),
        )


class ResilienceStore:
    """In-process breaker + cooldown state, shared across requests in this worker.

    Breakers are keyed by bare provider name (provider health is global — but only
    5xx/timeout failures feed them; 429s never do). Connection cooldowns are keyed
    by the tenant's ``redis_prefix`` + provider, because rate limits are a property
    of the tenant's key. Each uvicorn worker keeps independent state; swap in a
    cross-worker implementation via ``set_resilience_store`` if needed.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._cooldowns: Dict[str, float] = {}   # key → expiry (monotonic)

    def _breaker(self, provider: str, cfg: ResilienceConfig) -> CircuitBreaker:
        cb = self._breakers.get(provider)
        if cb is None:
            cb = CircuitBreaker(
                failure_threshold=cfg.failure_threshold,
                cooldown_seconds=cfg.cooldown_seconds,
                _clock=self._clock,
            )
            self._breakers[provider] = cb
        else:
            # Refresh tunables every access so hot-reload / per-provider overrides
            # apply without a worker restart (frozen-config bug fix).
            cb.configure(cfg.failure_threshold, cfg.cooldown_seconds)
        return cb

    # ── Circuit breaker (per provider, global; fed by 5xx/timeouts only) ─────
    def allow_provider(self, provider: str, cfg: ResilienceConfig) -> bool:
        return self._breaker(provider, cfg).allow_request()

    def record_provider_success(self, provider: str, cfg: ResilienceConfig) -> None:
        self._breaker(provider, cfg).record_success()

    def record_provider_failure(self, provider: str, cfg: ResilienceConfig) -> BreakerState:
        cb = self._breaker(provider, cfg)
        cb.record_failure()
        return cb.state

    def breaker_state(self, provider: str, cfg: ResilienceConfig) -> BreakerState:
        return self._breaker(provider, cfg).state

    def peek_provider_state(self, provider: str) -> BreakerState:
        """Display-only state read: never creates a breaker, reflects elapsed
        cooldown as HALF_OPEN without mutating the machine (for metrics)."""
        cb = self._breakers.get(provider)
        return cb.peek_state() if cb is not None else BreakerState.CLOSED

    # ── Per-tenant connection cooldown (fed by 429s on that tenant's key) ────
    def set_cooldown(self, redis_prefix: str, provider: str, ttl: float) -> None:
        self._cooldowns[f"{redis_prefix}|{provider}"] = self._clock() + ttl

    def in_cooldown(self, redis_prefix: str, provider: str) -> bool:
        exp = self._cooldowns.get(f"{redis_prefix}|{provider}")
        if exp is None:
            return False
        if self._clock() >= exp:
            self._cooldowns.pop(f"{redis_prefix}|{provider}", None)
            return False
        return True


def note_provider_outcome(provider: str, exc: Optional[BaseException],
                          config: Dict[str, Any]) -> None:
    """Feed a provider call outcome observed OUTSIDE call_with_resilience into the
    breaker (G06 cascade tiers, G13 batch items, G10 summaries make their own
    litellm calls). Success or provider-health failure only — 429s and caller
    errors are ignored (tenant-scoped / not health signals). Never raises; those
    call paths keep their own fallback behaviour and are never *gated* here — this
    only makes the breaker SEE their traffic so it opens/closes on true outages.
    """
    try:
        if not provider:
            return
        cfg = ResilienceConfig.resolve(config or {}, provider)
        if not cfg.enabled:
            return
        store = get_resilience_store()
        if exc is None:
            store.record_provider_success(provider, cfg)
        elif is_retryable_error(exc) and not is_rate_limit_error(exc):
            store.record_provider_failure(provider, cfg)
    except Exception:  # pragma: no cover - observability must never break a call
        pass


@dataclass
class CallTarget:
    """One candidate provider call the resilient caller may attempt.

    ``has_key=False`` marks a target the tenant has no usable credential for —
    it is skipped without an attempt (we never spend another tenant's key).
    ``adapter`` may be set lazily by ``invoke`` itself (lazy fallback targets
    resolve their key/adapter only when actually attempted).
    """

    model: str
    provider: str
    invoke: Callable[[], Awaitable[Any]]   # performs the actual litellm call for this target
    has_key: bool = True
    adapter: Any = None                     # provider adapter — pinned onto ctx on success


@dataclass
class Attempt:
    """Recorded on ctx.provider_attempts for observability + the failover audit trail.

    ``error`` holds a SAFE descriptor (class name + status), never the raw repr.
    """

    provider: str
    model: str
    outcome: str        # "success" | "error" | "failopen_attempt" |
                        # "skipped_breaker" | "skipped_cooldown" | "skipped_no_key"
    error: str = ""


class AllTargetsFailedError(Exception):
    """Raised when every resilient target was skipped or failed. Carries the last
    real error object for status mapping; the MESSAGE embeds only safe descriptors
    (never a raw exception repr, which can leak base_url/key material)."""

    def __init__(self, attempts: List[Attempt], last_error: Optional[BaseException]):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            "All %d provider target(s) failed: [%s]" % (
                len(attempts),
                ", ".join(
                    f"{a.provider}/{a.model}:{a.outcome}"
                    + (f"({a.error})" if a.error else "")
                    for a in attempts
                ),
            )
        )


# Process-wide store singleton — per-worker state, swappable for tests or a future
# cross-worker implementation.
_STORE: Optional[ResilienceStore] = None


def get_resilience_store() -> ResilienceStore:
    global _STORE
    if _STORE is None:
        _STORE = ResilienceStore()
    return _STORE


def set_resilience_store(store: ResilienceStore) -> None:
    """Swap the process store (tests, or a future cross-worker implementation)."""
    global _STORE
    _STORE = store


async def call_with_resilience(
    targets: List[CallTarget],
    store: ResilienceStore,
    cfg: ResilienceConfig,
    *,
    redis_prefix: str = "",
    attempts_sink: Optional[List[Attempt]] = None,
    on_success: Optional[Callable[[CallTarget], None]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """Attempt ``targets`` in order with breaker/cooldown gating, retries, failover.

    Semantics (each is a reviewed invariant — see test_resilience.py):
      * **Fail-open:** a gated target is skipped only when a later viable target
        remains or a real error was already collected; the last viable target is
        always attempted, so gates can never produce a zero-attempt failure.
      * **Signal scoping:** 429s set the per-tenant cooldown only; 5xx/timeouts
        feed the global breaker only.
      * **Error surfaces:** a non-retryable error on the FIRST target (the caller's
        own routed model — auth/bad-request is the caller/config's fault) is
        re-raised unchanged. A non-retryable error on a FALLBACK is recorded and
        the chain continues (a fallback's revoked key must not abort the chain nor
        masquerade as the caller's error). Single-target exhaustion re-raises the
        original error so the pre-feature surface is preserved.

    On success calls ``on_success(target)`` so the caller pins ``ctx.routed_model``
    / ``ctx.provider_adapter`` to the winning target (cost/provider attribution).
    """
    attempts = attempts_sink if attempts_sink is not None else []
    last_error: Optional[BaseException] = None
    single = len(targets) <= 1

    for i, target in enumerate(targets):
        prov = target.provider

        # Skip a target the tenant has no key for (BYOK): never spend another tenant's key.
        if not target.has_key:
            attempts.append(Attempt(prov, target.model, "skipped_no_key"))
            continue

        # Gate check (breaker open / tenant cooldown) — but FAIL OPEN: only skip when
        # skipping still leaves something (a later viable target, or an error already
        # collected from a real attempt). The last viable target is always attempted.
        if cfg.enabled:
            gated = store.in_cooldown(redis_prefix, prov) or not store.allow_provider(prov, cfg)
            if gated:
                later_viable = any(t.has_key for t in targets[i + 1:])
                if later_viable or last_error is not None:
                    reason = "skipped_cooldown" if store.in_cooldown(redis_prefix, prov) \
                        else "skipped_breaker"
                    attempts.append(Attempt(prov, target.model, reason))
                    continue
                logger.info(
                    "resilience: %s/%s gated but no alternative — failing open",
                    prov, target.model,
                )

        # Attempt this target, retrying transient errors up to num_retries times.
        max_tries = (cfg.num_retries + 1) if cfg.enabled else 1
        for attempt_i in range(max_tries):
            try:
                result = await target.invoke()
            except BaseException as exc:  # noqa: BLE001 — classified below
                last_error = exc
                retryable = is_retryable_error(exc)
                rate_limited = is_rate_limit_error(exc)
                # Signal scoping: 5xx/timeouts feed the global breaker; 429s feed
                # only this tenant's cooldown (set once, at target exhaustion below).
                if cfg.enabled and retryable and not rate_limited:
                    store.record_provider_failure(prov, cfg)
                if retryable and attempt_i < max_tries - 1:
                    await sleep(cfg.retry_base_delay * (2 ** attempt_i))
                    continue  # retry the SAME target
                # Retries exhausted for this target (or error is non-retryable).
                attempts.append(Attempt(prov, target.model, "error", describe_error(exc)))
                if cfg.enabled and rate_limited:
                    store.set_cooldown(redis_prefix, prov, cfg.cooldown_seconds)
                if not retryable and i == 0:
                    # Caller/config error on the PRIMARY (auth, bad request): fail
                    # fast with the original surface — it would fail everywhere.
                    raise
                break  # move to the next target (failover)
            else:
                if cfg.enabled:
                    store.record_provider_success(prov, cfg)
                attempts.append(Attempt(prov, target.model, "success"))
                if on_success is not None:
                    on_success(target)
                return result

    # Every target exhausted.
    if single and last_error is not None:
        # Preserve the exact single-provider error surface callers already handle.
        raise last_error
    raise AllTargetsFailedError(attempts, last_error)
