"""Unit tests for G18 — Observability & Token FinOps."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_llm_response(prompt_tokens=20, completion_tokens=5):
    return {
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@pytest.mark.asyncio
class TestG18Observability:
    async def test_disabled_no_langfuse_call(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G18_observability"]["enabled"] = False
        with patch("middleware.langfuse_tracing.finish_trace") as mock_finish:
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, _make_llm_response())
        mock_finish.assert_not_called()

    async def test_enabled_calls_finish_trace(self, make_ctx):
        ctx = make_ctx()
        with patch("middleware.langfuse_tracing.finish_trace") as mock_finish:
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, _make_llm_response())
        mock_finish.assert_called_once()

    async def test_updates_final_tokens_from_response(self, make_ctx):
        ctx = make_ctx()
        from middleware.g18_observability import G18Observability
        with patch("middleware.langfuse_tracing.finish_trace"):
            await G18Observability().record(ctx, _make_llm_response(prompt_tokens=42, completion_tokens=7))
        assert ctx.savings.final_tokens_sent == 42
        assert ctx.savings.response_tokens == 7

    async def test_cost_actual_updated(self, make_ctx):
        ctx = make_ctx()
        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, _make_llm_response(50, 10))
        assert ctx.savings.cost_actual_usd >= 0.0

    async def test_provider_prompt_tokens_captured_as_z(self, make_ctx):
        """B1 — G18 records the provider's prompt_tokens as z (and final == z)."""
        ctx = make_ctx()
        ctx.savings.proxy_optimised_tokens = 30  # y estimate from pipeline
        from middleware.g18_observability import G18Observability
        with patch("middleware.langfuse_tracing.finish_trace"):
            await G18Observability().record(ctx, _make_llm_response(prompt_tokens=42, completion_tokens=7))
        assert ctx.savings.provider_prompt_tokens == 42   # z
        assert ctx.savings.final_tokens_sent == 42

    async def test_provider_prompt_falls_back_to_proxy_estimate_when_usage_absent(self, make_ctx):
        """B1 — with no provider usage, z stays None and final falls back to y."""
        ctx = make_ctx()
        ctx.savings.proxy_optimised_tokens = 33  # y estimate
        resp = {
            "id": "x",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }  # no "usage"
        from middleware.g18_observability import G18Observability
        with patch("middleware.langfuse_tracing.finish_trace"):
            await G18Observability().record(ctx, resp)
        assert ctx.savings.provider_prompt_tokens is None
        assert ctx.savings.final_tokens_sent == 33

    async def test_langfuse_error_handled_in_finish_trace(self, make_ctx):
        """finish_trace swallows Langfuse failures so the proxy never breaks."""
        ctx = make_ctx()
        ctx.savings.final_tokens_sent = 10
        ctx.savings.response_tokens = 5
        # Simulate a Langfuse client failure
        with patch("middleware.langfuse_tracing.get_client", return_value=None):
            from middleware.langfuse_tracing import finish_trace
            # Should NOT raise
            finish_trace(ctx, _make_llm_response())

    async def test_trace_lifecycle_calls_generation_and_scores(self, make_ctx):
        """start_trace creates a trace; finish_trace adds generation + scores."""
        ctx = make_ctx()
        ctx.savings.final_tokens_sent = 20
        ctx.savings.response_tokens = 5
        ctx.savings.cost_actual_usd = 0.0001
        ctx.savings.cost_baseline_usd = 0.0005

        mock_trace = MagicMock()
        mock_lf = MagicMock()
        mock_lf.trace.return_value = mock_trace

        with patch("middleware.langfuse_tracing.get_client", return_value=mock_lf):
            from middleware.langfuse_tracing import start_trace, finish_trace
            start_trace(ctx)
            assert ctx.langfuse_trace is mock_trace
            finish_trace(ctx, _make_llm_response(20, 5))

        mock_lf.trace.assert_called_once()
        mock_trace.generation.assert_called_once()
        mock_trace.score.assert_any_call(name="savings_pct", value=mock_trace.score.call_args_list[0][1]["value"])
        mock_lf.flush.assert_called_once()

    async def test_effective_token_et_in_metadata(self, make_ctx):
        ctx = make_ctx()
        captured_metadata = {}

        def _capture(c, response):
            captured_metadata.update(c.savings.to_langfuse_metadata())

        with patch("middleware.langfuse_tracing.finish_trace", side_effect=_capture):
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, _make_llm_response(20, 5))

        assert "effective_token_et" in captured_metadata
        assert captured_metadata["effective_token_et"] >= 0

    async def test_prometheus_metrics_have_no_user_id_label(self, make_ctx):
        """COST_USD and REQUESTS_TOTAL must not carry an unbounded user_id label
        (cardinality risk) — only bounded model/team/feature/tenant_id labels."""
        from middleware.g18_observability import COST_USD, REQUESTS_TOTAL

        assert "user_id" not in COST_USD._labelnames
        assert "user_id" not in REQUESTS_TOTAL._labelnames
        assert set(COST_USD._labelnames) == {"model", "team", "feature", "tenant_id"}
        assert set(REQUESTS_TOTAL._labelnames) == {"model", "team", "feature", "tenant_id"}

    async def test_record_emits_prometheus_metrics_with_team_feature_labels(self, make_ctx):
        ctx = make_ctx()
        ctx.params["x_team"] = "team-a"
        ctx.params["x_feature"] = "feature-x"
        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability, REQUESTS_TOTAL, COST_USD
            before = REQUESTS_TOTAL.labels(
                model=ctx.routed_model, team="team-a", feature="feature-x", tenant_id=ctx.tenant_id
            )._value.get()
            await G18Observability().record(ctx, _make_llm_response())
            after = REQUESTS_TOTAL.labels(
                model=ctx.routed_model, team="team-a", feature="feature-x", tenant_id=ctx.tenant_id
            )._value.get()
        assert after == before + 1
        # COST_USD should also be incrementable with team/feature/tenant_id labels
        COST_USD.labels(model=ctx.routed_model, team="team-a", feature="feature-x", tenant_id=ctx.tenant_id)

    async def test_turn_efficiency_check_does_not_close_shared_redis(self, make_ctx):
        """G18 must not call aclose() on the shared connection-pool client —
        doing so disconnects the entire pool out from under concurrent requests."""
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-1"
        ctx.params["_token_budget"] = {"workflow_turn": 3}

        shared_redis = AsyncMock()
        shared_redis.get = AsyncMock(return_value=None)
        shared_redis.set = AsyncMock(return_value=True)

        cfg = ctx.config["groups"]["G18_observability"]
        with patch("middleware.g18_observability._get_redis", return_value=shared_redis):
            from middleware.g18_observability import G18Observability
            await G18Observability()._check_turn_efficiency(ctx, cfg)

        shared_redis.aclose.assert_not_called()
        shared_redis.close.assert_not_called()

    async def test_record_tool_calls_does_not_close_shared_redis(self, make_ctx):
        ctx = make_ctx()
        shared_redis = AsyncMock()
        shared_redis.zadd = AsyncMock(return_value=1)

        response = _make_llm_response()
        response["choices"][0]["message"]["tool_calls"] = [
            {"function": {"name": "lookup_order"}}
        ]

        with patch("middleware.g18_observability._get_redis", return_value=shared_redis):
            from middleware.g18_observability import G18Observability
            await G18Observability()._record_tool_calls(ctx, response)

        shared_redis.aclose.assert_not_called()
        shared_redis.close.assert_not_called()

    async def test_per_group_savings_counter_matches_step_savings(self, make_ctx):
        """Each group's StepSaving.absolute_saving must be reflected in the
        per-group Prometheus counter, enabling a real-time per-group view
        without waiting for the daily/per-call Langfuse export."""
        ctx = make_ctx()
        ctx.savings.add_step("G01", "compression", 100, 60)
        ctx.savings.add_step("G05", "cache", 50, 50)  # zero saving — should not increment

        from middleware.g18_observability import G18Observability, GROUP_TOKENS_SAVED

        tid = ctx.tenant_id  # GROUP_TOKENS_SAVED is now labelled (group, tenant_id)
        before_g01 = GROUP_TOKENS_SAVED.labels(group="G01", tenant_id=tid)._value.get()
        before_g05 = GROUP_TOKENS_SAVED.labels(group="G05", tenant_id=tid)._value.get()

        with patch("middleware.langfuse_tracing.finish_trace"):
            await G18Observability().record(ctx, _make_llm_response())

        after_g01 = GROUP_TOKENS_SAVED.labels(group="G01", tenant_id=tid)._value.get()
        after_g05 = GROUP_TOKENS_SAVED.labels(group="G05", tenant_id=tid)._value.get()

        assert after_g01 == before_g01 + 40
        assert after_g05 == before_g05

    async def test_shared_redis_client_survives_concurrent_g18_calls(self, make_ctx):
        """A single shared client (as returned by cache.redis_pool.get_redis)
        must remain usable across back-to-back G18 operations — confirms the
        pool isn't torn down mid-use."""
        shared_redis = AsyncMock()
        shared_redis.get = AsyncMock(return_value=None)
        shared_redis.set = AsyncMock(return_value=True)
        shared_redis.zadd = AsyncMock(return_value=1)

        ctx1 = make_ctx()
        ctx1.params["workflow_id"] = "wf-1"
        ctx1.params["_token_budget"] = {"workflow_turn": 1}

        response = _make_llm_response()
        response["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "lookup_order"}}]

        cfg = ctx1.config["groups"]["G18_observability"]
        with patch("middleware.g18_observability._get_redis", return_value=shared_redis):
            from middleware.g18_observability import G18Observability
            g18 = G18Observability()
            await g18._check_turn_efficiency(ctx1, cfg)
            await g18._record_tool_calls(ctx1, response)

        # The same client instance handled both calls without being closed in between.
        shared_redis.aclose.assert_not_called()
        shared_redis.close.assert_not_called()
        shared_redis.get.assert_awaited()
        shared_redis.zadd.assert_awaited()


# ── G18 cost-accounting regression tests (the −21093% cost-line bug) ──────────

_PRICING = {  # per-1k USD; pinned so the tests don't depend on ambient config_loader state
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "o1": {"input": 0.015, "output": 0.06},
    "default": {"input": 0.005, "output": 0.015},
}


@pytest.mark.asyncio
class TestG18CostAccounting:
    """G18 owns cost. Baseline and actual must be computed on the SAME basis
    (both include the same completion tokens); baseline uses the originally-requested
    model, actual uses the routed model. Regression coverage for the bug where G06
    pre-seeded an input-only baseline that G18 then left alone while overwriting actual
    with output included, making 'actual' appear ~200x 'baseline'.
    """

    async def _record(self, ctx, prompt_tokens, completion_tokens):
        from middleware.g18_observability import G18Observability
        await G18Observability().record(ctx, _make_llm_response(prompt_tokens, completion_tokens))

    async def test_baseline_and_actual_share_completion_basis(self, make_ctx):
        """No routing, no prompt savings: baseline must EQUAL actual because both
        include the same completion tokens. If baseline excluded output (the old bug)
        these would diverge."""
        ctx = make_ctx(model="gpt-4o-mini")
        ctx.savings.baseline_tokens = 100  # == prompt sent below → no prompt savings

        with patch("config_loader.get_pricing_table", return_value=_PRICING), \
             patch("middleware.langfuse_tracing.finish_trace"):
            from savings.calculator import estimate_cost
            # Simulate the stale, input-only value G06 used to leave behind.
            ctx.savings.cost_baseline_usd = estimate_cost(100, 0, "gpt-4o-mini")
            await self._record(ctx, prompt_tokens=100, completion_tokens=50)

            expected = estimate_cost(100, 50, "gpt-4o-mini")
            input_only = estimate_cost(100, 0, "gpt-4o-mini")

        # G18 recomputed baseline WITH completion — it did not keep the input-only seed.
        assert ctx.savings.cost_baseline_usd == expected
        assert ctx.savings.cost_actual_usd == expected
        assert ctx.savings.cost_baseline_usd == ctx.savings.cost_actual_usd
        # And it is strictly larger than the input-only figure it replaced.
        assert ctx.savings.cost_baseline_usd > input_only

    async def test_routing_to_cheaper_model_yields_positive_cost_saving(self, make_ctx):
        """gpt-4o requested, routed down to gpt-4o-mini → actual < baseline, both > 0."""
        ctx = make_ctx(model="gpt-4o")          # model_requested = gpt-4o
        ctx.routed_model = "gpt-4o-mini"         # G06 routed cheaper
        ctx.savings.baseline_tokens = 100

        with patch("config_loader.get_pricing_table", return_value=_PRICING), \
             patch("middleware.langfuse_tracing.finish_trace"):
            from savings.calculator import estimate_cost
            await self._record(ctx, prompt_tokens=80, completion_tokens=20)
            exp_baseline = estimate_cost(100, 20, "gpt-4o")
            exp_actual = estimate_cost(80, 20, "gpt-4o-mini")

        assert ctx.savings.cost_baseline_usd == exp_baseline
        assert ctx.savings.cost_actual_usd == exp_actual
        assert ctx.savings.cost_actual_usd < ctx.savings.cost_baseline_usd
        assert ctx.savings.cost_saving_usd > 0

    async def test_cascade_escalation_to_pricier_model_can_exceed_baseline(self, make_ctx):
        """gpt-4o-mini requested, cascade escalated UP to o1 → actual > baseline.

        This is a REAL cost increase (o1 is ~100x gpt-4o-mini per token), not the
        output-exclusion artifact: baseline still includes completion (it is strictly
        larger than the input-only figure). Documents that the cost-savings line can
        legitimately go negative for escalating workloads.
        """
        ctx = make_ctx(model="gpt-4o-mini")     # model_requested = gpt-4o-mini
        ctx.routed_model = "o1"                   # cascade escalated up
        ctx.savings.baseline_tokens = 100

        with patch("config_loader.get_pricing_table", return_value=_PRICING), \
             patch("middleware.langfuse_tracing.finish_trace"):
            from savings.calculator import estimate_cost
            await self._record(ctx, prompt_tokens=80, completion_tokens=20)
            exp_baseline = estimate_cost(100, 20, "gpt-4o-mini")
            exp_actual = estimate_cost(80, 20, "o1")
            input_only_baseline = estimate_cost(100, 0, "gpt-4o-mini")

        assert ctx.savings.cost_baseline_usd == exp_baseline
        assert ctx.savings.cost_actual_usd == exp_actual
        # Escalation genuinely costs more — negative saving is expected and correct.
        assert ctx.savings.cost_actual_usd > ctx.savings.cost_baseline_usd
        assert ctx.savings.cost_saving_usd < 0
        # Proof it is not the old artifact: baseline includes output, not input-only.
        assert ctx.savings.cost_baseline_usd > input_only_baseline

    async def test_cached_tokens_credit_provider_cache_discount(self, make_ctx):
        """Response-reported cached tokens reduce actual cost via the adapter multiplier (P1)."""
        from providers.openai_adapter import OpenAIAdapter
        from savings.calculator import estimate_cost, estimate_cost_with_cache
        ctx = make_ctx(model="gpt-4o")
        ctx.provider_adapter = OpenAIAdapter()
        ctx.savings.baseline_tokens = 100

        resp = _make_llm_response(prompt_tokens=100, completion_tokens=20)
        resp["usage"]["prompt_tokens_details"] = {"cached_tokens": 80}

        with patch("config_loader.get_pricing_table", return_value=_PRICING), \
             patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, resp)
            full = estimate_cost(100, 20, "gpt-4o")                       # no cache credit
            expected = estimate_cost_with_cache(100, 80, 20, "gpt-4o", 0.5)  # OpenAI 50%

        assert ctx.savings.cost_actual_usd == expected
        assert ctx.savings.cost_actual_usd < full      # discount genuinely applied
        assert ctx.savings.cost_saving_usd > 0

    async def test_cached_tokens_without_adapter_apply_no_discount(self, make_ctx):
        """No provider_adapter → multiplier defaults to 1.0 (no crash, no phantom discount)."""
        from savings.calculator import estimate_cost
        ctx = make_ctx(model="gpt-4o")  # provider_adapter is None
        ctx.savings.baseline_tokens = 100
        resp = _make_llm_response(prompt_tokens=100, completion_tokens=20)
        resp["usage"]["prompt_tokens_details"] = {"cached_tokens": 80}

        with patch("config_loader.get_pricing_table", return_value=_PRICING), \
             patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, resp)

        assert ctx.savings.cost_actual_usd == estimate_cost(100, 20, "gpt-4o")


# ── F3-T: G18 AuditLogger integration regression tests ───────────────────────

@pytest.mark.asyncio
class TestG18AuditLoggerIntegration:
    """F3-T: G18 must call AuditLogger.log once per request when audit.enabled,
    and must not break Prometheus metrics when audit is active."""

    def _response(self):
        return {
            "id": "chatcmpl-f3t",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }

    async def test_audit_logger_log_called_once(self, make_ctx):
        ctx = make_ctx()
        ctx.config["audit"] = {"enabled": True}

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            g18 = G18Observability(audit_logger=mock_audit)
            await g18.record(ctx, self._response())

        mock_audit.log.assert_awaited_once()

    async def test_audit_logger_not_called_when_disabled(self, make_ctx):
        ctx = make_ctx()
        ctx.config["audit"] = {"enabled": False}

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            g18 = G18Observability(audit_logger=mock_audit)
            await g18.record(ctx, self._response())

        mock_audit.log.assert_not_awaited()

    async def test_audit_logger_not_called_when_no_audit_config(self, make_ctx):
        ctx = make_ctx()
        # No audit key in config at all
        ctx.config.pop("audit", None)

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            g18 = G18Observability(audit_logger=mock_audit)
            await g18.record(ctx, self._response())

        mock_audit.log.assert_not_awaited()

    async def test_prometheus_metrics_still_recorded_with_audit_active(self, make_ctx):
        ctx = make_ctx()
        ctx.config["audit"] = {"enabled": True}
        ctx.params["x_team"] = "audit-team"
        ctx.params["x_feature"] = "audit-feature"

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock()

        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability, REQUESTS_TOTAL
            before = REQUESTS_TOTAL.labels(
                model=ctx.routed_model, team="audit-team", feature="audit-feature", tenant_id=ctx.tenant_id
            )._value.get()
            g18 = G18Observability(audit_logger=mock_audit)
            await g18.record(ctx, self._response())
            after = REQUESTS_TOTAL.labels(
                model=ctx.routed_model, team="audit-team", feature="audit-feature", tenant_id=ctx.tenant_id
            )._value.get()

        assert after == before + 1
        mock_audit.log.assert_awaited_once()

    async def test_audit_logger_failure_does_not_raise(self, make_ctx):
        """AuditLogger failure must be swallowed — audit must not break serving."""
        ctx = make_ctx()
        ctx.config["audit"] = {"enabled": True}

        mock_audit = AsyncMock()
        mock_audit.log = AsyncMock(side_effect=RuntimeError("db gone"))

        with patch("middleware.langfuse_tracing.finish_trace"):
            from middleware.g18_observability import G18Observability
            g18 = G18Observability(audit_logger=mock_audit)
            await g18.record(ctx, self._response())  # must not raise
