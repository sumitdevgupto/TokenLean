"""Unit tests for G00 — Rate Limiting."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
class TestG00RateLimit:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = False
        from middleware.g00_rate_limit import G00RateLimit
        ctx = await G00RateLimit().process_request(ctx)
        assert ctx.user_id == "test_user"

    async def test_enabled_default_limits(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {}
        mock_redis.expire.return_value = True
        mock_redis.hset.return_value = True
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit
            ctx = await G00RateLimit().process_request(ctx)
        
        assert ctx.user_id == "test_user"

    async def test_rate_limit_exceeded_minute(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        
        # Simulate bucket with 0 tokens at current time (no refill possible)
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {"tokens": "0", "last_refill": str(time.time())}
        mock_redis.expire.return_value = True
        mock_redis.hset.return_value = True
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded
            rl = G00RateLimit()
            with pytest.raises(RateLimitExceeded) as exc_info:
                ctx = await rl.process_request(ctx)
            
            assert exc_info.value.limit_type == "requests_per_minute"
            assert exc_info.value.retry_after > 0

    async def test_rate_limit_exceeded_hour(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        
        # Minute passes, hour fails
        now = time.time()
        mock_redis = AsyncMock()
        # First call (minute) - has tokens; second call (hour) - empty at current time
        mock_redis.hgetall.side_effect = [
            {"tokens": "10", "last_refill": str(now)},  # minute check — has tokens
            {"tokens": "0", "last_refill": str(now)},   # hour check — none left
        ]
        mock_redis.expire.return_value = True
        mock_redis.hset.return_value = True
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded
            rl = G00RateLimit()
            with pytest.raises(RateLimitExceeded) as exc_info:
                ctx = await rl.process_request(ctx)
            
            assert exc_info.value.limit_type == "requests_per_hour"
            assert exc_info.value.retry_after == 3600

    async def test_per_user_override(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        ctx.config["rate_limit"]["per_user"] = {
            "test_user": {
                "requests_per_minute": 120,
                "requests_per_hour": 2000,
            }
        }
        
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {}
        mock_redis.expire.return_value = True
        mock_redis.hset.return_value = True
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit
            rl = G00RateLimit()
            ctx = await rl.process_request(ctx)
        
        # Should use per-user limits (120/min, 2000/hour)
        assert ctx.user_id == "test_user"

    async def test_per_team_override(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.params["x_team"] = "premium-team"
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        ctx.config["rate_limit"]["per_team"] = {
            "premium-team": {
                "requests_per_minute": 300,
                "requests_per_hour": 5000,
            }
        }
        
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {}
        mock_redis.expire.return_value = True
        mock_redis.hset.return_value = True
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit
            rl = G00RateLimit()
            ctx = await rl.process_request(ctx)
        
        assert ctx.user_id == "test_user"

    async def test_fail_open_on_redis_error(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        
        mock_redis = AsyncMock()
        mock_redis.hgetall.side_effect = Exception("Redis connection failed")
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit
            rl = G00RateLimit()
            ctx = await rl.process_request(ctx)
        
        # Should allow request despite Redis error (fail open)
        assert ctx.user_id == "test_user"

    async def test_token_bucket_refill(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["rate_limit"]["enabled"] = True
        ctx.config["rate_limit"]["default"] = {
            "requests_per_minute": 60,
            "requests_per_hour": 1000,
        }
        
        # Simulate bucket with some tokens that should refill
        import time
        now = time.time()
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {
            "tokens": "0.5",
            "last_refill": str(now - 10),  # 10 seconds ago → refill kicks in
        }
        mock_redis.expire.return_value = True
        mock_redis.hset.return_value = True
        
        with patch("middleware.g00_rate_limit._get_redis", return_value=mock_redis):
            from middleware.g00_rate_limit import G00RateLimit
            rl = G00RateLimit()
            ctx = await rl.process_request(ctx)
        
        # Should pass after refill
        assert ctx.user_id == "test_user"
