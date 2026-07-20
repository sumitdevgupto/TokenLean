"""Free-trial state derivation (OSS core, pure).

A tenant's free-trial state lives in ``ctx.config['trial']`` (written by the admin
console into ``tenant_configs.config_overrides`` and deep-merged into ``ctx.config``
per request by :class:`tenancy.config.TenantConfigLoader`). The block shape is::

    trial:
      status: active | converted | cancelled     # absent ⇒ "none"
      started_at: "2026-07-20T09:00:00+00:00"     # UTC ISO-8601
      days: 14            # 0/absent ⇒ time dimension unlimited
      max_requests: 5000  # 0/absent ⇒ request dimension unlimited
      generation: 2       # bumped by every admin mutation (re-arms warnings)

This module derives the *effective* view — remaining days/requests, percent used
per dimension, the driving dimension, an effective status, and a warning band —
from that block plus the served-2xx counter. It is pure and dependency-free so the
G00 enforcement gate (OSS core) and the admin/portal trial APIs (commercial) share
one source of truth and cannot drift. It contains NO I/O: callers supply
``requests_used`` (from Redis) and ``now`` (tz-aware UTC).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# Display warning bands (fixed — these drive the portal amber/red states). The set
# of percentages that actually FIRE `trial.threshold` webhooks is configurable
# (`rate_limit.trial.warn_pcts`, default [80, 90]) and handled in the G00 gate.
WARN80 = 80.0
WARN90 = 90.0

_GATED_STATUSES = ("active", "cancelled")


def parse_started_at(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 ``started_at`` into a tz-aware UTC datetime, or ``None``
    when missing/malformed (callers fail open on ``None``)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def trial_summary(
    trial: Optional[Dict[str, Any]],
    requests_used: int,
    enforced: bool,
    now: datetime,
) -> Dict[str, Any]:
    """Derive the effective trial view. Pure; ``now`` must be tz-aware UTC.

    The returned dict always carries ``status`` — one of
    ``none | active | expired | converted | cancelled`` (``expired`` is derived:
    a stored-``active`` trial whose days elapsed or requests exhausted). For
    active/expired trials it also carries ``days``/``max_requests``/``generation``,
    ``started_at``/``expires_at``, ``days_remaining``/``requests_remaining``,
    ``pct_days``/``pct_requests``/``pct_used``, the driving ``dimension``, a
    ``warn_level`` (``none | warn80 | warn90 | expired``), and ``valid`` (False when
    ``started_at`` was malformed — only the DAY dimension fails open in that case;
    a request-limited trial keeps enforcing since it needs no timestamp).
    """
    trial = trial or {}
    raw_status = trial.get("status") or "none"
    enforced = bool(enforced)

    # none / converted → not gated, no derived numbers.
    if raw_status not in _GATED_STATUSES:
        return {"status": raw_status, "enforced": enforced}

    days = _int(trial.get("days"))
    max_requests = _int(trial.get("max_requests"))
    generation = _int(trial.get("generation"))
    requests_used = max(0, _int(requests_used))

    out: Dict[str, Any] = {
        "status": raw_status,
        "started_at": trial.get("started_at"),
        "days": days,
        "max_requests": max_requests,
        "generation": generation,
        "requests_used": requests_used,
        "enforced": enforced,
    }

    if raw_status == "cancelled":
        out.update({"status": "cancelled", "warn_level": "expired",
                    "dimension": "cancelled", "valid": True})
        return out

    # A malformed/missing started_at only fails open on the DAY dimension (it's the
    # one that needs the timestamp) — the REQUEST dimension needs no timestamp and
    # must keep enforcing regardless, else a corrupted started_at grants unlimited
    # requests on a request-limited trial.
    started_at = parse_started_at(trial.get("started_at"))
    valid = started_at is not None

    pct_days = 0.0
    days_remaining: Optional[float] = None
    expired_days = False
    expires_at: Optional[str] = None
    if valid and days > 0:
        elapsed = max(0.0, (now - started_at).total_seconds())
        total = days * 86400.0
        pct_days = min(100.0, 100.0 * elapsed / total)
        days_remaining = max(0.0, (total - elapsed) / 86400.0)
        expired_days = elapsed >= total
        expires_at = (started_at + timedelta(days=days)).isoformat()

    pct_requests = 0.0
    requests_remaining: Optional[int] = None
    expired_requests = False
    if max_requests > 0:
        pct_requests = min(100.0, 100.0 * requests_used / max_requests)
        requests_remaining = max(0, max_requests - requests_used)
        expired_requests = requests_used >= max_requests

    expired = expired_days or expired_requests
    if expired:
        dimension = "days" if expired_days else "requests"
    else:
        dimension = "days" if pct_days >= pct_requests else "requests"
    pct_used = max(pct_days, pct_requests)

    if expired:
        warn_level = "expired"
    elif pct_used >= WARN90:
        warn_level = "warn90"
    elif pct_used >= WARN80:
        warn_level = "warn80"
    else:
        warn_level = "none"

    out.update({
        "status": "expired" if expired else "active",
        "expires_at": expires_at,
        "days_remaining": round(days_remaining, 2) if days_remaining is not None else None,
        "requests_remaining": requests_remaining,
        "pct_days": round(pct_days, 1),
        "pct_requests": round(pct_requests, 1),
        "pct_used": round(pct_used, 1),
        "dimension": dimension,
        "warn_level": warn_level,
        "valid": valid,
    })
    return out
