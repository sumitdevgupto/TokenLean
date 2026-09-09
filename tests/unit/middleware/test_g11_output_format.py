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

    async def test_no_evidence_leaves_max_tokens_unset(self, make_ctx):
        """DS18 cold-start regression (2026-08-09): output length is NOT derivable
        from input length — a ~200-token question can need a ~1300-token proof.
        With no completion-size history and no configured fallback, G11 must not
        invent a cap (the old input-proportional guess capped such answers at 128
        and the truncated completions then re-taught the low cap, permanently)."""
        ctx = make_ctx()
        assert "max_tokens" not in ctx.params
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" not in ctx.params

    async def test_does_not_override_existing_max_tokens(self, make_ctx):
        ctx = make_ctx(params={"max_tokens": 42})
        from middleware.g11_output_format import G11OutputFormat
        ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] == 42

    async def test_skips_max_tokens_for_reasoning_model(self, make_ctx):
        """Reasoning models (routed_model=o3-mini → adapter.supports_reasoning) spend
        max_tokens on hidden reasoning; tightening it empties the visible answer.
        G11 must NOT set max_tokens for them — even when a fallback cap is
        configured (so the absence below proves the reasoning skip, not the
        no-evidence skip)."""
        ctx = make_ctx(model="o3-mini")
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        assert "max_tokens" not in ctx.params
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" not in ctx.params  # enforcement skipped for reasoning model

    async def test_reasoning_skip_can_be_disabled_via_config(self, make_ctx):
        ctx = make_ctx(model="o3-mini")
        ctx.config["groups"]["G11_output"]["skip_max_tokens_for_reasoning"] = False
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] == 512  # explicit opt-out → enforcement anyway

    async def test_non_reasoning_model_still_gets_max_tokens(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")  # not a reasoning model
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] == 512  # unchanged behaviour for normal models

    async def test_records_step_saving(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert any(s.group == "G11" for s in ctx.savings.step_savings)

    async def test_no_evidence_skip_records_no_step_saving(self, make_ctx):
        """Fire-only honesty: when G11 declines to set a cap it must not claim a
        savings step for it."""
        ctx = make_ctx()
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert not any(s.group == "G11" for s in ctx.savings.step_savings)

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

    async def test_fallback_max_tokens_respects_model_limit(self, make_ctx):
        # A configured fallback larger than the model limit must be clamped to it
        # (prevents 502s from providers rejecting over-limit max_tokens).
        ctx = make_ctx([{"role": "user", "content": "word " * 2000}])
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 999999
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis in this test")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] == 4096  # default_model_max_tokens clamp

    async def test_fallback_max_tokens_config_applies(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "word " * 2000}])
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 256
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis in this test")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["max_tokens"] == 256

    async def test_fallback_max_tokens_bool_is_ignored(self, make_ctx):
        # YAML `fallback_max_tokens: true` must not become max_tokens=1.
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = True
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis in this test")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" not in ctx.params

    async def test_p95_historical_tightens_max_tokens(self, make_ctx):
        """With 10 mocked Redis entries, max_tokens = completion-p95 × 1.2.

        The evidence is the OBSERVED completion sizes, never the caps that were
        applied — a p95 over past caps only echoes past caps back (that echo
        chamber is how a bad 128 cap re-taught itself in the DS18 incident)."""
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

        # completions are [50, 55, ..., 95]; p95 index 8 → 90; 90 × 1.2 = 108.
        # (The caps in the entries are 100..190 — if the implementation regressed
        # to reading caps, this would come out 216 and fail.)
        expected = max(64, int(90 * 1.2))
        assert ctx.params["max_tokens"] == expected
        mock_redis.zrevrange.assert_called_once()

    async def test_truncated_history_entries_escalate_the_estimate(self, make_ctx):
        """A truncation only proves the answer wanted MORE than the cap: entries
        marked `truncated` must count as completion × truncation_backoff so the
        cap climbs out of a bad guess instead of re-learning it."""
        mock_entries = [
            json.dumps({"max_tokens": 128, "completion_tokens": 128, "truncated": True})
            for _ in range(5)
        ]
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=mock_entries)

        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.config["groups"]["G11_output"]["truncation_backoff_multiplier"] = 2.0
        # Pinned explicitly (2026-09-08): the shipped `tighten_multiplier` default moved
        # 1.2 → 2.0, and this case is about the BACKOFF, not the multiplier. Setting it
        # here keeps the assertion testing exactly what it always tested; the new default
        # is pinned separately by test_default_tighten_multiplier_is_two.
        ctx.config["groups"]["G11_output"]["tighten_multiplier"] = 1.2
        ctx.params["workflow_id"] = "wf-test"
        ctx.params["template_id"] = "tmpl-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        # 128 × 2.0 backoff = 256 evidence → 256 × 1.2 tighten = 307 (> the 128
        # that a cap-echo implementation would keep applying forever).
        assert ctx.params["max_tokens"] == int(256 * 1.2)

    async def test_insufficient_history_leaves_max_tokens_unset(self, make_ctx):
        """With no usable history and no fallback configured, G11 applies no cap."""
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=[])

        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-test"
        ctx.params["template_id"] = "tmpl-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert "max_tokens" not in ctx.params

    # ── 2026-09-08 truncation defect (tracker 23.12) ──────────────────────────
    # G11 capped answers from a bucket shared by every workload a tenant runs, read as a
    # 10-entry sliding window with 20% headroom. Live: 4 of 54 DS1 answers and 6 of 27
    # readiness probes came back finish_reason=length on billed 200s, and the arm sent
    # byte-identical INPUT tokens — the whole observable effect was missing answer content.

    @staticmethod
    def _history(mock_redis, completions):
        mock_redis.zrevrange = AsyncMock(return_value=[
            json.dumps({"max_tokens": 0, "completion_tokens": c}) for c in completions
        ])
        mock_redis.get = AsyncMock(return_value=None)
        return mock_redis

    async def test_catch_all_bucket_is_never_capped(self, make_ctx):
        """A request that identifies NO workload must not be capped. Both ids default to
        "default"; ordinary traffic, every readiness probe and every pitch dataset set
        neither, so the only evidence available is other workloads' answers — which is
        exactly how a long-form answer got a short-form cap."""
        mock_redis = self._history(AsyncMock(), [200] * 10)
        ctx = make_ctx()  # no workflow_id, no template_id
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert "max_tokens" not in ctx.params
        mock_redis.zrevrange.assert_not_called()  # the bucket is not even consulted

    async def test_one_id_is_enough_to_discriminate_a_bucket(self, make_ctx):
        """Only the both-absent case is the catch-all; a caller who sets either id has
        told us which work this is."""
        mock_redis = self._history(AsyncMock(), [100] * 10)
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["template_id"] = "tmpl-only"
        # tests/conftest.py's minimal_config still pins tighten_multiplier 1.2 (the
        # pre-2026-09-08 value) and is outside this change's scope, so the three cases
        # below that are NOT about the default set it explicitly.
        ctx.config["groups"]["G11_output"]["tighten_multiplier"] = 2.0

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 200  # 100 × the 2.0 default multiplier

    async def test_whole_retained_history_is_read_not_a_sliding_window(self, make_ctx):
        """`zrevrange(key, 0, -1)`. Reading `0, min_entries*2-1` was the amplifier: over 10
        samples int((10-1)*0.95) is index 8 — the second largest — and any long answer aged
        out after ten requests, which is also what discarded the truncation escalation."""
        mock_redis = self._history(AsyncMock(), [100] * 6 + [900])
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            await G11OutputFormat().process_request(ctx)

        args, _ = mock_redis.zrevrange.call_args
        assert args[1] == 0 and args[2] == -1, f"read a window, not the history: {args}"

    async def test_default_tighten_multiplier_is_two(self, make_ctx):
        """1.2 was BELOW the model's own same-prompt spread: on DS1 at temperature 0 the
        same prompt exceeded 1.2× another repeat's length in 15 of 108 ordered pairs, with
        a per-request spread up to 2.03×. A margin under the observed variance truncates
        answers the model has already been seen to produce."""
        mock_redis = self._history(AsyncMock(), [150] * 10)
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.config["groups"]["G11_output"].pop("tighten_multiplier", None)
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 300

    async def test_sticky_floor_raises_a_cap_the_percentile_would_undercut(self, make_ctx):
        """The floor is what makes the loop converge. On DS1 ds1-08 was cut at 232, the
        escalated entry pushed the cap to 256, then aged out and the cap fell to 224 and cut
        the same request again. A floor is monotone within its TTL, so that cannot recur."""
        mock_redis = self._history(AsyncMock(), [100] * 10)
        mock_redis.get = AsyncMock(return_value=b"512")
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 512  # not the 200 the percentile wanted

    async def test_sticky_floor_never_lowers_a_cap(self, make_ctx):
        """It is a floor, not an override: evidence above it still wins."""
        mock_redis = self._history(AsyncMock(), [400] * 10)
        mock_redis.get = AsyncMock(return_value=b"100")
        ctx = make_ctx()
        # tests/conftest.py's minimal_config still pins tighten_multiplier 1.2 (the
        # pre-2026-09-08 value) and is outside this change's scope, so the cases here
        # that are NOT about the default set it explicitly.
        ctx.config["groups"]["G11_output"]["tighten_multiplier"] = 2.0
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 800

    async def test_floor_still_bounded_by_the_model_limit(self, make_ctx):
        mock_redis = self._history(AsyncMock(), [100] * 10)
        mock_redis.get = AsyncMock(return_value=b"999999")
        ctx = make_ctx(model="gpt-4o-mini")
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.config["groups"]["G11_output"]["model_max_tokens"] = {"gpt-4o-mini": 16384}
        ctx.params["model"] = "gpt-4o-mini"
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 16384

    async def test_sanity_floor_is_never_the_applied_cap(self, make_ctx):
        """Degenerate evidence must DECLINE, not clamp up to 64. The old `max(64, ...)` made
        the floor the answer length whenever the estimate collapsed — three readiness probes
        were served capped at 64/74 on 2026-09-07 for exactly this reason."""
        mock_redis = self._history(AsyncMock(), [1] * 10)
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert "max_tokens" not in ctx.params

    async def test_history_min_entries_is_configurable(self, make_ctx):
        mock_redis = self._history(AsyncMock(), [100, 100, 100])
        ctx = make_ctx()
        ctx.config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True
        ctx.config["groups"]["G11_output"]["history_min_entries"] = 3
        # tests/conftest.py's minimal_config still pins tighten_multiplier 1.2 (the
        # pre-2026-09-08 value) and is outside this change's scope, so the three cases
        # below that are NOT about the default set it explicitly.
        ctx.config["groups"]["G11_output"]["tighten_multiplier"] = 2.0
        ctx.params["workflow_id"] = "wf-test"

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx = await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 200  # 3 entries would be too few at the default 5

    async def test_process_response_records_completed_answers(self, make_ctx):
        """process_response records (max_tokens, completion_tokens) for a COMPLETED
        (finish_reason=stop) answer."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        # 2026-09-08: the catch-all bucket (no workflow_id/template_id) is no longer
        # recorded, because nothing may be capped from it. This test is about the
        # recording rule, not the bucket rule, so it names a workload.
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.params["max_tokens"] = 256
        ctx.params["workflow_id"] = "wf-test"
        ctx.params["template_id"] = "tmpl-test"

        response = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "usage": {"completion_tokens": 128},
        }

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
        assert "truncated" not in data
        mock_redis.expire.assert_called_once()

    async def test_uncapped_completions_still_teach_the_history(self, make_ctx):
        """Cold-start bootstrap: an answer that completed with NO cap set is the
        most valuable evidence there is (its natural length). It must be recorded
        — otherwise a cold deployment that skips capping never accumulates the
        history that lets capping activate."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        # 2026-09-08: the catch-all bucket (no workflow_id/template_id) is no longer
        # recorded, because nothing may be capped from it. This test is about the
        # recording rule, not the bucket rule, so it names a workload.
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        assert "max_tokens" not in ctx.params

        response = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "a full proof"}}],
            "usage": {"completion_tokens": 1300},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        assert mock_redis.zadd.called
        zadd_call = mock_redis.zadd.call_args
        mapping = zadd_call.args[1] if len(zadd_call.args) > 1 else zadd_call.kwargs.get("mapping", {})
        data = json.loads(list(mapping.keys())[0])
        assert data["completion_tokens"] == 1300

    async def test_truncated_by_caller_cap_is_not_recorded(self, make_ctx):
        """A caller-capped truncation is neither a completed answer nor a G11
        mistake — it must contribute no evidence at all."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        # 2026-09-08: the catch-all bucket (no workflow_id/template_id) is no longer
        # recorded, because nothing may be capped from it. This test is about the
        # recording rule, not the bucket rule, so it names a workload.
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.params["max_tokens"] = 100  # caller-set: no _g11_max_tokens_set marker

        response = {
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": "cut off mi"}}],
            "usage": {"completion_tokens": 100},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        mock_redis.zadd.assert_not_called()

    async def test_g11_capped_truncation_recorded_with_marker(self, make_ctx):
        """DS18 anti-seal: an answer truncated by a cap G11 itself set must enter
        the history with the `truncated` marker (read back escalated), so the cap
        climbs after a bad guess instead of the truncated size re-teaching it."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        # 2026-09-08: the catch-all bucket (no workflow_id/template_id) is no longer
        # recorded, because nothing may be capped from it. This test is about the
        # recording rule, not the bucket rule, so it names a workload.
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.params["max_tokens"] = 128
        ctx.params["_g11_max_tokens_set"] = True

        response = {
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": "cut off mi"}}],
            "usage": {"completion_tokens": 128},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        assert mock_redis.zadd.called
        zadd_call = mock_redis.zadd.call_args
        mapping = zadd_call.args[1] if len(zadd_call.args) > 1 else zadd_call.kwargs.get("mapping", {})
        data = json.loads(list(mapping.keys())[0])
        assert data["truncated"] is True
        assert data["completion_tokens"] == 128

    async def test_tool_calls_finish_is_not_recorded(self, make_ctx):
        """Tool-call turns are structurally short; letting them teach the prose
        cap would drag the p95 down for mixed workloads."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        # 2026-09-08: the catch-all bucket (no workflow_id/template_id) is no longer
        # recorded, because nothing may be capped from it. This test is about the
        # recording rule, not the bucket rule, so it names a workload.
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.params["max_tokens"] = 256

        response = {
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "c1"}]}}],
            "usage": {"completion_tokens": 40},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        mock_redis.zadd.assert_not_called()

    async def test_process_response_feedback_loop_disabled_skips_recording(self, make_ctx):
        """When max_tokens_feedback_loop is false, process_response should skip recording."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        # 2026-09-08: the catch-all bucket (no workflow_id/template_id) is no longer
        # recorded, because nothing may be capped from it. This test is about the
        # recording rule, not the bucket rule, so it names a workload.
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = False
        ctx.params["max_tokens"] = 256

        response = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "usage": {"completion_tokens": 128},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            ctx, resp = await G11OutputFormat().process_response(ctx, response)

        mock_redis.zadd.assert_not_called()

    async def test_catch_all_bucket_is_not_recorded(self, make_ctx):
        """Nothing may be capped from the catch-all bucket, so nothing writes to it —
        a Redis write on every request into a set no read path consults is pure cost."""
        mock_redis = AsyncMock()
        ctx = make_ctx()  # no workflow_id, no template_id
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        response = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "usage": {"completion_tokens": 128},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            await G11OutputFormat().process_response(ctx, response)

        mock_redis.zadd.assert_not_called()

    async def test_truncation_raises_the_sticky_floor(self, make_ctx):
        """When G11's own cap cuts an answer, the bucket's floor rises to cap × backoff so
        no later percentile can put the cap back where it was. Without this the escalated
        history entry is one sample among many and ages out — which is how DS1's ds1-08 was
        cut at 232, raised, and cut again at 224 two repeats later."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.config["groups"]["G11_output"]["truncation_backoff_multiplier"] = 2.0
        ctx.params["max_tokens"] = 232
        ctx.params["_g11_max_tokens_set"] = True
        response = {
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": "cut off mi"}}],
            "usage": {"completion_tokens": 232},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            await G11OutputFormat().process_response(ctx, response)

        # The raise is a server-side compare-and-set (EVAL), not GET-then-SET: two workers
        # racing on a read-modify-write could LOWER the floor. Subject unchanged — the
        # floor still lands at cap × backoff — only the mechanism is atomic now.
        mock_redis.eval.assert_called_once()
        args = mock_redis.eval.call_args.args
        assert args[1] == 1, "EVAL must declare exactly one KEY"
        key, value = args[2], args[3]
        assert "max_tokens_floor" in key and "max_tokens_history" not in key
        assert value == 464
        mock_redis.set.assert_not_called()

    async def test_a_completed_answer_does_not_raise_the_floor(self, make_ctx):
        """Only a truncation is evidence that the cap was too low."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.params["max_tokens"] = 232
        ctx.params["_g11_max_tokens_set"] = True
        response = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "complete"}}],
            "usage": {"completion_tokens": 100},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            await G11OutputFormat().process_response(ctx, response)

        mock_redis.set.assert_not_called()

    async def test_history_is_trimmed_to_history_max_entries(self, make_ctx):
        """The read takes the whole set, so the bound has to live at write time."""
        mock_redis = AsyncMock()
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-test"
        ctx.config["groups"]["G11_output"]["max_tokens_feedback_loop"] = True
        ctx.config["groups"]["G11_output"]["history_max_entries"] = 50
        response = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "usage": {"completion_tokens": 128},
        }

        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            await G11OutputFormat().process_response(ctx, response)

        mock_redis.zremrangebyrank.assert_called_once()
        assert mock_redis.zremrangebyrank.call_args.args[1:] == (0, -51)


class TestG11ShippedDefaults:
    """The template is what a fresh install runs, so the defence against the 2026-09-08
    truncation defect has to be pinned there and not only in the code."""

    @staticmethod
    def _shipped_g11():
        import pathlib
        import yaml
        root = pathlib.Path(__file__).resolve().parents[3]
        cfg = yaml.safe_load((root / "config" / "config.yaml.template").read_text(encoding="utf-8"))
        return cfg["groups"]["G11_output"]

    def test_template_ships_auto_tighten_off(self):
        """It shipped ON and cut 4 of 54 DS1 answers and 6 of 27 readiness probes
        mid-sentence, for +0.00% input-token savings."""
        assert self._shipped_g11()["max_tokens_auto_tighten"] is False

    def test_template_multiplier_covers_observed_variance(self):
        """1.2 was under the model's own same-prompt spread (up to 2.03× measured)."""
        assert self._shipped_g11()["tighten_multiplier"] >= 2.0

    def test_template_and_code_defaults_agree(self):
        from middleware import g11_output_format as g11
        shipped = self._shipped_g11()
        assert shipped["tighten_multiplier"] == g11._DEFAULT_TIGHTEN_MULTIPLIER
        assert shipped["history_min_entries"] == g11._DEFAULT_HISTORY_MIN_ENTRIES
        assert shipped["history_max_entries"] == g11._DEFAULT_HISTORY_MAX_ENTRIES
        assert shipped["truncation_backoff_multiplier"] == g11._DEFAULT_TRUNCATION_BACKOFF

    def test_the_dead_knobs_stay_dead(self):
        """`absolute_default_max_tokens` and `default_max_tokens_multiplier` have no reader
        anywhere in the proxy. They survived in the portal catalog as two response-length
        dials a truncated tenant could turn with no effect; nothing may reintroduce them."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[3]
        dead = ("absolute_default_max_tokens", "default_max_tokens_multiplier")
        offenders = []
        for path in (root / "src" / "proxy").rglob("*.py"):
            code = chr(10).join(
                line.split("#", 1)[0]
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            )
            if any(name in code for name in dead):
                offenders.append(str(path))
        assert not offenders, f"dead max_tokens knob referenced in: {offenders}"


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
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert "max_tokens" in ctx.params          # shaping applied (default behaviour)
        assert "_g11_cohort" not in ctx.params      # no cohort assigned when disabled

    async def test_holdout_full_skips_shaping(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        self._enable_holdout(ctx, 1.0)
        from middleware.g11_output_format import G11OutputFormat
        with patch("middleware.g11_output_format._get_redis", side_effect=Exception("no redis")):
            ctx = await G11OutputFormat().process_request(ctx)
        assert ctx.params["_g11_cohort"] == "holdout"
        assert "max_tokens" not in ctx.params       # shaping skipped for the control cohort

    async def test_holdout_zero_fraction_is_treatment(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
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

    async def test_cohort_bucket_matches_shared_stable_bucket_helper(self, make_ctx):
        """Regression (2026-07-20 code-review reuse finding): the holdout bucket must be
        computed via g06_routing.stable_bucket, not a second, independently-maintained
        copy of the same hash formula — assert the two never diverge."""
        import hashlib
        from middleware.g11_output_format import _assign_cohort, _HOLDOUT_BUCKETS
        from middleware.g06_routing import stable_bucket
        cfg = {"enabled": True, "fraction": 0.5, "sticky_key": "workflow_id"}
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-shared-bucket-check"
        cohort = _assign_cohort(ctx, cfg)
        expected_bucket = stable_bucket("wf-shared-bucket-check", _HOLDOUT_BUCKETS)
        expected_cohort = "holdout" if expected_bucket < 0.5 * _HOLDOUT_BUCKETS else "treatment"
        assert cohort == expected_cohort

    async def test_cohort_ignores_harness_arm_scope_suffix(self, make_ctx):
        """Backlog #10: the pitch harness scopes workflow_id per arm as
        '<id>::<arm>::<token>'. The cohort must key on the ORIGINAL id so a workflow
        stays in ONE cohort across arms (else the A3 treatment-vs-holdout comparison
        fragments). All scoped variants of one id → the same cohort as the bare id."""
        from middleware.g11_output_format import _assign_cohort
        cfg = {"enabled": True, "fraction": 0.5, "sticky_key": "workflow_id"}
        base = make_ctx(); base.params["workflow_id"] = "ds3-workflow-1"
        bare = _assign_cohort(base, cfg)
        for scoped in ("ds3-workflow-1::all-off::a1b2c3d4",
                       "ds3-workflow-1::only-G17::99887766",
                       "ds3-workflow-1::all-on::deadbeef"):
            ctx = make_ctx(); ctx.params["workflow_id"] = scoped
            assert _assign_cohort(ctx, cfg) == bare, scoped

    async def test_cohort_distinct_workflows_still_differ_under_scoping(self, make_ctx):
        """Stripping the suffix must not collapse DISTINCT workflows onto one key."""
        from middleware.g11_output_format import _assign_cohort, _HOLDOUT_BUCKETS
        from middleware.g06_routing import stable_bucket
        cfg = {"enabled": True, "fraction": 0.5, "sticky_key": "workflow_id"}
        ctx = make_ctx(); ctx.params["workflow_id"] = "ds3-workflow-2::all-on::tok"
        cohort = _assign_cohort(ctx, cfg)
        # keyed on the stripped original 'ds3-workflow-2', not the scoped string
        expected_bucket = stable_bucket("ds3-workflow-2", _HOLDOUT_BUCKETS)
        assert cohort == ("holdout" if expected_bucket < 0.5 * _HOLDOUT_BUCKETS else "treatment")

    async def test_holdout_preserves_structured_output(self, make_ctx):
        """Even in the control cohort, correctness (JSON response_format) is NOT skipped."""
        ctx = make_ctx(model="gpt-4o")
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
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


class TestVerbosityCacheTagHoldout:
    """Regression coverage: G05 caches BEFORE G11 decides the A3 holdout cohort, so
    verbosity_cache_tag must independently re-derive the cohort — otherwise a holdout
    (unshaped) request and a treatment (suffix-shaped) request compute the same cache
    key despite receiving different prompts."""

    def _ctx(self, holdout_fraction, request_id="req-1", user_id=None, level="full"):
        class _C:
            config = {
                "groups": {
                    "G11_output": {
                        "enabled": True,
                        "verbosity_steering": {"enabled": True, "level": level},
                        "output_holdout": {"enabled": True, "fraction": holdout_fraction},
                    }
                }
            }
            tenant_id = "t"
            params = {}
        _C.user_id = user_id
        _C.request_id = request_id
        return _C()

    def test_holdout_cohort_returns_empty_tag(self):
        # fraction=1.0 → _assign_cohort deterministically returns "holdout" regardless
        # of request_id, matching G11's later skip-the-suffix decision.
        c = self._ctx(holdout_fraction=1.0)
        assert verbosity_cache_tag(c) == ""

    def test_treatment_cohort_returns_nonempty_tag(self):
        # fraction=0.0 → deterministically "treatment" → suffix will be injected.
        c = self._ctx(holdout_fraction=0.0)
        assert verbosity_cache_tag(c) != ""

    def test_holdout_and_treatment_tags_differ_for_same_question(self):
        # The actual bug scenario: two requests, same tenant/level, one drawn into
        # holdout and one into treatment, must NOT collide on the same cache tag.
        holdout_tag = verbosity_cache_tag(self._ctx(holdout_fraction=1.0, request_id="req-A"))
        treatment_tag = verbosity_cache_tag(self._ctx(holdout_fraction=0.0, request_id="req-B"))
        assert holdout_tag != treatment_tag
        assert holdout_tag == ""

    def test_holdout_disabled_ignores_cohort(self):
        # output_holdout.enabled=False → cohort logic never runs; tag reflects level only.
        c = self._ctx(holdout_fraction=1.0)
        c.config["groups"]["G11_output"]["output_holdout"]["enabled"] = False
        assert verbosity_cache_tag(c) != ""
