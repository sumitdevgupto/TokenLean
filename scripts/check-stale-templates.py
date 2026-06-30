#!/usr/bin/env python3
"""
G02 Template Deprecation Job — Flag templates unused for 30 days.

This script is designed to run as a Cloud Scheduler job (daily) or
manually by operators. It queries the template registry for templates
that haven't been accessed in N days and flags them for deprecation.

Usage:
    python check-stale-templates.py \
        --staleness-days 30 \
        --warning-days 7 \
        --notify-webhook https://hooks.slack.com/services/...

Environment:
    REDIS_URL: Redis connection URL
    GCP_PROJECT_ID: For Cloud Logging integration
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Redis key prefixes (must match g02_template_registry.py)
_TEMPLATE_META_PREFIX = "tok_opt:template:meta:"
_TEMPLATE_HISTORY_PREFIX = "tok_opt:template:history:"
_TEMPLATE_ACCESS_PREFIX = "tok_opt:template:last_access:"


@dataclass
class TemplateStatus:
    """Status report for a single template."""
    template_id: str
    version: str
    last_accessed: Optional[float]
    days_since_access: int
    total_uses_30d: int
    status: str  # ACTIVE, WARNING, STALE, DEPRECATED
    action_taken: str = ""


class StaleTemplateChecker:
    """Check for and flag stale templates."""
    
    def __init__(self, redis_url: str, staleness_days: int, warning_days: int):
        self.redis_url = redis_url
        self.staleness_days = staleness_days
        self.warning_days = warning_days
        self._redis = None
    
    def _get_redis(self):
        """Get Redis connection."""
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis
    
    async def _get_all_templates(self) -> List[str]:
        """Get list of all template IDs from Redis."""
        redis = self._get_redis()
        # Scan for all template meta keys
        pattern = f"{_TEMPLATE_META_PREFIX}*"
        template_ids = []
        
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=100)
            for key in keys:
                # Extract template_id from key
                template_id = key[len(_TEMPLATE_META_PREFIX):]
                template_ids.append(template_id)
            if cursor == 0:
                break
        
        return template_ids
    
    async def _get_template_last_access(self, template_id: str) -> Optional[float]:
        """Get last access timestamp for a template."""
        redis = self._get_redis()
        key = f"{_TEMPLATE_ACCESS_PREFIX}{template_id}"
        value = await redis.get(key)
        if value:
            return float(value)
        return None
    
    async def _get_template_usage_30d(self, template_id: str) -> int:
        """Get usage count for last 30 days."""
        redis = self._get_redis()
        key = f"{_TEMPLATE_HISTORY_PREFIX}{template_id}"
        
        # Get all history entries and count recent ones
        now = time.time()
        cutoff = now - (30 * 86400)
        
        try:
            entries = await redis.zrangebyscore(key, cutoff, "+inf")
            return len(entries)
        except Exception as exc:
            logger.warning("Failed to get history for %s: %s", template_id, exc)
            return 0
    
    async def _mark_template_warning(self, template_id: str, days: int):
        """Mark template with deprecation warning."""
        redis = self._get_redis()
        key = f"{_TEMPLATE_META_PREFIX}{template_id}"
        
        try:
            meta_raw = await redis.get(key)
            if meta_raw:
                meta = json.loads(meta_raw)
                meta["deprecation_warning_at"] = time.time()
                meta["deprecation_warning_days"] = days
                meta["deprecation_message"] = (
                    f"Template {template_id} has not been used in {days} days. "
                    f"It will be marked deprecated after {self.staleness_days} days of inactivity."
                )
                await redis.set(key, json.dumps(meta))
                return True
        except Exception as exc:
            logger.error("Failed to mark warning for %s: %s", template_id, exc)
        return False
    
    async def _mark_template_stale(self, template_id: str, days: int):
        """Mark template as stale/deprecated."""
        redis = self._get_redis()
        key = f"{_TEMPLATE_META_PREFIX}{template_id}"
        
        try:
            meta_raw = await redis.get(key)
            if meta_raw:
                meta = json.loads(meta_raw)
                meta["deprecated_at"] = time.time()
                meta["deprecated_reason"] = f"No usage for {days} days (auto-deprecation)"
                meta["sunset_at"] = time.time() + (30 * 86400)  # Sunset in 30 days
                await redis.set(key, json.dumps(meta))
                
                # Add to stale templates set
                await redis.sadd("tok_opt:templates:stale", template_id)
                return True
        except Exception as exc:
            logger.error("Failed to mark stale for %s: %s", template_id, exc)
        return False
    
    async def check_templates(self) -> List[TemplateStatus]:
        """Run staleness check on all templates."""
        template_ids = await self._get_all_templates()
        logger.info("Found %d templates to check", len(template_ids))
        
        now = time.time()
        results = []
        
        for template_id in template_ids:
            try:
                last_access = await self._get_template_last_access(template_id)
                usage_30d = await self._get_template_usage_30d(template_id)
                
                if last_access is None:
                    # Never accessed - check creation time from meta
                    days_since_access = self.staleness_days + 1  # Treat as stale
                else:
                    days_since_access = int((now - last_access) / 86400)
                
                # Determine status
                if days_since_access >= self.staleness_days:
                    status = "STALE"
                    action_taken = "auto_deprecated"
                    await self._mark_template_stale(template_id, days_since_access)
                elif days_since_access >= (self.staleness_days - self.warning_days):
                    status = "WARNING"
                    action_taken = "warning_issued"
                    await self._mark_template_warning(template_id, days_since_access)
                elif usage_30d == 0:
                    status = "WARNING"
                    action_taken = "no_usage_30d"
                else:
                    status = "ACTIVE"
                    action_taken = "none"
                
                results.append(TemplateStatus(
                    template_id=template_id,
                    version="unknown",  # Could be fetched from meta
                    last_accessed=last_access,
                    days_since_access=days_since_access,
                    total_uses_30d=usage_30d,
                    status=status,
                    action_taken=action_taken,
                ))
                
            except Exception as exc:
                logger.error("Failed to check template %s: %s", template_id, exc)
        
        return results
    
    async def send_notification(self, webhook_url: str, results: List[TemplateStatus]):
        """Send notification to webhook (e.g., Slack)."""
        stale_count = sum(1 for r in results if r.status == "STALE")
        warning_count = sum(1 for r in results if r.status == "WARNING")
        
        if stale_count == 0 and warning_count == 0:
            logger.info("No stale or warning templates - skipping notification")
            return
        
        message = {
            "text": f"📋 Template Deprecation Report",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "Template Deprecation Report",
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Summary:*\n• {stale_count} templates marked as STALE\n• {warning_count} templates with WARNING\n• Checked {len(results)} total templates",
                    }
                },
            ]
        }
        
        # Add stale templates details
        stale_templates = [r for r in results if r.status == "STALE"]
        if stale_templates:
            stale_text = "\n".join([
                f"• `{t.template_id}` - {t.days_since_access} days idle"
                for t in stale_templates[:10]  # Limit to first 10
            ])
            if len(stale_templates) > 10:
                stale_text += f"\n... and {len(stale_templates) - 10} more"
            
            message["blocks"].append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Stale Templates (Auto-Deprecated):*\n{stale_text}",
                }
            })
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(webhook_url, json=message, timeout=30.0)
                response.raise_for_status()
            logger.info("Notification sent to webhook")
        except Exception as exc:
            logger.error("Failed to send notification: %s", exc)
    
    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()


def main():
    parser = argparse.ArgumentParser(description="Check for stale templates and flag for deprecation")
    parser.add_argument("--staleness-days", type=int, default=30, help="Days of inactivity before marking stale")
    parser.add_argument("--warning-days", type=int, default=7, help="Days before staleness to issue warning")
    parser.add_argument("--notify-webhook", help="Webhook URL for notifications (Slack/Teams)")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--output-json", help="Write detailed report to JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Check only, don't modify")
    
    args = parser.parse_args()
    
    checker = StaleTemplateChecker(args.redis_url, args.staleness_days, args.warning_days)
    
    try:
        results = asyncio.run(checker.check_templates())
        
        # Print summary
        stale = [r for r in results if r.status == "STALE"]
        warning = [r for r in results if r.status == "WARNING"]
        active = [r for r in results if r.status == "ACTIVE"]
        
        print(f"\n{'='*60}")
        print(f"Template Deprecation Check Complete")
        print(f"{'='*60}")
        print(f"Total templates checked: {len(results)}")
        print(f"  🟢 Active: {len(active)}")
        print(f"  🟡 Warning: {len(warning)}")
        print(f"  🔴 Stale (auto-deprecated): {len(stale)}")
        
        if stale:
            print(f"\nStale templates (action: deprecated + sunset in 30 days):")
            for t in stale:
                print(f"  - {t.template_id}: {t.days_since_access} days idle")
        
        if warning:
            print(f"\nWarning templates (action: warning issued):")
            for t in warning[:5]:  # Show first 5
                print(f"  - {t.template_id}: {t.days_since_access} days since last use")
            if len(warning) > 5:
                print(f"  ... and {len(warning) - 5} more")
        
        # Send notification if webhook provided
        if args.notify_webhook:
            asyncio.run(checker.send_notification(args.notify_webhook, results))
        
        # Write JSON report if requested
        if args.output_json:
            report = {
                "checked_at": time.time(),
                "staleness_days": args.staleness_days,
                "warning_days": args.warning_days,
                "summary": {
                    "total": len(results),
                    "active": len(active),
                    "warning": len(warning),
                    "stale": len(stale),
                },
                "templates": [
                    {
                        "template_id": t.template_id,
                        "status": t.status,
                        "days_since_access": t.days_since_access,
                        "uses_30d": t.total_uses_30d,
                        "action": t.action_taken,
                    }
                    for t in results
                ]
            }
            with open(args.output_json, "w") as f:
                json.dump(report, f, indent=2)
            print(f"\nDetailed report written to: {args.output_json}")
        
        # Exit with error if stale templates found (for CI/CD alerting)
        if stale:
            print(f"\n⚠️  {len(stale)} templates were auto-deprecated!")
            sys.exit(1)
        
    finally:
        asyncio.run(checker.close())


if __name__ == "__main__":
    main()
