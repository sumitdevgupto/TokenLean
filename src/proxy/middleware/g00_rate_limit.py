"""
G00 · Rate Limiting
Stage: At the Gate (before any optimisation)
Purpose: Prevent abuse and control LLM costs by limiting request rate per user/team/feature
Technique: Token bucket algorithm with Redis backend for distributed limiting
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from redis.exceptions import ResponseError

from middleware import RequestContext
from cache.redis_pool import get_redis as _get_redis
from tenancy.resolver import DEFAULT_TEAM, team_tag
from trial import trial_summary
import events

logger = logging.getLogger(__name__)
GROUP = "G00"

# An idle bucket expires after this long. It must cover the hour window: an hour bucket
# untouched for 3600 s has refilled completely, so expiring it then loses nothing.
_BUCKET_TTL_SECONDS = 3600

# Both windows in ONE atomic step — Redis runs a script without interleaving another
# client's commands. Until 2026-09-18 each window was an `hgetall` then an `hset`, so
# requests in flight together (within one instance or across several) all read the same
# token count and all got through. KEYS[1] = minute bucket, KEYS[2] = hour bucket.
# ARGV = now, minute capacity, minute refill/s, hour capacity, hour refill/s, ttl.
# Returns 0 = admitted (one token taken from EACH window), 1 = minute window exhausted,
# 2 = hour window exhausted; a refused request takes nothing but still records the refill
# so elapsed time is never counted twice.
_BUCKET_LUA = """
local now = tonumber(ARGV[1])
local function level(key, cap, rate)
  local b = redis.call('HMGET', key, 'tokens', 'last_refill')
  local tokens, last = tonumber(b[1]), tonumber(b[2])
  if tokens == nil or last == nil then return cap end
  local elapsed = now - last
  if elapsed < 0 then elapsed = 0 end
  return math.min(cap, tokens + elapsed * rate)
end
local cap_m, rate_m = tonumber(ARGV[2]), tonumber(ARGV[3])
local cap_h, rate_h = tonumber(ARGV[4]), tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])
local tm = level(KEYS[1], cap_m, rate_m)
local th = level(KEYS[2], cap_h, rate_h)
local verdict = 0
if tm < 1 then
  verdict = 1
elseif th < 1 then
  verdict = 2
else
  tm = tm - 1
  th = th - 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tm), 'last_refill', tostring(now))
redis.call('HSET', KEYS[2], 'tokens', tostring(th), 'last_refill', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return verdict
"""


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""
    def __init__(self, retry_after: int, limit_type: str, scope: str):
        self.retry_after = retry_after
        self.limit_type = limit_type
        self.scope = scope
        super().__init__(f"Rate limit exceeded: {limit_type} for {scope}")


class G00RateLimit:
    def __init__(self) -> None:
        self._default_limits: Dict[str, int] = {}
        self._per_user_limits: Dict[str, Dict[str, int]] = {}
        self._per_team_limits: Dict[str, Dict[str, int]] = {}

    def _load_config(self, cfg: Dict[str, Any]) -> None:
        """Load rate limit configuration from config.yaml."""
        rl_cfg = cfg.get("rate_limit", {})
        if not rl_cfg.get("enabled", False):
            return

        # Default limits
        default = rl_cfg.get("default", {})
        self._default_limits = {
            "requests_per_minute": default.get("requests_per_minute", 60),
            "requests_per_hour": default.get("requests_per_hour", 1000),
        }

        # Per-user overrides
        self._per_user_limits = rl_cfg.get("per_user") or {}

        # Per-team overrides
        self._per_team_limits = rl_cfg.get("per_team") or {}

    def _get_limits_for_scope(
        self, user_id: str, team: str,
        tenant_id: str = "default", tier: str = "",
        rl_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """Applicable limits, most specific wins (WS23):
        per_user > per_team > per_tenant > tiers[tier] > default."""
        if user_id in self._per_user_limits:
            return self._per_user_limits[user_id]
        if team in self._per_team_limits:
            return self._per_team_limits[team]
        rl_cfg = rl_cfg or {}
        per_tenant = rl_cfg.get("per_tenant") or {}
        if tenant_id in per_tenant and isinstance(per_tenant[tenant_id], dict):
            return {**self._default_limits, **per_tenant[tenant_id]}
        tier_map = rl_cfg.get("tiers") or {}
        if tier and tier in tier_map and isinstance(tier_map[tier], dict):
            return {**self._default_limits, **tier_map[tier]}
        return self._default_limits

    @staticmethod
    def quota_key(redis_prefix: str, now: Optional[float] = None) -> str:
        """Monthly billable-request counter key (tenant-prefixed; bumped by main
        on every served 2xx, checked here before any work happens)."""
        month = time.strftime("%Y%m", time.gmtime(now if now is not None else time.time()))
        return f"{redis_prefix}quota:{month}"

    @staticmethod
    def spend_key(redis_prefix: str, now: Optional[float] = None) -> str:
        """Monthly running-USD spend counter key (tenant-prefixed; bumped by main
        on every served 2xx by the request's real ``cost_actual_usd``, checked here
        before any work happens). Separate from ``quota_key`` — this is the
        denial-of-wallet ceiling, not the request-count billing gate."""
        month = time.strftime("%Y%m", time.gmtime(now if now is not None else time.time()))
        return f"{redis_prefix}spend:{month}"

    @staticmethod
    def trial_key(redis_prefix: str) -> str:
        """Lifetime free-trial served-2xx counter key (tenant-prefixed). Unlike the
        monthly ``quota_key``/``spend_key`` this carries **no month suffix and no
        TTL** — a trial spans arbitrary calendar time. It is reset (DEL) by the admin
        console's trial ``start``/``convert``/``cancel`` actions, never by time; main
        bumps it by 1 on every served 2xx while the trial is active."""
        return f"{redis_prefix}trial_used"

    async def _check_trial(self, ctx, redis, rl_cfg: Dict[str, Any]) -> None:
        """Per-tenant free-trial gate: N days AND M served-2xx requests, whichever is
        hit first.

        Enforced only when ``rate_limit.trial.enabled`` (OSS/self-host default OFF)
        AND the tenant carries a ``trial`` block in ``config_overrides`` with status
        ``active``/``cancelled``. Expiry (days elapsed OR requests exhausted) or a
        ``cancelled`` trial raises ``RateLimitExceeded(limit_type="trial_expired")``
        which main maps to a **402** (not billed, doesn't consume allowance). Runs at
        the very top of ``process_request`` — before quota/spend and before the cache
        — so an expired tenant can't be served from cache. Fires a one-shot
        ``trial.threshold`` event at each configured warn %% and a one-shot
        ``trial.expired`` event on exhaustion (both PII-free, no-op in OSS). Fail-open
        on Redis errors (request dimension only — the day dimension is config-only and
        still enforced) and on a malformed ``started_at``."""
        tcfg = rl_cfg.get("trial") or {}
        if not tcfg.get("enabled", False):
            return
        tenant_id = getattr(ctx, "tenant_id", "default")
        if tenant_id in set(tcfg.get("exempt_tenants") or ["admin", "default"]):
            return
        trial = ctx.config.get("trial") or {}
        status = trial.get("status")
        if status not in ("active", "cancelled"):
            return  # none / converted → not gated

        redis_prefix = getattr(ctx, "redis_prefix", f"t:{tenant_id}:")
        requests_used = 0
        try:
            max_requests = int(trial.get("max_requests") or 0)
        except (TypeError, ValueError):
            max_requests = 0
        if status == "active" and max_requests > 0:
            try:
                requests_used = int(await redis.get(self.trial_key(redis_prefix)) or 0)
            except Exception as exc:
                # Fail open on the request dimension only; days still enforced below.
                logger.warning("[%s] G00 trial counter read failed (fail-open on requests): %s",
                               ctx.request_id, exc)
                requests_used = 0

        summary = trial_summary(trial, requests_used, enforced=True,
                                now=datetime.now(timezone.utc))
        try:
            generation = int(trial.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        effective = summary.get("status")

        if effective in ("expired", "cancelled"):
            reason = "cancelled" if effective == "cancelled" else "exhausted"
            try:
                first = await redis.set(f"{redis_prefix}trialexpired:{generation}", "1",
                                        nx=True, ex=8640000)  # 100 days
            except Exception:
                first = True  # dedup unavailable → still emit once (best-effort)
            if first:
                events.schedule_event(tenant_id, events.TRIAL_EXPIRED, {
                    "reason": reason,
                    "dimension": summary.get("dimension"),
                    "days": summary.get("days"),
                    "max_requests": summary.get("max_requests"),
                    "requests_used": summary.get("requests_used"),
                    "generation": generation,
                })
            raise RateLimitExceeded(
                retry_after=86400,
                limit_type="trial_expired",
                scope=f"tenant={tenant_id},reason={reason},"
                      f"dimension={summary.get('dimension')},used={summary.get('requests_used')}",
            )

        if effective == "active" and summary.get("valid"):
            await self._maybe_warn_trial(ctx, redis, tcfg, tenant_id, redis_prefix,
                                         generation, summary)

    async def _maybe_warn_trial(self, ctx, redis, tcfg, tenant_id, redis_prefix,
                                generation: int, summary: Dict[str, Any]) -> None:
        """Emit a one-shot ``trial.threshold`` event the first time ``pct_used``
        crosses each configured warn %% (``rate_limit.trial.warn_pcts``, default
        80/90) for this trial generation. De-duped per (generation, pct) via Redis
        SETNX so a bumped generation (extend/set) re-arms the warnings. Best-effort;
        never breaks the request."""
        pct_used = float(summary.get("pct_used") or 0.0)
        warn_pcts = sorted({int(p) for p in (tcfg.get("warn_pcts") or [80, 90]) if int(p) > 0})
        for threshold in warn_pcts:
            if pct_used < threshold:
                continue
            try:
                first = await redis.set(f"{redis_prefix}trialwarn:{generation}:{threshold}",
                                        "1", nx=True, ex=8640000)
                if not first:
                    continue  # already warned at this threshold for this generation
            except Exception:
                continue  # dedup unavailable → skip rather than spam
            events.schedule_event(tenant_id, events.TRIAL_THRESHOLD, {
                "pct": threshold,
                "dimension": summary.get("dimension"),
                "pct_days": summary.get("pct_days"),
                "pct_requests": summary.get("pct_requests"),
                "days_remaining": summary.get("days_remaining"),
                "requests_remaining": summary.get("requests_remaining"),
                "generation": generation,
            })

    async def _check_quota(self, ctx, redis, rl_cfg: Dict[str, Any]) -> None:
        """WS23 monthly request-quota gate from the billing rate card.

        Enforced only when ``rate_limit.quota.enabled`` — OSS/self-host default OFF.
        Cap = included_requests × (1 + grace_pct/100); 0/absent included_requests
        means unlimited (the enterprise custom row). Fail-open on Redis errors."""
        qcfg = rl_cfg.get("quota") or {}
        if not qcfg.get("enabled", False):
            return
        tenant_id = getattr(ctx, "tenant_id", "default")
        if tenant_id in set(qcfg.get("exempt_tenants") or ["admin", "default"]):
            return
        tier = (getattr(ctx, "pricing_tier", "") or "free").lower()
        rate_card = (ctx.config.get("billing", {}) or {}).get("rate_card", {}) or {}
        included = ((rate_card.get(tier) or {}).get("included_requests") or 0)
        try:
            included = int(included)
        except (TypeError, ValueError):
            included = 0
        if included <= 0:
            return  # unlimited
        grace = float(qcfg.get("grace_pct", 10) or 0)
        cap = int(included * (1 + grace / 100.0))
        try:
            used = int(await redis.get(self.quota_key(getattr(ctx, "redis_prefix", f"t:{tenant_id}:"))) or 0)
        except Exception as exc:
            logger.warning("[%s] G00 quota check failed (fail-open): %s", ctx.request_id, exc)
            return
        if used >= cap:
            raise RateLimitExceeded(
                retry_after=86400,
                limit_type="quota_exceeded",
                scope=f"tenant={tenant_id},tier={tier},included={included},used={used}",
            )

    @staticmethod
    def _effective_spend_cap(ctx, scfg: Dict[str, Any]) -> Optional[float]:
        """Resolve the effective monthly USD cap for this request, most specific wins:
        per-tenant override (``limits.monthly_spend_cap_usd`` — written by the portal
        into ``tenant_configs.config_overrides`` and deep-merged into ``ctx.config``)
        > tier default (``billing.rate_card.<tier>.monthly_spend_cap_usd``). Returns
        ``None`` when no cap is configured (unlimited) — 0/negative also means unlimited."""
        override = (ctx.config.get("limits") or {}).get("monthly_spend_cap_usd")
        if override is None:
            tier = (getattr(ctx, "pricing_tier", "") or "free").lower()
            rate_card = (ctx.config.get("billing", {}) or {}).get("rate_card", {}) or {}
            override = (rate_card.get(tier) or {}).get("monthly_spend_cap_usd")
        if override is None:
            return None
        try:
            cap = float(override)
        except (TypeError, ValueError):
            return None
        return cap if cap > 0 else None

    async def _check_spend(self, ctx, redis, rl_cfg: Dict[str, Any]) -> None:
        """Per-tenant monthly running-USD spend gate (denial-of-wallet ceiling).

        Enforced only when ``rate_limit.spend_cap.enabled`` — OSS/self-host default
        OFF. Cap = effective ``monthly_spend_cap_usd`` × (1 + grace_pct/100); absent
        cap means unlimited. Reads the running ``t:<id>:spend:<YYYYMM>`` counter that
        main bumps by each served request's real ``cost_actual_usd``. Fail-open on
        Redis errors so the ceiling never takes the proxy down."""
        scfg = rl_cfg.get("spend_cap") or {}
        if not scfg.get("enabled", False):
            return
        tenant_id = getattr(ctx, "tenant_id", "default")
        if tenant_id in set(scfg.get("exempt_tenants") or ["admin", "default"]):
            return
        cap = self._effective_spend_cap(ctx, scfg)
        if cap is None:
            return  # unlimited
        grace = float(scfg.get("grace_pct", 0) or 0)
        ceiling = cap * (1 + grace / 100.0)
        redis_prefix = getattr(ctx, "redis_prefix", f"t:{tenant_id}:")
        try:
            spent = float(await redis.get(self.spend_key(redis_prefix)) or 0.0)
        except Exception as exc:
            logger.warning("[%s] G00 spend check failed (fail-open): %s", ctx.request_id, exc)
            return
        if spent >= ceiling:
            # Outbound event: the cap was hit (best-effort, PII-free, no-op in OSS).
            events.schedule_event(tenant_id, events.SPEND_CAP_REACHED, {
                "cap_usd": round(cap, 4), "spent_usd": round(spent, 4),
                "tier": (getattr(ctx, "pricing_tier", "") or "free").lower(),
            })
            raise RateLimitExceeded(
                retry_after=86400,
                limit_type="spend_cap_exceeded",
                scope=f"tenant={tenant_id},cap_usd={cap:.4f},spent_usd={spent:.4f}",
            )
        # Early-warning event: spend crossed `warn_pct`% of the cap (below the ceiling).
        # De-duped once per tenant per month via a Redis SETNX flag so it fires on the
        # crossing, not on every subsequent request.
        await self._maybe_warn_budget(ctx, redis, scfg, tenant_id, redis_prefix, cap, spent)

    async def _maybe_warn_budget(self, ctx, redis, scfg, tenant_id, redis_prefix,
                                 cap: float, spent: float) -> None:
        """Emit a one-shot ``budget.threshold`` event when spend first crosses
        ``warn_pct``% of the cap this month. Best-effort; never breaks the request."""
        warn_pct = float(scfg.get("warn_pct", 0) or 0)
        if warn_pct <= 0 or spent < cap * (warn_pct / 100.0):
            return
        try:
            month = time.strftime("%Y%m", time.gmtime())
            first = await redis.set(f"{redis_prefix}budgetwarn:{month}", "1", nx=True, ex=2678400)
            if not first:
                return  # already warned this month
        except Exception:
            return  # dedup unavailable → skip the warning rather than spam
        events.schedule_event(tenant_id, events.BUDGET_THRESHOLD, {
            "cap_usd": round(cap, 4), "spent_usd": round(spent, 4),
            "warn_pct": warn_pct,
            "tier": (getattr(ctx, "pricing_tier", "") or "free").lower(),
        })

    async def _check_token_bucket(
        self,
        redis,
        key: str,
        capacity: int,
        refill_rate: float,
        now: float,
    ) -> bool:
        """
        Token bucket algorithm using Redis — a read-modify-write, so it is only the
        FALLBACK for a Redis that refuses EVAL (see ``_consume``): requests in flight
        together all read the same count, so concurrent traffic can overdraw it.

        Args:
            redis: Redis client
            key: Redis key for this bucket
            capacity: Maximum tokens in bucket
            refill_rate: Tokens refilled per second
            now: Current timestamp

        Returns:
            True if request allowed, False otherwise
        """
        # Read current bucket state
        bucket = await redis.hgetall(key)
        await redis.expire(key, _BUCKET_TTL_SECONDS)

        if not bucket:
            # Initialize new bucket
            tokens = float(capacity)
            last_refill = now
        else:
            tokens = float(bucket.get("tokens", capacity))
            last_refill = float(bucket.get("last_refill", now))

            # Refill tokens based on elapsed time
            elapsed = now - last_refill
            tokens = min(float(capacity), tokens + elapsed * refill_rate)

        # Check if we have enough tokens
        if tokens >= 1:
            # Consume one token
            tokens -= 1
            await redis.hset(key, mapping={"tokens": tokens, "last_refill": now})
            await redis.expire(key, _BUCKET_TTL_SECONDS)
            return True
        else:
            await redis.hset(key, mapping={"tokens": tokens, "last_refill": now})
            await redis.expire(key, _BUCKET_TTL_SECONDS)
            return False

    async def _consume(self, redis, minute_key: str, hour_key: str,
                       limits: Dict[str, Any], now: float) -> Optional[str]:
        """Take one request from BOTH windows atomically (``_BUCKET_LUA``).

        Returns None when admitted, else the exhausted window — ``"minute"`` or ``"hour"``
        — in which case nothing was taken. A Redis that refuses EVAL (``ResponseError``:
        scripting disabled, a cluster CROSSSLOT) falls back to the per-window
        read-modify-write with a WARNING — correct for serial traffic only. Any other error
        propagates, so the caller fails open exactly as it always has.
        """
        cap_m = float(limits["requests_per_minute"])
        cap_h = float(limits["requests_per_hour"])
        try:
            verdict = await redis.eval(
                _BUCKET_LUA, 2, minute_key, hour_key,
                now, cap_m, cap_m / 60.0, cap_h, cap_h / 3600.0, _BUCKET_TTL_SECONDS)
        except ResponseError as exc:
            logger.warning(
                "G00 could not update the rate-limit bucket atomically (%s); falling back "
                "to a read-modify-write that concurrent requests can overdraw", exc)
            if not await self._check_token_bucket(redis, minute_key, cap_m, cap_m / 60.0, now):
                return "minute"
            if not await self._check_token_bucket(redis, hour_key, cap_h, cap_h / 3600.0, now):
                return "hour"
            return None
        return {1: "minute", 2: "hour"}.get(int(verdict))

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        """Check rate limits before processing the request."""
        cfg = ctx.config.get("rate_limit", {})
        # Free-trial gate runs FIRST — before quota/spend and before the cache — so an
        # expired trial can never be served from cache. Independent switch; a deployment
        # may run trials without the rps/rph limiter or the quota/spend gates.
        if cfg.get("trial", {}).get("enabled", False):
            try:
                await self._check_trial(ctx, _get_redis(), cfg)
            except RateLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("[%s] G00 trial gate error (fail-open): %s", ctx.request_id, exc)
        # WS23: the monthly quota gate is independent of the rps/rph limiter switch —
        # a deployment may enforce quotas without token-bucket limiting.
        if cfg.get("quota", {}).get("enabled", False):
            try:
                await self._check_quota(ctx, _get_redis(), cfg)
            except RateLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("[%s] G00 quota gate error (fail-open): %s", ctx.request_id, exc)
        # Per-tenant USD spend cap — independent of the rps/rph limiter and the
        # request-count quota; a deployment may enforce a dollar ceiling alone.
        if cfg.get("spend_cap", {}).get("enabled", False):
            try:
                await self._check_spend(ctx, _get_redis(), cfg)
            except RateLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("[%s] G00 spend gate error (fail-open): %s", ctx.request_id, exc)
        if not cfg.get("enabled", False):
            return ctx

        self._load_config(ctx.config)

        # Scope identifiers — the AUTHENTICATED identity only (2026-09-18). The bucket used to
        # key on ctx.user_id and params["x_team"], both caller-chosen (an allow-listed
        # X-User-ID override; any X-Team), so a fresh value per request got a fresh bucket and
        # these limits never bound. principal = the identity the key carries; team = a gateway
        # key's X-Team, else "default" (tenancy.resolver.apply_caller_identity). A context
        # built outside the pipeline carries no stamp and falls back to user_id / "default".
        principal = getattr(ctx, "key_principal", "") or ctx.user_id
        team = getattr(ctx, "team", "") or DEFAULT_TEAM
        tenant_id = getattr(ctx, "tenant_id", "default")

        # Applicable limits (per_user > per_team > per_tenant > tier > default). per_team can
        # only match a gateway key's team: every other key is team "default".
        limits = self._get_limits_for_scope(
            principal, team, tenant_id=tenant_id,
            tier=(getattr(ctx, "pricing_tier", "") or "").lower(), rl_cfg=cfg)

        # For a caller sending no identity headers this is the exact pre-2026-09-18 key, so
        # its bucket carries over. team_tag: a gateway's team is never embedded verbatim.
        scope = f"{tenant_id}:{principal}:{team_tag(team)}"
        try:
            exhausted = await self._consume(
                _get_redis(), f"tok_opt:rate_limit:minute:{scope}",
                f"tok_opt:rate_limit:hour:{scope}", limits, time.time())
        except Exception as exc:
            logger.warning("[%s] G00 rate limit check failed: %s", ctx.request_id, exc)
            # Fail open: allow request if rate limiting fails
            return ctx

        if exhausted:
            raise RateLimitExceeded(
                retry_after=60 if exhausted == "minute" else 3600,
                limit_type=f"requests_per_{exhausted}",
                scope=f"user={principal},team={team_tag(team)}",
            )
        logger.debug("[%s] G00 rate limit check passed for user=%s team=%s",
                     ctx.request_id, principal, team_tag(team))
        return ctx
