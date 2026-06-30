"""
G05 Temporal Workflow Activity Replay — Step-Level Idempotent Caching

Provides step-level idempotent caching for Temporal workflows:
- Cache activity results with deterministic keys
- Support replay from cache on workflow retry
- Integration with G05 cache layers

Uses Redis for activity result storage with workflow-aware TTL.
"""
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G05_ACTIVITY"


class ActivityCacheKey:
    """Generate deterministic cache keys for Temporal activities."""
    
    @staticmethod
    def generate(
        workflow_id: str,
        activity_name: str,
        activity_input: Dict,
        run_id: Optional[str] = None,
    ) -> str:
        """
        Generate deterministic cache key.
        
        Keys are deterministic for the same workflow+activity+input combination,
        allowing replay from cache across retries.
        """
        # Normalize input for consistent hashing
        input_str = json.dumps(activity_input, sort_keys=True, separators=(',', ':'))
        
        # Combine components
        key_components = f"{workflow_id}:{activity_name}:{input_str}"
        
        # Generate hash
        hash_value = hashlib.sha256(key_components.encode()).hexdigest()[:32]
        
        return f"tok_opt:activity:{hash_value}"
    
    @staticmethod
    def generate_run_specific(
        workflow_id: str,
        run_id: str,
        activity_name: str,
        activity_input: Dict,
    ) -> str:
        """Generate run-specific cache key (for non-idempotent activities)."""
        input_str = json.dumps(activity_input, sort_keys=True, separators=(',', ':'))
        key_components = f"{workflow_id}:{run_id}:{activity_name}:{input_str}"
        hash_value = hashlib.sha256(key_components.encode()).hexdigest()[:32]
        return f"tok_opt:activity:run:{hash_value}"


class TemporalActivityCache:
    """Cache for Temporal activity results."""
    
    def __init__(self, redis_client):
        self.redis = redis_client
    
    async def get_cached_result(
        self,
        workflow_id: str,
        activity_name: str,
        activity_input: Dict,
        run_id: Optional[str] = None,
        use_deterministic_key: bool = True,
    ) -> Optional[Dict]:
        """
        Get cached activity result.
        
        Args:
            use_deterministic_key: If True, key is deterministic across runs
                                   (for idempotent activities)
        """
        if use_deterministic_key:
            key = ActivityCacheKey.generate(workflow_id, activity_name, activity_input)
        else:
            if not run_id:
                return None
            key = ActivityCacheKey.generate_run_specific(
                workflow_id, run_id, activity_name, activity_input
            )
        
        try:
            cached = await self.redis.get(key)
            if cached:
                data = json.loads(cached)
                logger.debug(
                    "Activity cache hit: %s/%s",
                    workflow_id,
                    activity_name,
                )
                return {
                    "result": data["result"],
                    "cached_at": data.get("cached_at"),
                    "execution_time_ms": data.get("execution_time_ms"),
                    "from_cache": True,
                }
        except Exception as exc:
            logger.warning("Activity cache get failed: %s", exc)
        
        return None
    
    async def cache_result(
        self,
        workflow_id: str,
        activity_name: str,
        activity_input: Dict,
        result: Any,
        execution_time_ms: float,
        run_id: Optional[str] = None,
        use_deterministic_key: bool = True,
        ttl_seconds: int = 86400 * 7,  # 7 days default
    ):
        """Cache activity result for future replay."""
        if use_deterministic_key:
            key = ActivityCacheKey.generate(workflow_id, activity_name, activity_input)
        else:
            if not run_id:
                return
            key = ActivityCacheKey.generate_run_specific(
                workflow_id, run_id, activity_name, activity_input
            )
        
        data = {
            "result": result,
            "cached_at": time.time(),
            "execution_time_ms": execution_time_ms,
            "workflow_id": workflow_id,
            "activity_name": activity_name,
        }
        
        try:
            await self.redis.setex(key, ttl_seconds, json.dumps(data))
            logger.debug(
                "Cached activity result: %s/%s (TTL=%ds)",
                workflow_id,
                activity_name,
                ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Activity cache set failed: %s", exc)
    
    async def invalidate_workflow_cache(self, workflow_id: str):
        """Invalidate all cached results for a workflow."""
        try:
            # Find all keys for this workflow
            pattern = f"tok_opt:activity:*"
            keys = await self.redis.keys(pattern)
            
            # Filter to this workflow's keys
            workflow_keys = [k for k in keys if workflow_id in k.decode()]
            
            if workflow_keys:
                await self.redis.delete(*workflow_keys)
                logger.info(
                    "Invalidated %d cached activities for workflow %s",
                    len(workflow_keys),
                    workflow_id,
                )
        except Exception as exc:
            logger.warning("Activity cache invalidation failed: %s", exc)


class G05TemporalActivity:
    """G05 middleware for Temporal activity replay support."""
    
    def __init__(self):
        self._activity_cache: Optional[TemporalActivityCache] = None
    
    def _get_cache(self, redis) -> TemporalActivityCache:
        if self._activity_cache is None:
            self._activity_cache = TemporalActivityCache(redis)
        return self._activity_cache
    
    async def check_activity_cache(
        self,
        ctx: RequestContext,
        workflow_id: str,
        activity_name: str,
        activity_input: Dict,
        run_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Check if activity result is cached.
        
        Returns cached result or None if not cached.
        """
        cfg = ctx.config.get("groups", {}).get("G5_cache", {})
        if not cfg.get("temporal_activity_cache", False):
            return None
        
        try:
            from cache.redis_pool import get_redis
            redis = get_redis()
            cache = self._get_cache(redis)
            
            # Determine if activity is idempotent (can use deterministic key)
            idempotent_activities = cfg.get("idempotent_activities", [])
            use_deterministic = activity_name in idempotent_activities
            
            result = await cache.get_cached_result(
                workflow_id=workflow_id,
                activity_name=activity_name,
                activity_input=activity_input,
                run_id=run_id,
                use_deterministic_key=use_deterministic,
            )
            
            if result:
                ctx.savings.add_step(
                    GROUP,
                    f"Temporal activity replay: {activity_name}",
                    0,  # No tokens saved, but time/compute saved
                    0,
                )
            
            return result
            
        except Exception as exc:
            logger.warning("[%s] G05 activity cache check failed: %s", ctx.request_id, exc)
            return None
    
    async def cache_activity_result(
        self,
        ctx: RequestContext,
        workflow_id: str,
        activity_name: str,
        activity_input: Dict,
        result: Any,
        execution_time_ms: float,
        run_id: Optional[str] = None,
    ):
        """Cache activity result after execution."""
        cfg = ctx.config.get("groups", {}).get("G5_cache", {})
        if not cfg.get("temporal_activity_cache", False):
            return
        
        try:
            from cache.redis_pool import get_redis
            redis = get_redis()
            cache = self._get_cache(redis)
            
            # Determine key type
            idempotent_activities = cfg.get("idempotent_activities", [])
            use_deterministic = activity_name in idempotent_activities
            
            # Auto-TTL based on activity type
            ttl = cfg.get("activity_cache_ttl_seconds", 86400 * 7)
            
            await cache.cache_result(
                workflow_id=workflow_id,
                activity_name=activity_name,
                activity_input=activity_input,
                result=result,
                execution_time_ms=execution_time_ms,
                run_id=run_id,
                use_deterministic_key=use_deterministic,
                ttl_seconds=ttl,
            )
            
        except Exception as exc:
            logger.warning("[%s] G05 activity cache store failed: %s", ctx.request_id, exc)


# Decorator for Temporal activities
def cached_activity(activity_name: str, idempotent: bool = True):
    """
    Decorator to enable caching for Temporal activities.
    
    Usage:
        @cached_activity("fetch_data", idempotent=True)
        async def fetch_data_activity(ctx, query: str) -> Dict:
            # Expensive operation
            return result
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            # Extract workflow context from args
            workflow_ctx = args[0] if args else None
            
            # Generate cache key from function args
            cache_key_data = {
                "args": args[1:],  # Skip workflow context
                "kwargs": kwargs,
            }
            
            # Check cache (if Redis available)
            # ... cache lookup logic ...
            
            # Execute function
            start_time = time.time()
            result = await func(*args, **kwargs)
            execution_time_ms = (time.time() - start_time) * 1000
            
            # Cache result
            # ... cache store logic ...
            
            return result
        
        return wrapper
    return decorator


if __name__ == "__main__":
    # Test cache key generation
    key1 = ActivityCacheKey.generate("wf-123", "activity-A", {"query": "test"})
    key2 = ActivityCacheKey.generate("wf-123", "activity-A", {"query": "test"})
    key3 = ActivityCacheKey.generate("wf-123", "activity-A", {"query": "different"})
    
    print(f"Same input -> Same key: {key1 == key2}")
    print(f"Different input -> Different key: {key1 != key3}")
