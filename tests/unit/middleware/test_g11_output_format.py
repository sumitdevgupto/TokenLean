"""Unit tests for G11 — Output Length & Format Control."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
class TestG11OutputFormat:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["enabled"] = False
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" not in ctx.params

    async def test_injects_max_tokens_when_absent(self, make_ctx):
        ctx = make_ctx()
        assert "max_tokens" not in ctx.params
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" in ctx.params
        assert ctx.params["max_tokens"] > 0

    async def test_does_not_override_existing_max_tokens(self, make_ctx):
        ctx = make_ctx(params={"max_tokens": 42})
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] == 42

    async def test_skips_max_tokens_for_reasoning_model(self, make_ctx):
        """Reasoning models (routed_model=o3-mini → adapter.supports_reasoning) spend
        max_tokens on hidden reasoning; tightening it empties the visible answer.
        G11 must NOT set max_tokens for them."""
        ctx = make_ctx(model="o3-mini")
        assert "max_tokens" not in ctx.params
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" not in ctx.params  # enforcement skipped for reasoning model

    async def test_reasoning_skip_can_be_disabled_via_config(self, make_ctx):
        ctx = make_ctx(model="o3-mini")
        ctx.config["groups"]["G11_output"]["skip_max_tokens_for_reasoning"] = False
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" in ctx.params  # explicit opt-out → tighten anyway

    async def test_non_reasoning_model_still_gets_max_tokens(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")  # not a reasoning model
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" in ctx.params  # unchanged behaviour for normal models

    async def test_records_step_saving(self, make_ctx):
        ctx = make_ctx()
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert any(s.group == "G11" for s in ctx.savings.step_savings)

    async def test_injects_json_response_format_on_x_json_output(self, make_ctx):
        ctx = make_ctx(params={"x_json_output": True})
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params.get("response_format", {}).get("type") == "json_object"

    async def test_force_json_for_all_config(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["force_json_for_all"] = True
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params.get("response_format", {}).get("type") == "json_object"

    async def test_max_tokens_capped_at_absolute_default(self, make_ctx):
        # Even with a very long input, max_tokens should not exceed 1024.
        # auto_tighten is on by default (minimal_config) so this must force
        # the historical-lookup path to miss — otherwise, against a real
        # local Redis with leftover max_tokens_history data from other runs,
        # this test becomes order/environment-dependent (it picks up stale
        # historical data and takes the tightening branch instead of the
        # absolute-cap heuristic branch this test exists to verify).
        ctx = make_ctx([{"role": "user", "content": "word " * 2000}])
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis in this test")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] <= 1024

    async def test_absolute_default_max_tokens_config_override(self, make_ctx):
        # absolute_default_max_tokens is now a template knob; overriding it must move the cap.
        ctx = make_ctx([{"role": "user", "content": "word " * 2000}])
        ctx.config["groups"]["G11_output"]["absolute_default_max_tokens"] = 256
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis in this test")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] <= 256

    async def test_p95_historical_tightens_max_tokens(self, make_ctx):
        """With 10 mocked Redis entries, assert max_tokens is tightened to p95×1.2."""
        # Build 10 Redis ZSET entries with max_tokens = 100..190
        mock_entries = [
            json.dumps({"max_tokens": 100 + i * 10, "completion_tokens": 50 + i * 5})
            for i in range(10)
        ]
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=mock_entries)

        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.config["groups"]["G11_output"]["tighten_quantile"] = 0.95
        ctx.config["groups"]["G11_output"]["tighten_multiplier"] = 1.2
        ctx.params["workflow_id"] = "wf-test"
        ctx.params["template_id"] = "tmpl-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        # p95 of [100, 110, 120, 130, 140, 150, 160, 170, 180, 190] = index 8 = 180
        # 180 * 1.2 = 216
        expected = max(64, int(180 * 1.2))
        assert ctx.params["max_tokens"] == expected
        mock_redis.zrevrange.assert_called_once()

    async def test_insufficient_history_falls_back_to_multiplier(self, make_ctx):
        """With fewer than 5 Redis entries, fall back to static multiplier."""
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=[])

        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-test"
        ctx.params["template_id"] = "tmpl-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        # Should use static multiplier fallback, not p95
        assert "max_tokens" in ctx.params
        assert ctx.params["max_tokens"] > 0

    async def test_process_response_records_to_redis(self, make_ctx):
        """process_response should record (max_tokens, completion_tokens) to Redis ZSET."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.params["max_tokens"] = 256
        ctx.params["workflow_id"] = "wf-test"
        ctx.params["template_id"] = "tmpl-test"

        response = {"usage": {"completion_tokens": 128}}

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        assert mock_redis.zadd.called
        # Verify the ZADD member contains our max_tokens and completion_tokens
        # redis.asyncio zadd(key, {member: score}) — positional args
        zadd_call = mock_redis.zadd.call_args
        mapping = zadd_call.args[1] if len(zadd_call.args) > 1 else zadd_call.kwargs.get("mapping", {})
        member = list(mapping.keys())[0]
        data = json.loads(member)
        assert data["max_tokens"] == 256
        assert data["completion_tokens"] == 128
        mock_redis.expire.assert_called_once()

    async def test_process_response_feedback_loop_disabled_skips_recording(self, make_ctx):
        """When max_tokens_feedback_loop is false, process_response should skip recording."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = False
        ctx.params["max_tokens"] = 256

        response = {"usage": {"completion_tokens": 128}}

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        mock_redis.zadd.assert_not_called()


@pytest.mark.asyncio
class TestG11OutputHoldout:
    """A3 — output-savings holdout: a control cohort skips G11 shaping so the
    real output-token reduction can be measured (treatment vs holdout)."""

    @staticmethod
    def _enable_holdout(ctx, fraction, sticky_key="workflow_id"):
        ctx.config["groups"]["G11_output"]["output_holdout"] = {
            "enabled": True, "fraction": fraction, "sticky_key": sticky_key,
        }

    async def test_holdout_disabled_applies_shaping(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" in ctx.params          # shaping applied (default behaviour)
        assert "_g11_cohort" not in ctx.params      # no cohort assigned when disabled

    async def test_holdout_full_skips_shaping(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        self._enable_holdout(ctx, 1.0)
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["_g11_cohort"] == "holdout"
        assert "max_tokens" not in ctx.params       # shaping skipped for the control cohort

    async def test_holdout_zero_fraction_is_treatment(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        self._enable_holdout(ctx, 0.0)
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["_g11_cohort"] == "treatment"
        assert "max_tokens" in ctx.params           # shaping applied

    async def test_cohort_assignment_is_deterministic_and_sticky(self, make_ctx):
        from middleware.g11_output_format import _assign_cohort
        cfg = {"enabled": True, "fraction": 0.5, "sticky_key": "workflow_id"}
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-sticky-123"
        first = _assign_cohort(ctx, cfg)
        assert first in ("holdout", "treatment")
        assert all(_assign_cohort(ctx, cfg) == first for _ in range(5))  # sticky/deterministic

    async def test_holdout_preserves_structured_output(self, make_ctx):
        """Even in the control cohort, correctness (JSON response_format) is NOT skipped."""
        ctx = make_ctx(model="gpt-4o")
        self._enable_holdout(ctx, 1.0)
        ctx.config["groups"]["G11_output"]["force_json_for_all"] = True
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["_g11_cohort"] == "holdout"
        assert "max_tokens" not in ctx.params                                       # shaping skipped
        assert ctx.params.get("response_format", {}).get("type") == "json_object"   # correctness kept

    async def test_process_response_emits_cohort_metric(self, make_ctx):
        ctx = make_ctx()
        self._enable_holdout(ctx, 1.0)
        ctx.params["_g11_cohort"] = "holdout"
        response = {"usage": {"completion_tokens": 137}}
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g18_observability.OUTPUT_HOLDOUT_COMPLETION_TOKENS") as mock_metric:
            ctx, resp = await G11OutputFormat().process_response(ctx, response)
        mock_metric.labels.assert_called_once()
        assert mock_metric.labels.call_args.kwargs["cohort"] == "holdout"
        mock_metric.labels.return_value.observe.assert_called_once_with(137)

    async def test_process_response_no_metric_when_holdout_disabled(self, make_ctx):
        ctx = make_ctx()  # output_holdout not configured
        ctx.params["_g11_cohort"] = "treatment"
        response = {"usage": {"completion_tokens": 50}}
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g18_observability.OUTPUT_HOLDOUT_COMPLETION_TOKENS") as mock_metric:
            ctx, resp = await G11OutputFormat().process_response(ctx, response)
        mock_metric.labels.assert_not_called()


# ── Task 4: output JSON-schema validation ─────────────────────────────────────
def _resp(content):
    """A minimal OpenAI chat-completion with one text answer."""
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"completion_tokens": 12},
    }


_SCHEMA_RF = {
    "type": "json_schema",
    "json_schema": {"schema": {
        "type": "object", "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }},
}


@pytest.mark.asyncio
class TestG11OutputValidation:
    """Task 4 — validate a structured-output answer is parseable JSON (and schema-valid)."""

    async def test_off_by_default_is_passthrough(self, make_ctx):
        ctx = make_ctx(params={"response_format": {"type": "json_object"}})
        # validate_output unset → defaults to off
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("not json at all"))
        m.assert_not_called()
        assert resp["choices"][0]["message"]["content"] == "not json at all"
        assert "_token_opt" not in resp

    async def test_no_response_format_skips_validation(self, make_ctx):
        ctx = make_ctx()  # no response_format → nothing to validate even in flag mode
        ctx.config["groups"]["G11_output"]["validate_output"] = "flag"
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("plain prose"))
        m.assert_not_called()

    async def test_valid_json_object_passes(self, make_ctx):
        ctx = make_ctx(params={"response_format": {"type": "json_object"}})
        ctx.config["groups"]["G11_output"]["validate_output"] = "flag"
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp('{"ok": true}'))
        m.assert_not_called()
        assert resp.get("_token_opt", {}).get("output_validation") is None

    async def test_flag_mode_records_and_annotates_on_bad_json(self, make_ctx):
        ctx = make_ctx(params={"response_format": {"type": "json_object"}})
        ctx.config["groups"]["G11_output"]["validate_output"] = "flag"
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("this is not json"))
        m.assert_called_once()
        assert m.call_args.args[1] == "flag"
        assert resp["_token_opt"]["output_validation"]["validated"] is False
        # answer is unchanged in flag mode
        assert resp["choices"][0]["message"]["content"] == "this is not json"

    async def test_schema_missing_required_field_detected(self, make_ctx):
        ctx = make_ctx(params={"response_format": _SCHEMA_RF})
        ctx.config["groups"]["G11_output"]["validate_output"] = "flag"
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            # valid JSON, but missing the required "name" field
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp('{"age": 5}'))
        m.assert_called_once()
        assert resp["_token_opt"]["output_validation"]["validated"] is False

    async def test_schema_satisfied_passes(self, make_ctx):
        ctx = make_ctx(params={"response_format": _SCHEMA_RF})
        ctx.config["groups"]["G11_output"]["validate_output"] = "flag"
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp('{"name": "Ada"}'))
        m.assert_not_called()

    async def test_block_mode_withholds_bad_json(self, make_ctx):
        ctx = make_ctx(params={"response_format": {"type": "json_object"}})
        ctx.config["groups"]["G11_output"]["validate_output"] = "block"
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("nope"))
        m.assert_called_once()
        assert resp["choices"][0]["finish_reason"] == "content_filter"
        assert ctx.no_cache is True

    async def test_repair_success_replaces_answer(self, make_ctx):
        ctx = make_ctx(params={"response_format": _SCHEMA_RF})
        ctx.config["groups"]["G11_output"]["validate_output"] = "repair"
        from middleware.g11_output_format import G11OutputFormat
        # I4: mocked single re-ask returning a repaired, schema-valid answer
        reask = AsyncMock(return_value='{"name": "Grace"}')
        with patch("middleware.g11_output_format._reask", reask), \
             patch("middleware.quality_metrics.record_schema_failure"):
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("broken"))
        reask.assert_awaited_once()          # exactly ONE re-ask, no loop
        assert resp["choices"][0]["message"]["content"] == '{"name": "Grace"}'
        assert resp["_token_opt"]["output_validation"] == {"validated": True, "repaired": True}

    async def test_repair_still_invalid_falls_back_to_flag_no_loop(self, make_ctx):
        ctx = make_ctx(params={"response_format": _SCHEMA_RF})
        ctx.config["groups"]["G11_output"]["validate_output"] = "repair"  # repair_fallback defaults to flag
        from middleware.g11_output_format import G11OutputFormat
        reask = AsyncMock(return_value="still not json")   # re-ask fails to repair
        with patch("middleware.g11_output_format._reask", reask), \
             patch("middleware.quality_metrics.record_schema_failure"):
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("broken"))
        reask.assert_awaited_once()          # NO second re-ask
        assert resp["_token_opt"]["output_validation"]["repaired"] is False
        assert resp["choices"][0].get("finish_reason") != "content_filter"   # flag fallback, not block

    async def test_repair_fallback_block(self, make_ctx):
        ctx = make_ctx(params={"response_format": _SCHEMA_RF})
        ctx.config["groups"]["G11_output"]["validate_output"] = "repair"
        ctx.config["groups"]["G11_output"]["repair_fallback"] = "block"
        from middleware.g11_output_format import G11OutputFormat
        reask = AsyncMock(return_value=None)   # re-ask errored out
        with patch("middleware.g11_output_format._reask", reask), \
             patch("middleware.quality_metrics.record_schema_failure"):
            ctx, resp = await G11OutputFormat().process_response(ctx, _resp("broken"))
        reask.assert_awaited_once()
        assert resp["choices"][0]["finish_reason"] == "content_filter"

    async def test_tool_call_answer_not_validated(self, make_ctx):
        # A tool-call answer has content=None → not a JSON text answer, skip validation.
        ctx = make_ctx(params={"response_format": {"type": "json_object"}})
        ctx.config["groups"]["G11_output"]["validate_output"] = "block"
        resp_in = {"choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c1"}]}}], "usage": {"completion_tokens": 3}}
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.quality_metrics.record_schema_failure") as m:
            ctx, resp = await G11OutputFormat().process_response(ctx, resp_in)
        m.assert_not_called()
        assert resp["choices"][0]["message"].get("finish_reason") is None


# ─── Verbosity steering — terse-output presets + cache scoping ────────────────
from middleware.g11_output_format import (
    _get_verbosity_suffix,
    _VERBOSITY_PRESETS,
    verbosity_cache_tag,
)


def _last_system(messages):
    for m in reversed(messages):
        if m.get("role") == "system":
            return m["content"]
    return None


class TestVerbositySuffixResolver:
    def test_level_selects_bundled_preset(self):
        assert _get_verbosity_suffix("t", {"level": "full"}) == _VERBOSITY_PRESETS["full"]
        assert _get_verbosity_suffix("t", {"level": "ultra"}) == _VERBOSITY_PRESETS["ultra"]
        assert _get_verbosity_suffix("t", {"level": "lite"}) == _VERBOSITY_PRESETS["lite"]

    def test_unset_or_unknown_level_returns_none(self):
        assert _get_verbosity_suffix("t", {}) is None
        assert _get_verbosity_suffix("t", {"level": "bogus"}) is None

    def test_explicit_default_suffix_overrides_preset(self):
        got = _get_verbosity_suffix("t", {"level": "full", "default_suffix": "TERSE."})
        assert got == "TERSE."

    def test_per_tenant_suffix_wins(self):
        cfg = {"level": "ultra", "default_suffix": "X", "per_tenant_suffix": {"acme": "ACME"}}
        assert _get_verbosity_suffix("acme", cfg) == "ACME"
        assert _get_verbosity_suffix("other", cfg) == "X"

    def test_presets_have_safety_carveout(self):
        # full/ultra must keep security/destructive-action text in normal prose
        for level in ("full", "ultra"):
            low = _VERBOSITY_PRESETS[level].lower()
            assert "security" in low and ("destructive" in low or "irreversible" in low)


@pytest.mark.asyncio
class TestVerbositySteeringInjection:
    async def test_default_off_is_byte_identical(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["verbosity_steering"] = {"enabled": False, "level": "full"}
        before = list(ctx.messages)
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.messages == before  # nothing injected when steering disabled

    async def test_enabled_injects_preset_into_system(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["verbosity_steering"] = {"enabled": True, "level": "full"}
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert _VERBOSITY_PRESETS["full"] in (_last_system(ctx.messages) or "")

    async def test_suffix_appended_at_end_of_existing_system(self, make_ctx):
        ctx = make_ctx([
            {"role": "system", "content": "BASE POLICY"},
            {"role": "user", "content": "hi"},
        ])
        ctx.config["groups"]["G11_output"]["verbosity_steering"] = {"enabled": True, "level": "lite"}
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        sysmsg = _last_system(ctx.messages)
        assert sysmsg.startswith("BASE POLICY")  # prefix preserved → G21 cache-safe
        assert _VERBOSITY_PRESETS["lite"] in sysmsg


class TestVerbosityCacheTag:
    def _ctx(self, vs, enabled=True, tenant="t"):
        class _C:
            config = {"groups": {"G11_output": {"enabled": enabled, "verbosity_steering": vs}}}
            tenant_id = tenant
        return _C()

    def test_off_returns_empty(self):
        assert verbosity_cache_tag(self._ctx({"enabled": False})) == ""
        assert verbosity_cache_tag(self._ctx({"enabled": True, "level": ""})) == ""

    def test_on_returns_stable_nonempty(self):
        c = self._ctx({"enabled": True, "level": "full"})
        assert verbosity_cache_tag(c) and verbosity_cache_tag(c) == verbosity_cache_tag(c)

    def test_different_levels_differ(self):
        a = verbosity_cache_tag(self._ctx({"enabled": True, "level": "full"}))
        b = verbosity_cache_tag(self._ctx({"enabled": True, "level": "ultra"}))
        assert a != b

    def test_group_disabled_returns_empty(self):
        assert verbosity_cache_tag(self._ctx({"enabled": True, "level": "full"}, enabled=False)) == ""
