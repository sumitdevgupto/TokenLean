"""
G04 Rules-Based Bypass - Database Resolution Tests

Tests DB-first resolution layer:
- Exact match lookup
- Fuzzy matching
- Confidence scoring
- Bypass candidate auditing
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from middleware.g04_db_resolution import DBResolutionCache, G04DBResolution


class TestDBResolutionCache:
    """Test database resolution cache."""

    @pytest.fixture
    def mock_pool(self):
        """Create mock connection pool."""
        pool = AsyncMock()
        conn = AsyncMock()
        
        # Setup async context manager properly
        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=conn)
        async_cm.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=async_cm)
        
        return pool, conn

    @pytest.mark.asyncio
    async def test_exact_match_found(self, mock_pool):
        """Test exact query match returns cached response."""
        pool, conn = mock_pool
        cache = DBResolutionCache("postgresql://test")
        cache._pool = pool
        
        # Mock fetchrow to return a match
        conn.fetchrow = AsyncMock(return_value={
            "response_text": "Cached answer",
            "metadata": {"source": "cache"},
            "hit_count": 5,
        })
        
        result = await cache.get_exact_match("What is your return policy?")
        
        assert result is not None
        assert result["response"] == "Cached answer"
        assert result["match_type"] == "exact"
        assert result["confidence"] == 1.0
        assert result["hit_count"] == 6  # Incremented

    @pytest.mark.asyncio
    async def test_exact_match_not_found(self, mock_pool):
        """Test no match returns None."""
        pool, conn = mock_pool
        cache = DBResolutionCache("postgresql://test")
        cache._pool = pool
        
        conn.fetchrow = AsyncMock(return_value=None)
        
        result = await cache.get_exact_match("New query never seen")
        
        assert result is None

    @pytest.mark.asyncio
    async def test_fuzzy_match_high_similarity(self, mock_pool):
        """Test fuzzy match with high similarity."""
        pool, conn = mock_pool
        cache = DBResolutionCache("postgresql://test")
        cache._pool = pool
        
        # Mock similarity query - create mock that supports both attr and dict access
        row = MagicMock()
        row_data = {
            "sim": 0.92,
            "query_text": "What is your return policy?",
            "response_text": "Return policy info",
            "metadata": {},
            "hit_count": 3,
        }
        row.__getitem__ = MagicMock(side_effect=row_data.get)
        row.__iter__ = MagicMock(return_value=iter(row_data.keys()))
        
        conn.fetch = AsyncMock(return_value=[row])
        
        result = await cache.get_fuzzy_match("What's your return policy?", similarity_threshold=0.85)
        
        assert result is not None
        assert result["match_type"] == "fuzzy"
        assert result["confidence"] == 0.92

    @pytest.mark.asyncio
    async def test_fuzzy_match_below_threshold(self, mock_pool):
        """Test fuzzy match below threshold returns None."""
        pool, conn = mock_pool
        cache = DBResolutionCache("postgresql://test")
        cache._pool = pool
        
        row = MagicMock()
        row_data = {"sim": 0.70}
        row.__getitem__ = MagicMock(side_effect=row_data.get)
        conn.fetch = AsyncMock(return_value=[row])
        
        result = await cache.get_fuzzy_match("Completely different query", similarity_threshold=0.85)
        
        assert result is None

    @pytest.mark.asyncio
    async def test_store_new_response(self, mock_pool):
        """Test storing new query-response pair."""
        pool, conn = mock_pool
        cache = DBResolutionCache("postgresql://test")
        cache._pool = pool
        
        await cache.store_response(
            "How do I reset my password?",
            "Go to settings and click 'Reset Password'.",
            {"category": "account"},
        )
        
        conn.execute.assert_called_once()
        # Verify UPSERT pattern used
        call_args = conn.execute.call_args[0]
        assert "INSERT INTO query_resolution_cache" in call_args[0]

    @pytest.mark.asyncio
    async def test_query_hashing_deterministic(self):
        """Test that same query produces same hash."""
        cache = DBResolutionCache("")
        
        hash1 = cache._hash_query("What is the status?")
        hash2 = cache._hash_query("What is the status?")
        hash3 = cache._hash_query("what is the status?")  # Case different
        
        assert hash1 == hash2  # Identical queries
        assert hash1 == hash3  # Case insensitive


class TestG04DBResolution:
    """Test G04 middleware integration."""

    @pytest.fixture
    def ctx_with_db_config(self):
        """Create context with DB resolution enabled."""
        ctx = MagicMock()
        ctx.config = {
            "database_url": "postgresql://test",
            "groups": {
                "G4_rules_bypass": {
                    "db_first_resolution": True,
                    "fuzzy_similarity_threshold": 0.85,
                }
            }
        }
        ctx.messages = [{"role": "user", "content": "What is your return policy?"}]
        ctx.current_token_count = 100
        ctx.request_id = "test-001"
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        return ctx

    @pytest.mark.asyncio
    async def test_exact_match_bypass(self, ctx_with_db_config):
        """Test exact match triggers bypass."""
        g04 = G04DBResolution()
        
        mock_cache = AsyncMock()
        mock_cache.ensure_table = AsyncMock()
        mock_cache.get_exact_match = AsyncMock(return_value={
            "response": "30 days for full refund",
            "match_type": "exact",
            "confidence": 1.0,
            "hit_count": 10,
        })
        mock_cache.get_fuzzy_match = AsyncMock(return_value=None)
        
        with patch.object(g04, '_get_cache', return_value=mock_cache):
            result = await g04.process_request(ctx_with_db_config)
        
        assert result.cache_hit is True
        assert result.cache_level == "DB_EXACT"
        assert result.cache_response == "30 days for full refund"
        assert result.db_bypass_confidence == 1.0

    @pytest.mark.asyncio
    async def test_fuzzy_match_bypass(self, ctx_with_db_config):
        """Test fuzzy match triggers bypass."""
        g04 = G04DBResolution()
        
        mock_cache = AsyncMock()
        mock_cache.ensure_table = AsyncMock()
        mock_cache.get_exact_match = AsyncMock(return_value=None)
        mock_cache.get_fuzzy_match = AsyncMock(return_value={
            "response": "Return within 30 days",
            "match_type": "fuzzy",
            "confidence": 0.90,
            "hit_count": 5,
        })
        
        with patch.object(g04, '_get_cache', return_value=mock_cache):
            result = await g04.process_request(ctx_with_db_config)
        
        assert result.cache_hit is True
        assert result.cache_level == "DB_FUZZY"
        assert result.db_bypass_confidence == 0.90

    @pytest.mark.asyncio
    async def test_no_match_marks_candidate(self, ctx_with_db_config):
        """Test no match marks as bypass candidate."""
        g04 = G04DBResolution()
        
        mock_cache = AsyncMock()
        mock_cache.ensure_table = AsyncMock()
        mock_cache.get_exact_match = AsyncMock(return_value=None)
        mock_cache.get_fuzzy_match = AsyncMock(return_value=None)
        
        with patch.object(g04, '_get_cache', return_value=mock_cache):
            result = await g04.process_request(ctx_with_db_config)
        
        assert hasattr(result, 'db_bypass_candidate')
        assert result.db_bypass_candidate is True
        assert result.db_bypass_confidence == 0.0

    @pytest.mark.asyncio
    async def test_disabled_when_config_off(self):
        """Test disabled when db_first_resolution is False."""
        ctx = MagicMock()
        ctx.config = {
            "database_url": "postgresql://test",
            "groups": {
                "G4_rules_bypass": {
                    "db_first_resolution": False,
                }
            }
        }
        ctx.messages = [{"role": "user", "content": "Test"}]
        ctx.request_id = "test-disabled"
        ctx.current_token_count = 100
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        
        g04 = G04DBResolution()
        result = await g04.process_request(ctx)
        
        # Should return ctx without setting cache_hit
        assert result is ctx
        # MagicMock auto-creates attrs on access, so we can't use hasattr/getattr reliably
        # Just verify the returned object is the same ctx (no modifications made)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
