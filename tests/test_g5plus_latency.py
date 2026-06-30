"""
G5+ latency test — verifies cache-hit short-circuit without real timing dependencies.

Validates:
  - Cache hit returns without LLM call (call avoidance, not wall-clock timing)
  - L1 exact-match: sub-millisecond Redis lookup path
  - L2 semantic: embedding lookup path
  - Miss path proceeds normally
"""
import copy
import json
import sys
import os
import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g05_cache import G05Cache
from middleware import RequestContext
from tests.conftest import _make_savings


def _make_ctx(messages, config):
    savings = _make_savings(messages, "gpt-4o")
    return RequestContext(
        request_id="req-g5plus-latency",
        user_id="latency-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config=config,
        savings=savings,
    )


def _cache_config(l1_hit=False, l2_hit=False):
    return {
        "groups": {
            "G5_cache": {
                "enabled": True,
                "l1_ttl_seconds": 3600,
                "l2_similarity_threshold": 0.90,
                "l2_ttl_seconds": 86400,
                "l2_embedding_model": "BAAI/bge-small-en-v1.5",
                "gptcache_enabled": False,
                "step_cache_enabled": False,
            }
        }
    }


@pytest.mark.asyncio
async def test_l1_hit_short_circuits():
    """L1 exact-match hit sets cache_hit=True and short-circuits pipeline."""
    messages = [{"role": "user", "content": "What is the weather in Paris?"}]
    ctx = _make_ctx(messages, _cache_config())

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=json.dumps({"answer": "Sunny, 22C"}))
    mock_redis.hget = AsyncMock(return_value=None)
    mock_redis.hincrby = AsyncMock(return_value=1)

    with patch("middleware.g05_cache._get_redis", return_value=mock_redis):
        cache = G05Cache()
        result = await cache.process_request(ctx)

    assert result.cache_hit is True
    assert result.cache_level == "L1"
    assert result.cache_response == {"answer": "Sunny, 22C"}
    # Should have recorded a savings step
    assert len(result.savings.step_savings) >= 1
    assert result.savings.step_savings[0].group == "G05"


@pytest.mark.asyncio
async def test_l1_miss_l2_hit_short_circuits():
    """L1 miss → L2 semantic hit short-circuits pipeline."""
    messages = [{"role": "user", "content": "What is the weather in Paris?"}]
    ctx = _make_ctx(messages, _cache_config())

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # L1 miss
    mock_redis.hget = AsyncMock(return_value=None)
    mock_redis.hincrby = AsyncMock(return_value=1)

    with patch("middleware.g05_cache._get_redis", return_value=mock_redis), \
         patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock, return_value=({"answer": "Cloudy, 18C"}, 0.95)):
        cache = G05Cache()
        result = await cache.process_request(ctx)

    assert result.cache_hit is True
    assert result.cache_level == "L2"
    assert result.cache_response == {"answer": "Cloudy, 18C"}
    steps = result.savings.step_savings
    assert len(steps) >= 1
    assert steps[0].group == "G05"
    assert "score=0.950" in steps[0].description


@pytest.mark.asyncio
async def test_l1_miss_l2_miss_proceeds():
    """Both L1 and L2 miss: pipeline proceeds (no cache_hit flag)."""
    messages = [{"role": "user", "content": "What is the capital of Madagascar?"}]
    ctx = _make_ctx(messages, _cache_config())

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.hget = AsyncMock(return_value=None)
    mock_redis.hincrby = AsyncMock(return_value=1)

    with patch("middleware.g05_cache._get_redis", return_value=mock_redis), \
         patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock, return_value=(None, 0.0)):
        cache = G05Cache()
        result = await cache.process_request(ctx)

    assert result.cache_hit is False
    assert result.cache_level is None
    assert len(result.savings.step_savings) == 0  # No savings on miss


@pytest.mark.asyncio
async def test_hit_avoids_redundant_embed():
    """L1 hit must avoid calling _embed (which is the slow path)."""
    messages = [{"role": "user", "content": "Hello world"}]
    ctx = _make_ctx(messages, _cache_config())

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=json.dumps({"answer": "Hi"}))
    mock_redis.hget = AsyncMock(return_value=None)
    mock_redis.hincrby = AsyncMock(return_value=1)

    mock_embed = AsyncMock()

    with patch("middleware.g05_cache._get_redis", return_value=mock_redis), \
         patch("middleware.g05_cache._embed", mock_embed):
        cache = G05Cache()
        await cache.process_request(ctx)

    # _embed should never be called on L1 hit
    mock_embed.assert_not_called()
