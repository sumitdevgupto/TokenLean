"""Unit tests for T07 — G18 USD cost model and token_opt_usd_saved_total metric."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import patch


def _make_response(prompt_tokens=100, completion_tokens=20):
    return {
        "id": "chatcmpl-t07",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


_PRICING = {
    "gpt-4o":         {"input": 0.005,   "output": 0.015},
    "claude-sonnet":  {"input": 0.003,   "output": 0.015},
    "gemini-pro":     {"input": 0.00125, "output": 0.005},
    "default":        {"input": 0.005,   "output": 0.015},
}


class TestCostPerModel:
    """get_cost_per_1k() must return per-model rates from the config pricing table."""

    def _get_cost(self, model: str):
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            from savings.calculator import get_cost_per_1k
            return get_cost_per_1k(model)

    def test_gpt4o_input_cost(self):
        inp, out = self._get_cost("gpt-4o")
        assert inp == 0.005
        assert out == 0.015

    def test_claude_sonnet_cost(self):
        inp, out = self._get_cost("claude-sonnet-3-5")
        assert inp == 0.003
        assert out == 0.015

    def test_gemini_pro_cost(self):
        inp, out = self._get_cost("gemini-pro-1.5")
        assert inp == 0.00125
        assert out == 0.005

    def test_unknown_model_returns_default(self):
        inp, out = self._get_cost("some-unknown-model")
        assert inp == pytest.approx(_PRICING["default"]["input"])
        assert out == pytest.approx(_PRICING["default"]["output"])

    def test_empty_pricing_table_uses_hardcoded_fallback(self):
        with patch("config_loader.get_pricing_table", return_value={}):
            from savings.calculator import get_cost_per_1k
            inp, out = get_cost_per_1k("gpt-4o")
        # Falls back to hardcoded default when no pricing table entry or default key
        assert inp > 0
        assert out > 0


@pytest.mark.asyncio
class TestUsdSavedCounter:
    """token_opt_usd_saved_total must be emitted per group when prometheus_enabled."""

    async def test_counter_incremented_per_group(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.add_step("G01", "compression", 200, 120)  # 80 tokens saved
        ctx.savings.add_step("G05", "cache", 50, 50)          # 0 tokens saved — must NOT emit

        from middleware.g18_observability import G18Observability, USD_SAVED

        before_g01 = USD_SAVED.labels(group="G01", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()
        before_g05 = USD_SAVED.labels(group="G05", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()

        with patch("middleware.langfuse_tracing.finish_trace"), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            await G18Observability().record(ctx, _make_response())

        after_g01 = USD_SAVED.labels(group="G01", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()
        after_g05 = USD_SAVED.labels(group="G05", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()

        expected_usd = round(80 / 1000.0 * _PRICING["gpt-4o"]["input"], 8)
        assert after_g01 == pytest.approx(before_g01 + expected_usd, rel=1e-5)
        assert after_g05 == before_g05

    async def test_counter_uses_model_label(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.add_step("G01", "compression", 100, 50)

        from middleware.g18_observability import G18Observability, USD_SAVED

        with patch("middleware.langfuse_tracing.finish_trace"), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            await G18Observability().record(ctx, _make_response())

        # Counter must be labelable by model
        val = USD_SAVED.labels(group="G01", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()
        assert val > 0

    async def test_counter_uses_tenant_id_label(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.tenant_id = "nova-med"
        ctx.savings.add_step("G07", "rag", 300, 200)

        from middleware.g18_observability import G18Observability, USD_SAVED

        before = USD_SAVED.labels(group="G07", model="gpt-4o", tenant_id="nova-med")._value.get()

        with patch("middleware.langfuse_tracing.finish_trace"), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            await G18Observability().record(ctx, _make_response())

        after = USD_SAVED.labels(group="G07", model="gpt-4o", tenant_id="nova-med")._value.get()
        assert after > before

    async def test_no_steps_no_usd_emission(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        # No step savings added

        from middleware.g18_observability import G18Observability, USD_SAVED

        before = sum(
            s._value.get()
            for s in USD_SAVED._metrics.values()
        )

        with patch("middleware.langfuse_tracing.finish_trace"), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            await G18Observability().record(ctx, _make_response())

        after = sum(
            s._value.get()
            for s in USD_SAVED._metrics.values()
        )
        assert after == before

    async def test_counter_disabled_when_prometheus_disabled(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.config["groups"]["G18_observability"]["prometheus_enabled"] = False
        ctx.savings.add_step("G01", "compression", 200, 100)

        from middleware.g18_observability import G18Observability, USD_SAVED

        before = USD_SAVED.labels(group="G01", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()

        with patch("middleware.langfuse_tracing.finish_trace"), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            await G18Observability().record(ctx, _make_response())

        after = USD_SAVED.labels(group="G01", model="gpt-4o", tenant_id=ctx.tenant_id)._value.get()
        assert after == before


class TestUsdSavedCounterLabels:
    """token_opt_usd_saved_total must not carry unbounded labels (cardinality risk)."""

    def test_usd_saved_label_names(self):
        from middleware.g18_observability import USD_SAVED
        assert set(USD_SAVED._labelnames) == {"group", "model", "tenant_id"}
        assert "user_id" not in USD_SAVED._labelnames
        assert "team" not in USD_SAVED._labelnames
        assert "feature" not in USD_SAVED._labelnames


class TestCacheWriteCostAndMetrics:
    """#34 — G18 must price cache WRITES and expose both halves as metrics.

    Before this, G18 read only the discounted cached-READ count and applied a read
    multiplier; cache creation — the expensive half, and the half that grows when a prefix
    churns — was read by nothing at all.
    """

    @staticmethod
    def _response(prompt=1000, completion=20, cached=600, written=300, written_1h=None):
        details = {"cached_tokens": cached}
        if written is not None:
            details["cache_write_tokens"] = written
        if written_1h is not None:
            details["cache_creation_token_details"] = {"ephemeral_1h_input_tokens": written_1h}
        return {
            "id": "chatcmpl-cache",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "prompt_tokens_details": details},
        }

    async def _record(self, ctx, response):
        from middleware.g18_observability import G18Observability
        with patch("middleware.langfuse_tracing.finish_trace"),              patch("config_loader.get_pricing_table", return_value=_PRICING):
            await G18Observability().record(ctx, response)

    async def test_cache_counts_land_on_savings(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        await self._record(ctx, self._response())
        assert ctx.savings.cache_read_tokens == 600
        assert ctx.savings.cache_write_tokens == 300

    async def test_unreported_cache_stays_none_not_zero(self, make_ctx):
        """A provider that says nothing must not be recorded as 'zero cache activity'."""
        ctx = make_ctx(model="gpt-4o")
        await self._record(ctx, _make_response())
        assert ctx.savings.cache_write_tokens is None
        assert ctx.savings.cost_cache_write_usd is None

    async def test_cost_split_is_recorded_and_bounded_by_total(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        await self._record(ctx, self._response())
        read_usd = ctx.savings.cost_cache_read_usd
        write_usd = ctx.savings.cost_cache_write_usd
        assert read_usd is not None and write_usd is not None
        assert read_usd + write_usd <= ctx.savings.cost_actual_usd + 1e-9

    async def test_writes_raise_cost_above_a_read_only_call(self, make_ctx):
        """The whole point: churn costs money even though token counts look identical."""
        from providers.anthropic_adapter import AnthropicAdapter
        read_ctx = make_ctx(model="gpt-4o")
        read_ctx.provider_adapter = AnthropicAdapter()
        await self._record(read_ctx, self._response(cached=900, written=None))

        write_ctx = make_ctx(model="gpt-4o")
        write_ctx.provider_adapter = AnthropicAdapter()
        await self._record(write_ctx, self._response(cached=0, written=900))

        assert write_ctx.savings.cost_actual_usd > read_ctx.savings.cost_actual_usd

    async def test_share_of_bill_is_disclosed(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        await self._record(ctx, self._response())
        meta = ctx.savings.to_langfuse_metadata()
        assert meta["cache_read_tokens"] == 600
        assert meta["cache_write_tokens"] == 300
        assert 0 < meta["cache_share_of_bill_pct"] <= 100

    async def test_counters_increment(self, make_ctx):
        from middleware.g18_observability import CACHE_READ_TOKENS, CACHE_WRITE_TOKENS
        ctx = make_ctx(model="gpt-4o")
        labels = dict(model="gpt-4o", team="default", feature="default", tenant_id=ctx.tenant_id)
        before_r = CACHE_READ_TOKENS.labels(**labels)._value.get()
        before_w = CACHE_WRITE_TOKENS.labels(**labels)._value.get()
        await self._record(ctx, self._response())
        assert CACHE_READ_TOKENS.labels(**labels)._value.get() == before_r + 600
        assert CACHE_WRITE_TOKENS.labels(**labels)._value.get() == before_w + 300

    async def test_adapter_failure_degrades_to_unknown_not_crash(self, make_ctx):
        """A misbehaving adapter must never cost the caller their response."""
        class Exploding:
            name = "boom"
            def extract_usage(self, response):
                raise RuntimeError("adapter blew up")
        ctx = make_ctx(model="gpt-4o")
        ctx.provider_adapter = Exploding()
        await self._record(ctx, self._response())
        assert ctx.savings.cache_write_tokens is None
        assert ctx.savings.cost_actual_usd >= 0

