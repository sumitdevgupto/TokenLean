"""D1-T: Tests for G05Cache including warm_cache method."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import json
import hashlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from middleware.g05_cache import G05Cache, _embed
from middleware import RequestContext
from savings.models import SavingsRecord


def _make_ctx(config=None):
    savings = SavingsRecord(
        request_id="req-g05",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=100,
    )
    return RequestContext(
        request_id="req-g05",
        user_id="u1",
        original_messages=[{"role": "user", "content": "hello"}],
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config=config or {"groups": {"G05_cache": {"enabled": True, "l1_ttl_seconds": 3600}}},
        savings=savings,
    )


class TestWarmCache:
    @pytest.mark.asyncio
    async def test_warm_cache_stores_embedding_in_redis(self):
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()

        fake_embedding = [0.1, 0.2, 0.3]
        with patch("middleware.g05_cache._embed", AsyncMock(return_value=fake_embedding)):
            cache = G05Cache()
            result = await cache.warm_cache(
                ["hello world"],
                redis_client=mock_redis,
                prefix="",
                ttl=3600,
            )

        assert result == 1
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        assert "tok_opt:l2:warm:" in key
        stored_value = json.loads(call_args[0][1])
        assert stored_value == fake_embedding

    @pytest.mark.asyncio
    async def test_warm_cache_key_uses_sha256_of_pattern(self):
        mock_redis = AsyncMock()
        pattern = "what is the capital of France"
        expected_suffix = hashlib.sha256(pattern.encode()).hexdigest()[:16]
        expected_key = f"tok_opt:l2:warm:{expected_suffix}"

        with patch("middleware.g05_cache._embed", AsyncMock(return_value=[0.5, 0.6])):
            cache = G05Cache()
            await cache.warm_cache([pattern], redis_client=mock_redis)

        key_used = mock_redis.set.call_args[0][0]
        assert key_used == expected_key

    @pytest.mark.asyncio
    async def test_warm_cache_respects_prefix(self):
        mock_redis = AsyncMock()

        with patch("middleware.g05_cache._embed", AsyncMock(return_value=[0.1])):
            cache = G05Cache()
            await cache.warm_cache(
                ["test query"],
                redis_client=mock_redis,
                prefix="t:acme:",
            )

        key_used = mock_redis.set.call_args[0][0]
        assert key_used.startswith("t:acme:tok_opt:l2:warm:")

    @pytest.mark.asyncio
    async def test_warm_cache_returns_count_of_successes(self):
        mock_redis = AsyncMock()

        call_count = 0
        async def _embed_mock(text, model):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("embed failed")
            return [0.1]

        with patch("middleware.g05_cache._embed", _embed_mock):
            cache = G05Cache()
            result = await cache.warm_cache(
                ["ok query", "failing query", "another ok"],
                redis_client=mock_redis,
            )

        assert result == 2  # only 2 of 3 succeeded

    @pytest.mark.asyncio
    async def test_warm_cache_no_redis_returns_zero(self):
        with patch("middleware.g05_cache._get_redis", side_effect=ConnectionError("no redis")):
            cache = G05Cache()
            result = await cache.warm_cache(["test pattern"])

        assert result == 0

    @pytest.mark.asyncio
    async def test_warm_cache_empty_patterns_returns_zero(self):
        mock_redis = AsyncMock()
        cache = G05Cache()
        result = await cache.warm_cache([], redis_client=mock_redis)
        assert result == 0
        mock_redis.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_warm_cache_ttl_passed_to_redis(self):
        mock_redis = AsyncMock()

        with patch("middleware.g05_cache._embed", AsyncMock(return_value=[0.1, 0.2])):
            cache = G05Cache()
            await cache.warm_cache(["test"], redis_client=mock_redis, ttl=7200)

        call_kwargs = mock_redis.set.call_args[1]
        assert call_kwargs.get("ex") == 7200

    @pytest.mark.asyncio
    async def test_warm_cache_multiple_patterns_all_stored(self):
        mock_redis = AsyncMock()
        patterns = ["alpha query", "beta query", "gamma query"]

        with patch("middleware.g05_cache._embed", AsyncMock(return_value=[0.1])):
            cache = G05Cache()
            result = await cache.warm_cache(patterns, redis_client=mock_redis)

        assert result == 3
        assert mock_redis.set.call_count == 3

    @pytest.mark.asyncio
    async def test_warm_cache_different_patterns_different_keys(self):
        mock_redis = AsyncMock()
        patterns = ["pattern one", "pattern two"]

        with patch("middleware.g05_cache._embed", AsyncMock(return_value=[0.1])):
            cache = G05Cache()
            await cache.warm_cache(patterns, redis_client=mock_redis)

        keys = [call[0][0] for call in mock_redis.set.call_args_list]
        assert keys[0] != keys[1]
