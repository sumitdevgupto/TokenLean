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
