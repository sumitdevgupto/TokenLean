"""Unit tests for G17 — Loop Control & Token Budget Propagation."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, patch


class _FakeRedis:
    """The few Redis calls G17 makes, backed by a dict (state persists across requests)."""

    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def expire(self, key, seconds):
        return True


@pytest.mark.asyncio
class TestG17LoopControl:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx(params={"workflow_id": "wf-1"})
        ctx.config["groups"]["G17_loop"]["enabled"] = False
        from middleware.g17_loop_control import G17LoopControl
        ctx = await G17LoopControl().process_request(ctx)
        assert "_budget_remaining" not in str(ctx.messages)

    async def test_no_workflow_id_skips(self, make_ctx):
        ctx = make_ctx()
        original_messages = [m.copy() for m in ctx.messages]
        from middleware.g17_loop_control import G17LoopControl
        ctx = await G17LoopControl().process_request(ctx)
        assert ctx.messages == original_messages

    async def test_budget_propagated_into_params(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Do the next task."}],
            params={"workflow_id": "wf-budget-test"},
        )
        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=1)       # turn counter
        mock_redis.expire = AsyncMock()
        mock_redis.get = AsyncMock(return_value="800")    # budget remaining
        mock_redis.set = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g17_loop_control._get_redis", return_value=mock_redis):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)

        # _token_budget injected into params
        assert "_token_budget" in ctx.params
        assert ctx.params["_token_budget"]["token_budget_remaining"] >= 0

    async def test_exceeded_iterations_sets_warning(self, make_ctx):
        ctx = make_ctx(params={"workflow_id": "wf-overrun"})
        ctx.config["groups"]["G17_loop"]["max_iterations"] = 3

        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=6)   # 6 > 3 iterations
        mock_redis.expire = AsyncMock()
        mock_redis.get = AsyncMock(return_value="500")
        mock_redis.set = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g17_loop_control._get_redis", return_value=mock_redis):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)

        assert ctx.params.get("_token_opt_loop_limit_reached") is True
        warnings = ctx.params.get("_token_opt_warnings", [])
        assert any("loop limit" in w.lower() or "turns" in w.lower() for w in warnings)

    async def test_low_budget_injects_compact_instruction(self, make_ctx):
        from middleware.g17_loop_control import G17LoopControl, _COMPACT_INSTRUCTION
        ctx = make_ctx(
            [{"role": "system", "content": "You are a support assistant."},
             {"role": "user", "content": "Continue the workflow."}],
            params={"workflow_id": "wf-low-budget"},
        )
        ctx.config["groups"]["G17_loop"].update(
            compact_output_enabled=True, compact_output_below_tokens=10000)
        with patch("middleware.g17_loop_control._get_redis", return_value=_FakeRedis()):
            ctx = await G17LoopControl().process_request(ctx)
        system = ctx.messages[0]["content"]
        # Appended, so the start of the prompt (the provider's cached prefix) is unchanged,
        # and format-neutral: no "Respond ONLY with required JSON fields".
        assert system == "You are a support assistant.\n" + _COMPACT_INSTRUCTION
        assert "JSON" not in _COMPACT_INSTRUCTION

    async def test_compact_mode_is_off_by_default(self, make_ctx):
        from middleware.g17_loop_control import G17LoopControl
        messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "Go on."}]
        ctx = make_ctx(messages, params={"workflow_id": "wf-default"})
        ctx.config["groups"]["G17_loop"]["compact_output_below_tokens"] = 10**9
        ctx.config["groups"]["G17_loop"].pop("compact_output_enabled", None)
        with patch("middleware.g17_loop_control._get_redis", return_value=_FakeRedis()):
            ctx = await G17LoopControl().process_request(ctx)
        assert ctx.messages == messages

    async def test_a_system_message_of_parts_gets_a_text_part(self, make_ctx):
        from middleware.g17_loop_control import G17LoopControl, _COMPACT_INSTRUCTION
        part = {"type": "text", "text": "Policy", "cache_control": {"type": "ephemeral"}}
        ctx = make_ctx([{"role": "system", "content": [dict(part)]},
                        {"role": "user", "content": "Next step."}],
                       params={"workflow_id": "wf-parts"})
        ctx.config["groups"]["G17_loop"].update(
            compact_output_enabled=True, compact_output_below_tokens=10**9)
        with patch("middleware.g17_loop_control._get_redis", return_value=_FakeRedis()):
            ctx = await G17LoopControl().process_request(ctx)
        assert ctx.messages[0]["content"] == [part, {"type": "text", "text": _COMPACT_INSTRUCTION}]

    async def test_the_budget_is_the_conversations_own_size(self, make_ctx):
        """Not a running total: the same conversation sent twice under one workflow id has
        the same remaining budget, and no budget key is kept in Redis."""
        from middleware.g17_loop_control import G17LoopControl
        from savings.calculator import count_messages_tokens
        redis = _FakeRedis()
        messages = [{"role": "user", "content": "Summarise the incident report."}]
        remaining = []
        for _ in range(2):
            ctx = make_ctx(messages, params={"workflow_id": "wf-same"})
            ctx.config["groups"]["G17_loop"]["starting_budget_tokens"] = 1000
            with patch("middleware.g17_loop_control._get_redis", return_value=redis):
                ctx = await G17LoopControl().process_request(ctx)
            remaining.append(ctx.params["_token_budget"]["token_budget_remaining"])
        assert remaining == [1000 - count_messages_tokens(messages, "gpt-4o")] * 2
        assert not [k for k in redis.data if "budget" in k]

    async def test_a_prompt_bigger_than_the_budget_leaves_zero(self, make_ctx):
        from middleware.g17_loop_control import G17LoopControl
        ctx = make_ctx([{"role": "user", "content": "word " * 200}],
                       params={"workflow_id": "wf-big"})
        ctx.config["groups"]["G17_loop"]["starting_budget_tokens"] = 10
        with patch("middleware.g17_loop_control._get_redis", return_value=_FakeRedis()):
            ctx = await G17LoopControl().process_request(ctx)
        assert ctx.params["_token_budget"]["token_budget_remaining"] == 0

    async def test_an_ordinary_chat_is_not_compacted_after_a_few_turns(self, make_ctx):
        """The finding: a chat with a ~1.5k-token system prompt re-charged its whole prompt on
        every turn against a 10k budget, and got the JSON-only instruction by turn 5 or so."""
        from middleware.g17_loop_control import G17LoopControl, _COMPACT_INSTRUCTION
        redis = _FakeRedis()
        history = [{"role": "system", "content": "You are a support assistant. " * 250}]
        for turn in range(8):
            history = history + [{"role": "user", "content": f"Question {turn}?"}]
            ctx = make_ctx(history, params={"workflow_id": "wf-support-chat"})
            ctx.config["groups"]["G17_loop"].update(
                starting_budget_tokens=10000, compact_output_below_tokens=500,
                compact_output_enabled=True, max_iterations=50)
            with patch("middleware.g17_loop_control._get_redis", return_value=redis):
                ctx = await G17LoopControl().process_request(ctx)
            assert _COMPACT_INSTRUCTION not in str(ctx.messages), f"compacted at turn {turn + 1}"
            history = history + [{"role": "assistant", "content": f"Answer {turn}."}]

    async def test_redis_error_fallback(self, make_ctx):
        ctx = make_ctx(params={"workflow_id": "wf-redis-fail"})
        with patch("middleware.g17_loop_control._get_redis", side_effect=Exception("redis down")):
            from middleware.g17_loop_control import G17LoopControl
            ctx = await G17LoopControl().process_request(ctx)
        assert ctx is not None

    async def test_shared_redis_pool_survives_concurrent_workflows(self, make_ctx):
        """G17 must never call aclose() on the shared connection-pool client —
        doing so would disconnect the pool out from under other concurrent
        requests. Run two workflows against the same client instance and
        confirm it remains open and usable for both."""
        shared_redis = AsyncMock()
        shared_redis.incr = AsyncMock(return_value=1)
        shared_redis.expire = AsyncMock()
        shared_redis.get = AsyncMock(return_value="800")
        shared_redis.set = AsyncMock()

        ctx_a = make_ctx(
            [{"role": "user", "content": "Task A"}],
            params={"workflow_id": "wf-a"},
        )
        ctx_b = make_ctx(
            [{"role": "user", "content": "Task B"}],
            params={"workflow_id": "wf-b"},
        )

        with patch("middleware.g17_loop_control._get_redis", return_value=shared_redis):
            from middleware.g17_loop_control import G17LoopControl
            g17 = G17LoopControl()
            ctx_a = await g17.process_request(ctx_a)
            ctx_b = await g17.process_request(ctx_b)

        assert "_token_budget" in ctx_a.params
        assert "_token_budget" in ctx_b.params
        shared_redis.aclose.assert_not_called()
        shared_redis.close.assert_not_called()


def test_the_template_ships_compact_output_off():
    import yaml
    from pathlib import Path
    template = Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
    cfg = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert cfg["groups"]["G17_loop"]["compact_output_enabled"] is False
