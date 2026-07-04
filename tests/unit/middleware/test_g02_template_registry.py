"""Unit tests for G02 — Prompt Template Registry."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import time
import pytest
from unittest.mock import AsyncMock, PropertyMock, patch


@pytest.mark.asyncio
class TestG02TemplateRegistry:
    async def test_disabled_skips(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G2_template_registry"]["enabled"] = False
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)
        assert len(ctx.savings.step_savings) == 0

    async def test_no_template_id_skips(self, make_ctx):
        ctx = make_ctx()
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)
        assert len(ctx.savings.step_savings) == 0

    async def test_within_budget_no_warning(self, make_ctx):
        ctx = make_ctx(
            [{"role": "system", "content": "Hi"}, {"role": "user", "content": "yes"}],
            params={"template_id": "test-template"},
        )
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)
        warnings = ctx.params.get("_token_opt_warnings", [])
        g02_warnings = [w for w in warnings if "G02" in w or "budget" in w.lower()]
        assert len(g02_warnings) == 0

    async def test_exceeds_system_prompt_budget_warns(self, make_ctx):
        # Budget total_input_max=100 tokens; create a system prompt well over that
        long_system = "You are a very detailed and extremely thorough assistant. " * 10
        ctx = make_ctx(
            [{"role": "system", "content": long_system}, {"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)
        # G02 records a step_saving (not params warnings) when budget is exceeded
        assert len(ctx.savings.step_savings) == 1
        step = ctx.savings.step_savings[0]
        assert step.group == "G02"
        assert "OVER" in step.description or "test-template" in step.description

    async def test_unregistered_template_id_skips(self, make_ctx):
        ctx = make_ctx(params={"template_id": "nonexistent-template"})
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)
        assert len(ctx.savings.step_savings) == 0

    async def test_budget_exactly_at_limit_not_over(self, make_ctx):
        """current_tokens == total_input_max must NOT be flagged OVER
        (the check is strictly `>`)."""
        ctx = make_ctx(
            [{"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )

        with patch("middleware.g02_template_registry._get_redis") as mock_get_redis, \
             patch("middleware.RequestContext.current_token_count", new_callable=PropertyMock, return_value=100):
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value=None)
            mock_redis.set = AsyncMock(return_value=True)
            mock_redis.zadd = AsyncMock(return_value=1)
            mock_redis.expire = AsyncMock(return_value=True)
            mock_redis.zcard = AsyncMock(return_value=1)
            mock_redis.zrevrange = AsyncMock(return_value=[])
            mock_get_redis.return_value = mock_redis

            from middleware.g02_template_registry import G02TemplateRegistry
            ctx = await G02TemplateRegistry().process_request(ctx)

        step = ctx.savings.step_savings[0]
        assert "OVER" not in step.description

    async def test_budget_over_by_one_token_flags_over(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )

        with patch("middleware.g02_template_registry._get_redis") as mock_get_redis, \
             patch("middleware.RequestContext.current_token_count", new_callable=PropertyMock, return_value=101):
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value=None)
            mock_redis.set = AsyncMock(return_value=True)
            mock_redis.zadd = AsyncMock(return_value=1)
            mock_redis.expire = AsyncMock(return_value=True)
            mock_redis.zcard = AsyncMock(return_value=1)
            mock_redis.zrevrange = AsyncMock(return_value=[])
            mock_get_redis.return_value = mock_redis

            from middleware.g02_template_registry import G02TemplateRegistry
            ctx = await G02TemplateRegistry().process_request(ctx)

        step = ctx.savings.step_savings[0]
        assert "OVER by 1" in step.description

    async def test_token_history_reports_average(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )

        history_entries = [
            json.dumps({"timestamp": time.time(), "tokens": 40, "request_id": "r1"}),
            json.dumps({"timestamp": time.time(), "tokens": 60, "request_id": "r2"}),
        ]

        with patch("middleware.g02_template_registry._get_redis") as mock_get_redis, \
             patch("middleware.RequestContext.current_token_count", new_callable=PropertyMock, return_value=50):
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value=None)
            mock_redis.set = AsyncMock(return_value=True)
            mock_redis.zadd = AsyncMock(return_value=1)
            mock_redis.expire = AsyncMock(return_value=True)
            mock_redis.zcard = AsyncMock(return_value=2)
            mock_redis.zrevrange = AsyncMock(return_value=history_entries)
            mock_get_redis.return_value = mock_redis

            from middleware.g02_template_registry import G02TemplateRegistry
            ctx = await G02TemplateRegistry().process_request(ctx)

        step = ctx.savings.step_savings[0]
        assert "avg=50" in step.description
        assert "n=2" in step.description

    async def test_record_token_history_trims_beyond_1000(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )

        with patch("middleware.g02_template_registry._get_redis") as mock_get_redis:
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value=None)
            mock_redis.set = AsyncMock(return_value=True)
            mock_redis.zadd = AsyncMock(return_value=1)
            mock_redis.expire = AsyncMock(return_value=True)
            mock_redis.zcard = AsyncMock(return_value=1001)
            mock_redis.zremrangebyrank = AsyncMock(return_value=1)
            mock_redis.zrevrange = AsyncMock(return_value=[])
            mock_get_redis.return_value = mock_redis

            from middleware.g02_template_registry import G02TemplateRegistry
            ctx = await G02TemplateRegistry().process_request(ctx)

        mock_redis.zremrangebyrank.assert_awaited_once_with(
            "tok_opt:template:history:test-template:1.0", 0, 0
        )


class TestTemplateMetadataDeprecation:
    """Direct unit tests for TemplateMetadata.get_deprecation_status()."""

    def test_active_template_with_no_deprecation_fields(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(template_id="t1")
        status, days_remaining, message = meta.get_deprecation_status()
        assert status == "ACTIVE"
        assert days_remaining == -1

    def test_sunset_template_returns_sunset_status(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(template_id="t1", sunset_at=time.time() - 86400)
        status, days_remaining, message = meta.get_deprecation_status()
        assert status == "SUNSET"
        assert days_remaining == 0
        assert "no longer supported" in message

    def test_deprecation_warning_within_window_with_replacement(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(
            template_id="t1",
            deprecated_at=time.time() - 86400,
            sunset_at=time.time() + (10 * 86400),  # 10 days remaining
            replaced_by="t2",
        )
        status, days_remaining, message = meta.get_deprecation_status()
        assert status == "DEPRECATION_WARNING"
        assert 9 <= days_remaining <= 10
        assert "Migrate to t2" in message

    def test_deprecation_warning_within_window_without_replacement(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(
            template_id="t1",
            deprecated_at=time.time() - 86400,
            sunset_at=time.time() + (5 * 86400),
        )
        status, days_remaining, message = meta.get_deprecation_status()
        assert status == "DEPRECATION_WARNING"
        assert "Migrate to" not in message

    def test_deprecated_but_outside_warning_window(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(
            template_id="t1",
            deprecated_at=time.time() - 86400,
            sunset_at=time.time() + (60 * 86400),  # 60 days remaining > 30-day window
        )
        status, days_remaining, message = meta.get_deprecation_status()
        assert status == "DEPRECATED"
        assert days_remaining == -1

    def test_deprecated_without_sunset(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(template_id="t1", deprecated_at=time.time() - 86400)
        status, days_remaining, message = meta.get_deprecation_status()
        assert status == "DEPRECATED"
        assert days_remaining == -1

    def test_to_dict_and_from_dict_round_trip(self):
        from middleware.g02_template_registry import TemplateMetadata
        meta = TemplateMetadata(
            template_id="t1",
            version="2.0",
            deprecated_at=12345.0,
            sunset_at=67890.0,
            replaced_by="t2",
            author="alice",
            description="test template",
        )
        restored = TemplateMetadata.from_dict(meta.to_dict())
        assert restored.template_id == meta.template_id
        assert restored.version == meta.version
        assert restored.deprecated_at == meta.deprecated_at
        assert restored.sunset_at == meta.sunset_at
        assert restored.replaced_by == meta.replaced_by
        assert restored.author == meta.author
        assert restored.description == meta.description


@pytest.mark.asyncio
class TestG02BudgetTruncation:
    """Item 83(c) — the `budget.truncate_*` enforcement path (previously untested)."""

    _LONG_SYSTEM = "You are a very detailed and extremely thorough assistant. " * 10

    async def test_truncate_enabled_shrinks_over_budget_messages(self, make_ctx):
        """budget.truncate_enabled=True → over-budget messages are truncated and a
        'truncated to budget' savings step is recorded."""
        ctx = make_ctx(
            [{"role": "system", "content": self._LONG_SYSTEM}, {"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )
        ctx.config["groups"]["G2_template_registry"]["budget"] = {
            "truncate_enabled": True,
            "truncate_strategy": "tail_system",
            "min_keep_user_turns": 1,
        }
        original_system_len = len(ctx.messages[0]["content"])

        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)

        # system prompt was trimmed to fit the budget
        assert len(ctx.messages[0]["content"]) < original_system_len
        # last user turn is preserved (min_keep_user_turns=1)
        assert ctx.messages[-1] == {"role": "user", "content": "hi"}
        descs = [s.description for s in ctx.savings.step_savings]
        assert any("truncated to budget" in d for d in descs)

    async def test_truncate_disabled_leaves_messages_untouched(self, make_ctx):
        """Default (truncate_enabled absent/False) → OVER warning only; messages unchanged."""
        ctx = make_ctx(
            [{"role": "system", "content": self._LONG_SYSTEM}, {"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )
        # no budget block at all → truncate_enabled defaults False
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)

        assert ctx.messages[0]["content"] == self._LONG_SYSTEM  # untouched
        descs = [s.description for s in ctx.savings.step_savings]
        assert any("OVER" in d for d in descs)
        assert not any("truncated" in d for d in descs)

    async def test_truncate_strategy_and_min_keep_read_from_config(self, make_ctx):
        """The strategy + min_keep_user_turns are read config-first (not hardcoded):
        min_keep_user_turns=2 keeps both user turns."""
        ctx = make_ctx(
            [
                {"role": "system", "content": self._LONG_SYSTEM},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"},
            ],
            params={"template_id": "test-template"},
        )
        ctx.config["groups"]["G2_template_registry"]["budget"] = {
            "truncate_enabled": True,
            "truncate_strategy": "tail_system",
            "min_keep_user_turns": 2,
        }
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)

        user_contents = [m["content"] for m in ctx.messages if m.get("role") == "user"]
        assert "first" in user_contents and "second" in user_contents
