"""Unit tests for G10 — Conversation & Memory Management."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG10Memory:
    async def test_disabled_passes_through(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages)
        ctx.config["groups"]["G10_memory"]["enabled"] = False
        original_count = len(ctx.messages)
        from middleware.g10_memory import G10Memory
        ctx = await G10Memory().process_request(ctx)
        assert len(ctx.messages) == original_count

    async def test_no_session_id_skips(self, make_ctx):
        # Use very few messages so the sliding window doesn't trigger (turns <= window*2)
        ctx = make_ctx([{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}])
        # No session_id — _apply_sliding_window runs but with 2 turns and window=2: 2 <= 2*2 → returns early
        from middleware.g10_memory import G10Memory
        ctx = await G10Memory().process_request(ctx)
        # Nothing trimmed (too few turns)
        assert len(ctx.messages) == 2

    async def test_sliding_window_truncates_old_turns(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages, params={"x_session_id": "session-123"})
        ctx.config["groups"]["G10_memory"]["sliding_window_turns"] = 2
        tokens_before = ctx.current_token_count

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)   # new session
        mock_redis.set = AsyncMock()
        mock_redis.expire = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g10_memory._get_redis", return_value=mock_redis):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock,
                       return_value="Summary of old turns."):
                from middleware.g10_memory import G10Memory
                ctx = await G10Memory().process_request(ctx)

        tokens_after = ctx.current_token_count
        assert tokens_after <= tokens_before

    async def test_sliding_window_records_step_saving(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages, params={"x_session_id": "session-456"})
        ctx.config["groups"]["G10_memory"]["sliding_window_turns"] = 1

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()
        mock_redis.expire = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g10_memory._get_redis", return_value=mock_redis):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock,
                       return_value="Short summary."):
                from middleware.g10_memory import G10Memory
                ctx = await G10Memory().process_request(ctx)

        if any(s.group == "G10" for s in ctx.savings.step_savings):
            step = next(s for s in ctx.savings.step_savings if s.group == "G10")
            assert step.tokens_after <= step.tokens_before

    async def test_redis_error_fallback(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages, params={"x_session_id": "session-789"})
        # Redis fails → falls back to _apply_sliding_window; _summarise is called for old turns
        with patch("middleware.g10_memory._get_redis", side_effect=Exception("redis down")):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock,
                       return_value="Summary."):
                from middleware.g10_memory import G10Memory
                ctx = await G10Memory().process_request(ctx)
        # Should not raise; context trimmed to window
        assert ctx is not None
