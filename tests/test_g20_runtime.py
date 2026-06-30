"""B1-T: Tests for G20 runtime prompt optimizer middleware."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import hashlib
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from middleware import RequestContext
from middleware.g20_prompt_optimizer import G20PromptOptimizer, _fp
from savings.models import SavingsRecord


def _make_ctx(messages, enabled=True, redis_prefix=""):
    savings = SavingsRecord(
        request_id="req-g20",
        user_id="u1",
        timestamp=datetime.now(timezone.utc),
        model_requested="gpt-4o",
        routed_model="gpt-4o",
        baseline_tokens=100,
    )
    return RequestContext(
        request_id="req-g20",
        user_id="u1",
        original_messages=list(messages),
        messages=list(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config={
            "groups": {
                "g20_prompt_optimizer": {"enabled": enabled},
            }
        },
        savings=savings,
        redis_prefix=redis_prefix,
    )


def _make_redis(template: str) -> MagicMock:
    """Mock Redis that returns ``template`` for any .get() call.

    G20._load_template awaits redis.get(), so .get must be async.
    """
    r = MagicMock()
    r.get = AsyncMock(return_value=template.encode("utf-8"))
    return r


class TestFingerprintHelper:
    def test_fp_returns_16_hex_chars(self):
        result = _fp("hello world")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_fp_is_deterministic(self):
        assert _fp("same text") == _fp("same text")

    def test_fp_differs_for_different_text(self):
        assert _fp("text A") != _fp("text B")

    def test_fp_truncates_at_512_chars(self):
        long_text = "x" * 1000
        truncated = "x" * 512
        assert _fp(long_text) == _fp(truncated)


class TestG20PromptOptimizer:
    @pytest.mark.asyncio
    async def test_disabled_returns_ctx_unchanged(self):
        msgs = [{"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=False)
        g20 = G20PromptOptimizer(redis_client=_make_redis("Optimised version"))
        result = await g20.process_request(ctx)
        assert result.messages[0]["content"] == "You are a helpful assistant."

    @pytest.mark.asyncio
    async def test_enabled_with_matching_template_swaps_system_prompt(self):
        original = "You are a helpful assistant that answers questions."
        optimised = "Answer questions concisely."
        msgs = [{"role": "system", "content": original},
                {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True)
        g20 = G20PromptOptimizer(redis_client=_make_redis(optimised))
        result = await g20.process_request(ctx)
        assert result.messages[0]["content"] == optimised

    @pytest.mark.asyncio
    async def test_cache_miss_returns_original(self):
        original = "You are a helpful assistant."
        msgs = [{"role": "system", "content": original},
                {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True)
        redis_miss = MagicMock()
        redis_miss.get.return_value = None
        g20 = G20PromptOptimizer(redis_client=redis_miss)
        result = await g20.process_request(ctx)
        assert result.messages[0]["content"] == original

    @pytest.mark.asyncio
    async def test_no_redis_client_returns_original(self):
        original = "You are a helpful assistant."
        msgs = [{"role": "system", "content": original},
                {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True)
        g20 = G20PromptOptimizer(redis_client=None)
        result = await g20.process_request(ctx)
        assert result.messages[0]["content"] == original

    @pytest.mark.asyncio
    async def test_savings_tracked_on_swap(self):
        original = "You are a very verbose helpful assistant that answers all kinds of questions."
        optimised = "Answer questions."
        msgs = [{"role": "system", "content": original},
                {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True)
        g20 = G20PromptOptimizer(redis_client=_make_redis(optimised))
        result = await g20.process_request(ctx)
        steps = result.savings.step_savings
        assert any(s.group == "G20" for s in steps)
        g20_step = next(s for s in steps if s.group == "G20")
        assert g20_step.absolute_saving > 0

    @pytest.mark.asyncio
    async def test_no_system_message_no_op(self):
        msgs = [{"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True)
        g20 = G20PromptOptimizer(redis_client=_make_redis("Optimised"))
        result = await g20.process_request(ctx)
        assert result.messages == msgs

    @pytest.mark.asyncio
    async def test_redis_uses_tenant_prefix(self):
        original = "You are a helpful assistant."
        msgs = [{"role": "system", "content": original}, {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True, redis_prefix="t:acme:")
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        g20 = G20PromptOptimizer(redis_client=mock_redis)
        await g20.process_request(ctx)
        called_key = mock_redis.get.call_args[0][0]
        assert called_key.startswith("t:acme:tok_opt:g20:tpl:")

    @pytest.mark.asyncio
    async def test_redis_error_falls_back_to_original(self):
        original = "You are a helpful assistant."
        msgs = [{"role": "system", "content": original}, {"role": "user", "content": "Hi"}]
        ctx = _make_ctx(msgs, enabled=True)
        err_redis = MagicMock()
        err_redis.get.side_effect = ConnectionError("Redis down")
        g20 = G20PromptOptimizer(redis_client=err_redis)
        result = await g20.process_request(ctx)
        assert result.messages[0]["content"] == original

    @pytest.mark.asyncio
    async def test_process_response_passthrough(self):
        ctx = _make_ctx([{"role": "user", "content": "Hi"}], enabled=True)
        g20 = G20PromptOptimizer()
        response = {"choices": [{"message": {"content": "Hello"}}]}
        result = await g20.process_response(ctx, response)
        assert result == response
