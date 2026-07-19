"""
G00 · Rate Limiting
Stage: At the Gate (before any optimisation)
Purpose: Prevent abuse and control LLM costs by limiting request rate per user/team/feature
Technique: Token bucket algorithm with Redis backend for distributed limiting
"""
import logging
import time
from typing import Any, Dict, Optional

from middleware import RequestContext
from cache.redis_pool import get_redis as _get_redis
import events

logger = logging.getLogger(__name__)
GROUP = "G00"


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
        Token bucket algorithm using Redis.
        
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
        await redis.expire(key, 3600)  # 1 hour TTL

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
            await redis.expire(key, 3600)
            return True
        else:
            await redis.hset(key, mapping={"tokens": tokens, "last_refill": now})
            await redis.expire(key, 3600)
            return False

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        """Check rate limits before processing the request."""
        cfg = ctx.config.get("rate_limit", {})
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

        # Extract scope identifiers
        user_id = ctx.user_id
        team = ctx.params.get("x_team", "default")
        feature = ctx.params.get("x_feature", "default")
        tenant_id = getattr(ctx, "tenant_id", "default")

        # Get applicable limits (per_user > per_team > per_tenant > tier > default)
        limits = self._get_limits_for_scope(
            user_id, team, tenant_id=tenant_id,
            tier=(getattr(ctx, "pricing_tier", "") or "").lower(), rl_cfg=cfg)

        now = time.time()
        redis = _get_redis()

        try:
            # Check per-minute limit (60 tokens per minute = 1 token/sec refill rate)
            minute_key = f"tok_opt:rate_limit:minute:{tenant_id}:{user_id}:{team}"
            minute_allowed = await self._check_token_bucket(
                redis,
                minute_key,
                capacity=limits["requests_per_minute"],
                refill_rate=limits["requests_per_minute"] / 60.0,
                now=now,
            )

            if not minute_allowed:
                raise RateLimitExceeded(
                    retry_after=60,
                    limit_type="requests_per_minute",
                    scope=f"user={user_id},team={team}",
                )

            # Check per-hour limit (refill rate = capacity / 3600 seconds)
            hour_key = f"tok_opt:rate_limit:hour:{tenant_id}:{user_id}:{team}"
            hour_allowed = await self._check_token_bucket(
                redis,
                hour_key,
                capacity=limits["requests_per_hour"],
                refill_rate=limits["requests_per_hour"] / 3600.0,
                now=now,
            )

            if not hour_allowed:
                raise RateLimitExceeded(
                    retry_after=3600,
                    limit_type="requests_per_hour",
                    scope=f"user={user_id},team={team}",
                )

            logger.debug(
                "[%s] G00 rate limit check passed for user=%s team=%s",
                ctx.request_id,
                user_id,
                team,
            )
            return ctx

        except RateLimitExceeded:
            raise
        except Exception as exc:
            logger.warning("[%s] G00 rate limit check failed: %s", ctx.request_id, exc)
            # Fail open: allow request if rate limiting fails
            return ctx
