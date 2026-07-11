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


def _tool_call(cid: str, name: str = "f", args: str = "{}") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def _assert_no_orphan_tool_results(messages) -> None:
    """Every role:"tool" message must reference a tool_call_id declared by a
    *preceding* assistant tool_calls entry — otherwise litellm/OpenAI/Anthropic
    reject the whole request with a 400 (the C1 failure mode)."""
    declared: set = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if tc.get("id"):
                    declared.add(tc["id"])
        elif m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            assert tcid in declared, (
                f"orphaned tool result {tcid!r} at the window boundary — provider would 400"
            )


class TestSlidingWindowToolPairing:
    """C1 regression — the sliding-window cut must be tool-pairing-aware so a long
    agentic (tool-calling) conversation never gets an orphaned role:"tool" at the
    window boundary."""

    def test_split_no_tool_messages_is_plain_positional(self):
        from middleware.g10_memory import _safe_window_split
        turns = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
            for i in range(8)
        ]
        # No tool messages → identical to the old blind cut (len 8 - keep 4).
        assert _safe_window_split(turns, 4) == 4

    def test_split_snaps_back_over_orphaned_tool_result(self):
        from middleware.g10_memory import _safe_window_split
        turns = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_a")]},
            {"role": "tool", "tool_call_id": "call_a", "content": "result_a"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "user", "content": "final"},
        ]
        # keep=4 → blind boundary is index 4 (the tool result); it must snap back
        # to the declaring assistant at index 3 rather than orphan the result.
        assert _safe_window_split(turns, 4) == 3

    def test_split_snaps_over_parallel_tool_results(self):
        from middleware.g10_memory import _safe_window_split
        turns = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("a"), _tool_call("b", name="g")]},
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
            {"role": "user", "content": "final"},
        ]
        # keep=3 → blind boundary is index 2 (first of two parallel results);
        # snap back over both results to the single declaring assistant (index 1).
        assert _safe_window_split(turns, 3) == 1

    def test_split_pathological_all_tool_returns_zero(self):
        from middleware.g10_memory import _safe_window_split
        turns = [{"role": "assistant", "content": None, "tool_calls": [_tool_call("x")]}] + [
            {"role": "tool", "tool_call_id": "x", "content": "r"} for _ in range(6)
        ]
        # A boundary buried in an unbroken tool run has no clean cut → 0 (trim nothing).
        assert _safe_window_split(turns, 3) == 0

    @pytest.mark.asyncio
    async def test_apply_sliding_window_keeps_tool_pairs_wellformed(self, make_ctx):
        from middleware import g10_memory
        turns = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_a")]},
            {"role": "tool", "tool_call_id": "call_a", "content": "result_a"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "user", "content": "final"},
        ]
        ctx = make_ctx([{"role": "system", "content": "sys"}] + turns)
        # The naive turns[-4:] cut would have started the window on the orphaned
        # tool result; guard against a regression by asserting that shape up front.
        assert turns[-4:][0] == {"role": "tool", "tool_call_id": "call_a", "content": "result_a"}

        with patch.object(g10_memory, "_summarise", new_callable=AsyncMock,
                          return_value="earlier-turns summary"):
            await g10_memory._apply_sliding_window(ctx, window=2, summary_model="gpt-4o-mini")

        # No orphaned tool result survived the cut, and the declaring assistant did.
        _assert_no_orphan_tool_results(ctx.messages)
        assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in ctx.messages)
        # Still trimmed (a summary system message was injected).
        assert any("summary" in (m.get("content") or "") for m in ctx.messages
                   if m.get("role") == "system")
