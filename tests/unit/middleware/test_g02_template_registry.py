"""Unit tests for G02 — Prompt Template Registry."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import copy
import json
import time
from pathlib import Path

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
class TestG02NeverEditsTheRequest:
    """G02 is a read-only budget OBSERVER (D-056, 2026-09-08).

    The class this replaces (`TestG02BudgetTruncation`, item 83c) pinned the opt-in
    `budget.truncate_*` path, which cut the tail off the caller's system prompt until the
    request fit `total_input_max`. Measured on DS1 it removed 691 characters of policy text
    per request and CHANGED THE ANSWERS on billed 200s - a refund reply stopped naming the
    disputed amount, an SLA reply stopped naming the breach - while the source text for both
    facts was still in the prompt it sent. The path was deleted, not guarded, so these tests
    now pin the OPPOSITE contract: over budget, G02 warns and forwards the request unchanged.
    """

    _LONG_SYSTEM = "You are a very detailed and extremely thorough assistant. " * 10

    async def test_over_budget_request_is_forwarded_byte_identical(self, make_ctx):
        """Over budget -> a zero-saving OVER step, and not one byte of the request changes."""
        ctx = make_ctx(
            [{"role": "system", "content": self._LONG_SYSTEM}, {"role": "user", "content": "hi"}],
            params={"template_id": "test-template"},
        )
        before = copy.deepcopy(ctx.messages)

        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)

        assert ctx.messages == before
        assert ctx.messages[0]["content"] == self._LONG_SYSTEM
        steps = ctx.savings.step_savings
        assert len(steps) == 1 and steps[0].group == "G02"
        assert "OVER" in steps[0].description
        assert not any("truncat" in s.description.lower() for s in steps)
        # report-only: the step must claim no saving at all
        assert steps[0].tokens_before == steps[0].tokens_after

    async def test_legacy_truncate_knobs_are_inert(self, make_ctx):
        """A config left over from before the removal must not resurrect the behaviour.

        An operator (or a stale GCS config) can still carry `budget.truncate_enabled: true`.
        Nothing reads it any more, and this asserts that stays true.
        """
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
            "min_keep_user_turns": 1,
        }
        before = copy.deepcopy(ctx.messages)

        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)

        assert ctx.messages == before
        assert not any("truncat" in s.description.lower() for s in ctx.savings.step_savings)

    async def test_multiple_system_messages_all_survive(self, make_ctx):
        """The deleted second pass emptied EVERY system message (its guard counted roles,
        which emptying never changes), despite a comment promising to keep one. Pinned so a
        reintroduction cannot pass silently."""
        ctx = make_ctx(
            [
                {"role": "system", "content": self._LONG_SYSTEM},
                {"role": "system", "content": self._LONG_SYSTEM},
                {"role": "user", "content": "hi"},
            ],
            params={"template_id": "test-template"},
        )
        from middleware.g02_template_registry import G02TemplateRegistry
        ctx = await G02TemplateRegistry().process_request(ctx)

        assert [m["content"] for m in ctx.messages if m["role"] == "system"] == [
            self._LONG_SYSTEM,
            self._LONG_SYSTEM,
        ]


class TestG02SourceContract:
    """Source-inspection pins - the mutation path must not come back by any door."""

    @staticmethod
    def _source() -> str:
        import middleware.g02_template_registry as mod
        return Path(mod.__file__).read_text(encoding="utf-8")

    def test_no_code_path_edits_messages(self):
        src = self._source()
        assert "_truncate_messages" not in src
        assert "ctx.messages =" not in src
        assert "ctx.messages[" not in src
        # in-place mutation of a message dict
        assert '["content"] =' not in src and "['content'] =" not in src
        # W17-R-G02 F7: rebinding and item assignment are not the only doors. `ctx.messages`
        # is a list and every message is a dict, so a list method or a dict `.update(` would
        # rewrite the request while sailing past the checks above.
        for door in ("ctx.messages.pop", "ctx.messages.append", "ctx.messages.insert",
                     "ctx.messages.extend", "ctx.messages.clear", "ctx.messages.remove",
                     "ctx.messages.sort", "ctx.messages.reverse", ".update("):
            assert door not in src, f"{door} would let G02 edit the request"

    def test_mutation_only_imports_are_absent(self):
        """`cache_floor` existed only to re-snapshot G02's rewrite and the token counter only
        to measure it. Their absence is the cheapest signal that no rewrite exists."""
        src = self._source()
        assert "cache_floor" not in src.replace("`cache_floor`", "")
        assert "count_messages_tokens" not in src

    def test_removed_knobs_are_gone_from_the_config_template(self):
        import yaml
        root = Path(__file__).resolve().parents[3]
        cfg = yaml.safe_load((root / "config" / "config.yaml.template").read_text(encoding="utf-8"))
        g2 = cfg["groups"]["G2_template_registry"]
        assert "budget" not in g2, "the singular `budget` truncation block must stay removed"
        flat = yaml.safe_dump(g2)
        for knob in ("truncate_enabled", "truncate_strategy", "min_keep_user_turns",
                     "output_max"):
            assert knob not in flat, f"{knob} was removed on 2026-09-08 and must not return"
        # the budget that IS still read - and warned against - stays
        assert all("total_input_max" in b for b in g2["budgets"].values())

    def test_system_prompt_max_survives_because_ci_reads_it(self):
        """`system_prompt_max` is NOT a dead key and must not be tidied away with the runtime
        truncation knobs. scripts/ci/validate-templates.sh and scripts/ci/pr-diff-token-check.py
        both read it out of config.yaml.template to block a template whose system prompt has
        outgrown its registration. Deleting the key does not raise the limit for either:
        validate-templates.sh treats a missing value as "no budget" and SKIPS the check, while
        pr-diff-token-check.py falls back to its own hard default of 500 — a different limit
        from the registered one (stricter, for this template's 400)."""
        import yaml
        root = Path(__file__).resolve().parents[3]
        cfg = yaml.safe_load((root / "config" / "config.yaml.template").read_text(encoding="utf-8"))
        budgets = cfg["groups"]["G2_template_registry"]["budgets"]
        assert all("system_prompt_max" in b for b in budgets.values())
        ci = root / "scripts" / "ci"
        # validate-templates.sh: missing -> 0 -> the `if max_system and ...` guard never fires
        validate = (ci / "validate-templates.sh").read_text(encoding="utf-8")
        assert 'budget.get("system_prompt_max", 0)' in validate, (
            "validate-templates.sh no longer reads it — re-check before removing the key")
        # pr-diff-token-check.py: missing -> its OWN default, not "no budget"
        prdiff = (ci / "pr-diff-token-check.py").read_text(encoding="utf-8")
        assert 'budget["system_prompt_max"]' in prdiff, (
            "pr-diff-token-check.py no longer reads it — re-check before removing the key")
        assert '"system_prompt_max": 500' in prdiff, (
            "the hard fallback documented in config.yaml.template / config-reference has "
            "changed — update both before this test is edited")
