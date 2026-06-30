"""
Tests for G5+ — Enhanced Semantic Cache (embedding model upgrade).

Validates:
  - Config-driven embedding model name
  - Default model is BAAI/bge-small-en-v1.5
  - Model name passed through to _embed, _l2_lookup, _l2_store
  - Backward compatibility: old model name still works if configured
  - Single threshold retained at 0.90
"""
import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g05_cache import (
    _DEFAULT_L2_EMBEDDING_MODEL,
    _embed,
    _normalise,
    _cache_key,
)


# ─── Default model name ─────────────────────────────────────────────────────

def test_default_embedding_model():
    """Default L2 embedding model is bge-small-en-v1.5."""
    assert _DEFAULT_L2_EMBEDDING_MODEL == "BAAI/bge-small-en-v1.5"


# ─── _embed uses configured model ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_embed_uses_provided_model():
    """_embed passes model_name to get_sentence_transformer."""
    mock_model = MagicMock()
    mock_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[0.1, 0.2, 0.3]))

    with patch("ml_models.get_sentence_transformer", return_value=mock_model) as mock_get:
        result = await _embed("hello world", "custom-model-v2")
        mock_get.assert_called_once_with("custom-model-v2")
        assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_uses_default_model():
    """_embed uses default model when no model_name provided."""
    mock_model = MagicMock()
    mock_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[0.4, 0.5]))

    with patch("ml_models.get_sentence_transformer", return_value=mock_model) as mock_get:
        result = await _embed("hello world")
        mock_get.assert_called_once_with("BAAI/bge-small-en-v1.5")


@pytest.mark.asyncio
async def test_embed_backward_compat_old_model():
    """Can still use old model name via config."""
    mock_model = MagicMock()
    mock_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[0.1, 0.2]))

    with patch("ml_models.get_sentence_transformer", return_value=mock_model) as mock_get:
        result = await _embed("test", "all-MiniLM-L6-v2")
        mock_get.assert_called_once_with("all-MiniLM-L6-v2")


# ─── Config-driven model in G05Cache ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_g05_cache_passes_config_model_to_l2_lookup():
    """G05Cache.process_request passes l2_embedding_model from config to _l2_lookup."""
    import copy
    from middleware.g05_cache import G05Cache
    from tests.conftest import _make_savings
    from middleware import RequestContext

    custom_model = "my-org/custom-embed-v3"
    config = {
        "groups": {
            "G5_cache": {
                "enabled": True,
                "l1_ttl_seconds": 3600,
                "l2_similarity_threshold": 0.90,
                "l2_ttl_seconds": 86400,
                "l2_embedding_model": custom_model,
            }
        }
    }
    messages = [{"role": "user", "content": "What is the capital of France?"}]
    savings = _make_savings(messages, "gpt-4o")
    ctx = RequestContext(
        request_id="req-g5plus-test",
        user_id="test-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config=config,
        savings=savings,
    )

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # L1 miss
    mock_redis.hget = AsyncMock(return_value=None)
    mock_redis.hincrby = AsyncMock(return_value=1)

    with patch("middleware.g05_cache._get_redis", return_value=mock_redis), \
         patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock, return_value=(None, 0.0)) as mock_l2:
        cache = G05Cache()
        await cache.process_request(ctx)
        # Verify l2_lookup was called with the custom model
        mock_l2.assert_called_once()
        call_args = mock_l2.call_args
        assert call_args[0][1] == 0.90  # threshold
        assert call_args[0][2] == custom_model  # embedding_model


@pytest.mark.asyncio
async def test_g05_cache_default_model_when_not_configured():
    """G05Cache uses default model when l2_embedding_model not in config."""
    import copy
    from middleware.g05_cache import G05Cache
    from tests.conftest import _make_savings
    from middleware import RequestContext

    config = {
        "groups": {
            "G5_cache": {
                "enabled": True,
                "l1_ttl_seconds": 3600,
                "l2_similarity_threshold": 0.90,
                "l2_ttl_seconds": 86400,
                # No l2_embedding_model — should use default
            }
        }
    }
    messages = [{"role": "user", "content": "Test query"}]
    savings = _make_savings(messages, "gpt-4o")
    ctx = RequestContext(
        request_id="req-g5plus-default",
        user_id="test-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config=config,
        savings=savings,
    )

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.hget = AsyncMock(return_value=None)
    mock_redis.hincrby = AsyncMock(return_value=1)

    with patch("middleware.g05_cache._get_redis", return_value=mock_redis), \
         patch("middleware.g05_cache._l2_lookup", new_callable=AsyncMock, return_value=(None, 0.0)) as mock_l2:
        cache = G05Cache()
        await cache.process_request(ctx)
        call_args = mock_l2.call_args
        assert call_args[0][2] == "BAAI/bge-small-en-v1.5"


# ─── Normalise and cache key unchanged ───────────────────────────────────────

def test_normalise_unchanged():
    """Normalisation logic is not affected by model change."""
    msgs = [{"role": "user", "content": "Hello World"}]
    result = _normalise(msgs)
    assert "hello world" in result


def test_cache_key_deterministic():
    """Cache key is deterministic and not model-dependent."""
    key1 = _cache_key("user:hello world")
    key2 = _cache_key("user:hello world")
    assert key1 == key2
    assert key1.startswith("tok_opt:l1:")
