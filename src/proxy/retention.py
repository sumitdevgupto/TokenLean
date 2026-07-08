"""
Data-retention engine (CORE) — periodic purge of aged rows, config-driven.

Implements the previously declared-but-unimplemented retention story (WS25):
``audit_events`` and ``usage_events`` grew forever, and expired ``cache_l2`` rows
were only ever superseded, never deleted. All knobs default OFF/0 so self-host
behaviour is unchanged until an operator opts in:

    retention:
      enabled: false          # master switch for the background loop
      interval_hours: 24      # how often the purge pass runs
      audit_days: 0           # 0 = keep audit_events forever
      usage_days: 0           # 0 = keep usage_events forever (keep >= 400 for billing!)
      cache_l2_expired_cleanup: true   # delete cache_l2 rows past expires_at

GCS JSONL lifecycle is handled at the bucket (Terraform lifecycle rule), not here.
"""
import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# usage_events is the billing source of record — refuse to purge below ~13 months
# even if misconfigured, so an invoice period can never lose its rows.
_MIN_USAGE_DAYS = 400


async def run_retention_pass(pg_pool, cfg: Dict[str, Any]) -> Dict[str, int]:
    """One purge pass. Returns per-table deletion counts. Never raises."""
    out: Dict[str, int] = {}
    if pg_pool is None:
        return out

    def _days(key: str) -> int:
        try:
            return max(0, int(cfg.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    audit_days = _days("audit_days")
    usage_days = _days("usage_days")
    if usage_days and usage_days < _MIN_USAGE_DAYS:
        logger.warning(
            "retention: usage_days=%d is below the %d-day billing floor — clamping",
            usage_days, _MIN_USAGE_DAYS)
        usage_days = _MIN_USAGE_DAYS

    async def _purge(label: str, sql: str, *args) -> None:
        try:
            async with pg_pool.acquire() as conn:
                result = await conn.execute(sql, *args)
            try:
                out[label] = int(result.split()[-1])
            except Exception:
                out[label] = 0
        except Exception as exc:
            logger.warning("retention: %s purge failed: %s", label, exc)

    if audit_days:
        await _purge(
            "audit_events",
            "DELETE FROM audit_events WHERE timestamp < NOW() - ($1 || ' days')::interval",
            str(audit_days))
    if usage_days:
        await _purge(
            "usage_events",
            "DELETE FROM usage_events WHERE timestamp < NOW() - ($1 || ' days')::interval",
            str(usage_days))
    if cfg.get("cache_l2_expired_cleanup", True):
        await _purge(
            "cache_l2",
            "DELETE FROM cache_l2 WHERE expires_at IS NOT NULL AND expires_at < NOW()")

    if any(out.values()):
        logger.info("retention pass: %s", out)
    return out


async def run_retention_loop(get_pg_pool, get_config) -> None:
    """Background loop started from main startup when retention.enabled.

    ``get_pg_pool``/``get_config`` are zero-arg callables resolved each pass so
    config hot-reload applies and the pool can appear after startup.
    """
    while True:
        cfg = {}
        try:
            cfg = (get_config() or {}).get("retention", {}) or {}
        except Exception:
            pass
        interval = 3600 * max(1, int(cfg.get("interval_hours", 24) or 24))
        if cfg.get("enabled", False):
            try:
                pool = get_pg_pool()
                await run_retention_pass(pool, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("retention loop error: %s", exc)
        await asyncio.sleep(interval)
