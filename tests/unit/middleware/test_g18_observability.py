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
        # finish_trace deliberately does NOT flush synchronously — a per-request
        # client.flush() would block the response on a Langfuse round-trip. The
        # persistent client's background consumer flushes on an interval + at exit,
        # so delivery is unchanged; only the timing moves off the critical path.
        mock_lf.flush.assert_not_called()

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
        # team label comes from the TRUSTED team (ctx.team — a gateway key's X-Team), never
        # the raw params["x_team"] (2026-09-18). feature reads params and keeps its own label
        # only when listed: unlisted, any caller's value folds to "other".
        ctx.team = "team-a"
        ctx.params["x_feature"] = "feature-x"
        ctx.config["groups"]["G18_observability"]["label_values"] = {"feature": ["feature-x"]}
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
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    "gemini-2.5-flash": {"input": 0.0003, "output": 0.0025},
    "default": {"input": 0.005, "output": 0.015},
}
_PROVIDERS = [  # model -> provider, pinned for the same reason
    {"name": "openai", "model_prefixes": ["gpt-", "o1"]},
    {"name": "anthropic", "model_prefixes": ["claude-"]},
    {"name": "gemini", "model_prefixes": ["gemini-"]},
]


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
        ctx.config["providers"] = _PROVIDERS
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
        # OpenAI caches a repeated prompt with or without the proxy, so the same discount
        # is in the baseline and this request saved nothing.
        assert ctx.savings.cost_saving_usd == 0

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


@pytest.mark.asyncio
class TestCostIsNotObservability:
    """A tenant may switch G18 off in the portal. That turns off its metrics, export and
    tracing, not its cost: the spend counter, the spend cap and the billing row all read
    cost_actual_usd, and with G18 off every non-streamed answer was priced at $0 (a
    streamed one was still priced)."""

    async def _record(self, ctx, enabled):
        ctx.config["groups"]["G18_observability"]["enabled"] = enabled
        with patch("config_loader.get_pricing_table", return_value=_PRICING), \
                patch("middleware.langfuse_tracing.finish_trace"), \
                patch("middleware.g18_observability.emit_usage_metrics") as metrics:
            from middleware.g18_observability import G18Observability
            await G18Observability().record(ctx, _make_llm_response(100, 50))
        return metrics

    async def test_an_answer_is_priced_with_g18_off(self, make_ctx):
        from savings.calculator import estimate_cost
        ctx = make_ctx(model="gpt-4o-mini")
        ctx.savings.baseline_tokens = 100
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            expected = estimate_cost(100, 50, "gpt-4o-mini")
        await self._record(ctx, enabled=False)
        assert expected > 0 and ctx.savings.cost_actual_usd == expected
        assert ctx.savings.cost_baseline_usd == expected

    async def test_g18_off_still_emits_no_metrics(self, make_ctx):
        metrics = await self._record(make_ctx(model="gpt-4o-mini"), enabled=False)
        metrics.assert_not_called()

    async def test_the_fallback_pricing_prices_with_g18_off(self, make_ctx):
        from middleware.g18_observability import price_billed_call
        ctx = make_ctx(model="gpt-4o-mini")
        ctx.config["groups"]["G18_observability"]["enabled"] = False
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            price_billed_call(ctx, _make_llm_response(100, 50))
        assert ctx.savings.cost_actual_usd > 0


@pytest.fixture
def _pinned_pricing():
    with patch("config_loader.get_pricing_table", return_value=_PRICING), \
         patch("middleware.langfuse_tracing.finish_trace"):
        yield


def _adapter(name):
    from providers import get_adapter_by_name
    return get_adapter_by_name(name)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_pinned_pricing")
class TestBaselineCacheDiscount:
    """The baseline is the caller's own request, sent straight to the model it asked for.
    It gets the provider's prompt-cache discount (the reads and writes the served call
    reported, at the requested provider's rates) whenever that request would have been
    cached without the proxy: its provider caches repeated prompts on its own, or the
    caller marked the prompt for caching. Otherwise the discount is the proxy's saving."""

    async def _price(self, make_ctx, *, requested, served, usage, baseline_tokens=1000,
                     params=None):
        from middleware.g18_observability import G18Observability
        ctx = make_ctx(model=requested, params=params or {})
        ctx.config["providers"] = _PROVIDERS
        ctx.routed_model = served
        ctx.provider_adapter = _adapter(next(
            p["name"] for p in _PROVIDERS if served.startswith(tuple(p["model_prefixes"]))))
        ctx.savings.baseline_tokens = baseline_tokens
        resp = _make_llm_response(prompt_tokens=usage.pop("prompt_tokens", 1000),
                                  completion_tokens=20)
        resp["usage"].update(usage)
        await G18Observability().record(ctx, resp)
        return ctx.savings

    @pytest.mark.parametrize("model,rate", [("gpt-4o", 0.5), ("gemini-2.5-flash", 0.25)])
    async def test_a_provider_that_caches_on_its_own_puts_its_discount_in_the_baseline(
            self, make_ctx, model, rate):
        from savings.calculator import estimate_cost_with_cache
        savings = await self._price(make_ctx, requested=model, served=model,
                                    usage={"prompt_tokens_details": {"cached_tokens": 800}})
        assert savings.cost_baseline_usd == estimate_cost_with_cache(1000, 800, 20, model, rate)
        assert savings.cost_baseline_usd == savings.cost_actual_usd
        assert savings.cost_saving_usd == 0

    async def test_a_prompt_saving_still_counts_beside_the_discount(self, make_ctx):
        from savings.calculator import estimate_cost, estimate_cost_with_cache
        savings = await self._price(
            make_ctx, requested="gpt-4o", served="gpt-4o", baseline_tokens=1500,
            usage={"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800}})
        assert savings.cost_baseline_usd == estimate_cost_with_cache(1500, 800, 20, "gpt-4o", 0.5)
        assert savings.cost_saving_usd == pytest.approx(estimate_cost(500, 0, "gpt-4o"))

    async def test_a_discount_the_proxys_marker_earned_stays_a_saving(self, make_ctx):
        from savings.calculator import estimate_cost
        savings = await self._price(make_ctx, requested="claude-sonnet-4-5",
                                    served="claude-sonnet-4-5",
                                    usage={"prompt_tokens_details": {"cached_tokens": 800}})
        assert savings.cost_baseline_usd == estimate_cost(1000, 20, "claude-sonnet-4-5")
        # Anthropic bills a cache read at 10% of the input rate: 90% of 800 tokens saved.
        assert savings.cost_saving_usd == pytest.approx(
            0.9 * estimate_cost(800, 0, "claude-sonnet-4-5"))

    async def test_a_discount_the_callers_own_markers_earned_is_not_a_saving(self, make_ctx):
        from protocols.base import CALLER_CACHE_MARKERS
        from savings.calculator import estimate_cost_with_cache
        savings = await self._price(
            make_ctx, requested="claude-sonnet-4-5", served="claude-sonnet-4-5",
            params={CALLER_CACHE_MARKERS: True},
            usage={"prompt_tokens_details": {"cached_tokens": 600},
                   "cache_creation_input_tokens": 200,
                   "cache_creation_token_details": {"ephemeral_1h_input_tokens": 50}})
        assert savings.cost_baseline_usd == estimate_cost_with_cache(
            1000, 600, 20, "claude-sonnet-4-5", 0.1,
            cache_write_tokens=200, cache_write_multiplier=1.25,
            cache_write_1h_tokens=50, cache_write_1h_multiplier=2.0)
        assert savings.cost_baseline_usd == savings.cost_actual_usd
        assert savings.cost_saving_usd == 0

    async def test_the_callers_own_cache_write_is_not_charged_to_the_proxy(self, make_ctx):
        # The first call under the caller's marker only writes the cache, at a premium the
        # caller would have paid without the proxy too.
        from protocols.base import CALLER_CACHE_MARKERS
        savings = await self._price(
            make_ctx, requested="claude-sonnet-4-5", served="claude-sonnet-4-5",
            params={CALLER_CACHE_MARKERS: True}, usage={"cache_creation_input_tokens": 800})
        assert savings.cost_baseline_usd == savings.cost_actual_usd
        assert savings.cost_saving_usd == 0

    async def test_asked_for_a_provider_that_caches_on_its_own_but_routed_elsewhere(
            self, make_ctx):
        # Sent straight to OpenAI, the prompt would have been cached anyway, at OpenAI's
        # rate, though the proxy's marker earned the discount on the Anthropic call.
        from savings.calculator import estimate_cost_with_cache
        savings = await self._price(make_ctx, requested="gpt-4o", served="claude-sonnet-4-5",
                                    usage={"prompt_tokens_details": {"cached_tokens": 800}})
        assert savings.cost_baseline_usd == estimate_cost_with_cache(1000, 800, 20, "gpt-4o", 0.5)

    async def test_asked_for_a_provider_that_caches_only_a_marked_prompt_but_routed_elsewhere(
            self, make_ctx):
        # Sent straight to Anthropic unmarked, nothing would have been cached: the discount
        # the routed OpenAI call got is the proxy's.
        from savings.calculator import estimate_cost
        savings = await self._price(make_ctx, requested="claude-sonnet-4-5",
                                    served="gpt-4o-mini",
                                    usage={"prompt_tokens_details": {"cached_tokens": 800}})
        assert savings.cost_baseline_usd == estimate_cost(1000, 20, "claude-sonnet-4-5")

    async def test_no_cache_activity_prices_the_baseline_at_list_price(self, make_ctx):
        from savings.calculator import estimate_cost
        savings = await self._price(make_ctx, requested="gpt-4o", served="gpt-4o", usage={})
        assert savings.cost_baseline_usd == estimate_cost(1000, 20, "gpt-4o")

    async def test_a_failed_provider_lookup_prices_the_baseline_at_list_price(self, make_ctx):
        from savings.calculator import estimate_cost, estimate_cost_with_cache
        with patch("middleware.g18_observability.get_adapter", side_effect=RuntimeError("x")):
            try:
                savings = await self._price(
                    make_ctx, requested="gpt-4o", served="gpt-4o",
                    usage={"prompt_tokens_details": {"cached_tokens": 800}})
            except RuntimeError as exc:  # pricing must never fail the request
                pytest.fail(f"raised {exc!r}")
        assert savings.cost_baseline_usd == estimate_cost(1000, 20, "gpt-4o")
        assert savings.cost_actual_usd == estimate_cost_with_cache(1000, 800, 20, "gpt-4o", 0.5)


# ── G18 neither bills nor audits a request ───────────────────────────────────

def test_g18_takes_no_usage_meter_or_audit_logger():
    """G18 had branches that billed through a usage meter and wrote an audit row per request,
    but the pipeline builds G18Observability() bare, so they never ran: billing is
    main._record_outcome's, and the audit log records configuration changes and security
    events. The branches only made G18 look like the billing path."""
    import inspect
    from middleware.g18_observability import G18Observability
    assert list(inspect.signature(G18Observability.__init__).parameters) == ["self"]


@pytest.mark.asyncio
async def test_a_request_is_counted_under_its_team_and_feature(make_ctx):
    ctx = make_ctx()
    ctx.team = "audit-team"                       # trusted team → label (2026-09-18)
    ctx.params["x_feature"] = "audit-feature"     # listed, so it keeps its own label
    ctx.config["groups"]["G18_observability"]["label_values"] = {"feature": ["audit-feature"]}
    response = {
        "id": "chatcmpl-f3t",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }
    with patch("middleware.langfuse_tracing.finish_trace"):
        from middleware.g18_observability import G18Observability, REQUESTS_TOTAL
        labels = dict(model=ctx.routed_model, team="audit-team", feature="audit-feature",
                      tenant_id=ctx.tenant_id)
        before = REQUESTS_TOTAL.labels(**labels)._value.get()
        await G18Observability().record(ctx, response)
        after = REQUESTS_TOTAL.labels(**labels)._value.get()
    assert after == before + 1
