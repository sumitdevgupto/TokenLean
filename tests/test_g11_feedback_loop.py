"""
G11 Output Length & Format - Feedback Loop Tests

Tests the max_tokens feedback loop functionality:
- ZSET recording of (max_tokens, completion_tokens) pairs
- Historical p95 retrieval for auto-tightening
- Utilization tracking
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from middleware import RequestContext
from middleware.g11_output_format import (
    G11OutputFormat,
    _get_historical_p95,
    _record_max_tokens_pair,
    _history_key,
)


class TestG11FeedbackLoop:
    """Test G11 max_tokens feedback loop."""

    @pytest.fixture
    def g11(self):
        return G11OutputFormat()

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.zadd = AsyncMock()
        redis.expire = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[])
        return redis

    @pytest.fixture
    def ctx_with_config(self):
        """Create a request context with G11 config."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G11_output": {
                    "enabled": True,
                    "max_tokens_feedback_loop": True,
                    "max_tokens_history_ttl_days": 7,
                }
            }
        }
        ctx.params = {"max_tokens": 500}
        ctx.model = "gpt-4o-mini"
        ctx.request_id = "test-req-001"
        ctx.user_id = "user-001"
        return ctx

    @pytest.mark.asyncio
    async def test_process_response_records_to_redis(self, g11, mock_redis, ctx_with_config):
        """Test that process_response records max_tokens pair to Redis ZSET."""
        response = {
            "usage": {
                "completion_tokens": 300,
                "total_tokens": 800,
            }
        }

        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await g11.process_response(ctx_with_config, response)

        # Verify Redis ZSET add was called
        mock_redis.zadd.assert_called_once()
        call_args = mock_redis.zadd.call_args
        key = call_args[0][0]
        member_data = json.loads(list(call_args[0][1].keys())[0])

        assert key.startswith("tok_opt:max_tokens_history:")
        assert member_data["max_tokens"] == 500
        assert member_data["completion_tokens"] == 300

    @pytest.mark.asyncio
    async def test_process_response_skips_when_disabled(self, g11, mock_redis):
        """Test that process_response skips when feedback_loop is disabled."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G11_output": {
                    "enabled": True,
                    "max_tokens_feedback_loop": False,  # Disabled
                }
            }
        }
        ctx.params = {"max_tokens": 500}

        response = {"usage": {"completion_tokens": 300}}

        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await g11.process_response(ctx, response)

        # Redis should not be called when disabled
        mock_redis.zadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_response_skips_when_g11_disabled(self, g11, mock_redis):
        """Test that process_response skips when G11 is disabled."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G11_output": {
                    "enabled": False,  # G11 disabled
                    "max_tokens_feedback_loop": True,
                }
            }
        }

        response = {"usage": {"completion_tokens": 300}}

        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await g11.process_response(ctx, response)

        # Redis should not be called when G11 disabled
        mock_redis.zadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_response_calculates_utilization(self, g11, mock_redis, ctx_with_config):
        """Test that utilization is calculated and stored."""
        response = {
            "usage": {"completion_tokens": 250}  # 50% utilization
        }

        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await g11.process_response(ctx_with_config, response)

        # Check utilization stored in params
        assert "_token_opt_feedback" in ctx.params
        assert ctx.params["_token_opt_feedback"]["max_tokens_utilization"] == 0.5

    @pytest.mark.asyncio
    async def test_get_historical_p95_with_data(self):
        """Test p95 calculation from historical data."""
        redis = AsyncMock()
        # Mock 10 entries with various completion token counts
        entries = [
            json.dumps({"max_tokens": 500, "completion_tokens": 100}),
            json.dumps({"max_tokens": 500, "completion_tokens": 200}),
            json.dumps({"max_tokens": 500, "completion_tokens": 300}),
            json.dumps({"max_tokens": 500, "completion_tokens": 400}),
            json.dumps({"max_tokens": 500, "completion_tokens": 500}),
            json.dumps({"max_tokens": 500, "completion_tokens": 600}),
            json.dumps({"max_tokens": 500, "completion_tokens": 700}),
            json.dumps({"max_tokens": 500, "completion_tokens": 800}),
            json.dumps({"max_tokens": 500, "completion_tokens": 900}),
            json.dumps({"max_tokens": 500, "completion_tokens": 1000}),
        ]
        redis.zrevrange = AsyncMock(return_value=entries)

        result = await _get_historical_p95(redis, "test:key", quantile=0.95, min_entries=5)

        # Verify we get a valid p95 value from the data
        # The 10 values are: 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000
        # Any value between 500-1000 is reasonable depending on calculation method
        assert result is not None
        assert 500 <= result <= 1000

    @pytest.mark.asyncio
    async def test_get_historical_p95_insufficient_data(self):
        """Test that p95 returns None when insufficient data."""
        redis = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[
            json.dumps({"max_tokens": 500, "completion_tokens": 100}),
        ])

        result = await _get_historical_p95(redis, "test:key", min_entries=5)

        assert result is None

    @pytest.mark.asyncio
    async def test_record_max_tokens_pair(self):
        """Test recording max_tokens pair to Redis."""
        redis = AsyncMock()
        redis.zadd = AsyncMock()
        redis.expire = AsyncMock()

        await _record_max_tokens_pair(redis, "test:key", 500, 300, 604800)

        # Verify ZSET add
        redis.zadd.assert_called_once()
        call_args = redis.zadd.call_args
        assert call_args[0][0] == "test:key"
        member_data = json.loads(list(call_args[0][1].keys())[0])
        assert member_data["max_tokens"] == 500
        assert member_data["completion_tokens"] == 300

        # Verify TTL set
        redis.expire.assert_called_once_with("test:key", 604800)

    def test_history_key_generation(self):
        """Test history key includes workflow and template IDs."""
        ctx = MagicMock(spec=RequestContext)
        ctx.params = {
            "workflow_id": "wf-123",
            "template_id": "tpl-456",
        }

        key = _history_key(ctx)

        assert key == "tok_opt:max_tokens_history:wf-123:tpl-456"

    def test_history_key_defaults(self):
        """Test history key uses defaults when IDs not provided."""
        ctx = MagicMock(spec=RequestContext)
        ctx.params = {}

        key = _history_key(ctx)

        assert key == "tok_opt:max_tokens_history:default:default"


class TestG11AutoTighten:
    """Test auto-tighten functionality with p95 data."""

    @pytest.fixture
    def g11(self):
        return G11OutputFormat()

    @pytest.fixture
    def ctx_auto_tighten(self):
        """Create context with auto-tighten enabled."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G11_output": {
                    "enabled": True,
                    "enforce_max_tokens": True,
                    "max_tokens_auto_tighten": True,
                    "tighten_quantile": 0.95,
                    "tighten_multiplier": 1.2,
                }
            }
        }
        ctx.params = {}  # No max_tokens set - should trigger auto-tighten
        ctx.model = "gpt-4o-mini"
        ctx.current_token_count = 1000
        ctx.request_id = "test-auto-001"
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        return ctx

    @pytest.mark.asyncio
    async def test_auto_tighten_with_historical_p95(self, g11, ctx_auto_tighten):
        """Test auto-tighten uses p95 * multiplier when historical data exists."""
        redis = AsyncMock()
        # Mock p95 = 800 completion tokens
        entries = [
            json.dumps({"max_tokens": 1000, "completion_tokens": 800}),
        ] * 10
        redis.zrevrange = AsyncMock(return_value=entries)

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            result = await g11.process_request(ctx_auto_tighten)

        # Should set max_tokens based on p95 * multiplier (accept reasonable range)
        assert "max_tokens" in ctx_auto_tighten.params
        assert 800 <= ctx_auto_tighten.params["max_tokens"] <= 1200

    @pytest.mark.asyncio
    async def test_auto_tighten_fallback_without_history(self, g11, ctx_auto_tighten):
        """Test fallback to default multiplier when no historical data."""
        redis = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[])  # No history

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            result = await g11.process_request(ctx_auto_tighten)

        # Should use default: 30% of input * 2.0 = 600, capped at 1024
        assert ctx_auto_tighten.params["max_tokens"] == 600


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
