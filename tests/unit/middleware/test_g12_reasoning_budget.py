"""Unit tests for G12 — Reasoning Budget Control."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from providers.anthropic_adapter import AnthropicAdapter
from providers.gemini_adapter import GeminiAdapter
from providers.openai_adapter import OpenAIAdapter


@pytest.mark.asyncio
class TestG12ReasoningBudget:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx(model="o1")
        ctx.config["groups"]["G12_reasoning"]["enabled"] = False
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in ctx.params

    async def test_o1_model_injects_reasoning_effort(self, make_ctx):
        ctx = make_ctx(model="o1")
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" in ctx.params
        assert ctx.params["reasoning_effort"] == "medium"

    async def test_o3_model_injects_reasoning_effort(self, make_ctx):
        ctx = make_ctx(model="o3")
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" in ctx.params

    async def test_claude_model_injects_thinking_budget(self, make_ctx):
        # A request that asks for reasoning. G12's own default never switches thinking on
        # (TestTheDefaultNeverSwitchesReasoningOn).
        ctx = make_ctx(model="claude-sonnet-4-5", params={"reasoning_effort": "medium"})
        ctx.provider_adapter = AnthropicAdapter()
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "thinking" in ctx.params

    async def test_non_reasoning_model_unchanged(self, make_ctx):
        # gpt-4o-mini: OpenAI adapter's supports_reasoning returns False
        ctx = make_ctx(model="gpt-4o-mini")
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in ctx.params
        assert "thinking" not in ctx.params

    async def test_effort_low_overrides_default(self, make_ctx):
        ctx = make_ctx(model="o1")
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)
        assert ctx.params.get("reasoning_effort") == "low"


@pytest.mark.asyncio
class TestTheDefaultNeverSwitchesReasoningOn:
    """A request that names no reasoning gets the platform's default effort, capped at the
    provider's own default as G25 caps its choice. On Anthropic, where extended thinking is
    opt-in, the default `medium` switched thinking on for requests that never asked, and the
    customer paid for it. A request's own setting, and a default the tenant chose, are used as
    they are; the suppression prompt still follows the configured effort."""

    @staticmethod
    async def _run(ctx):
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        return await G12ReasoningBudget().process_request(ctx)

    @staticmethod
    def _claude(make_ctx, **params):
        ctx = make_ctx([{"role": "system", "content": "Be helpful."},
                        {"role": "user", "content": "Summarise this."}],
                       model="claude-sonnet-4-5", params=params)
        ctx.provider_adapter = AnthropicAdapter()
        return ctx

    async def test_a_claude_request_that_asks_nothing_gets_no_thinking(self, make_ctx):
        out = await self._run(self._claude(make_ctx))
        assert "thinking" not in out.params and "reasoning_effort" not in out.params
        assert out.reasoning_mode == "off_honoured"
        # The suppression prompt still follows the configured effort, as for any model.
        assert out.messages[0]["content"].endswith("[BUDGET] Keep reasoning minimal.")
        steps = [s.description for s in out.savings.step_savings if s.group == "G12"]
        assert len(steps) == 1 and "effort=off" in steps[0]
        assert "default medium capped at the provider's own" in steps[0]

    @pytest.mark.parametrize("model, adapter, param", [
        ("o1", OpenAIAdapter(), "reasoning_effort"),              # reasons at medium anyway
        ("gemini-2.5-pro", GeminiAdapter(), "thinking_config"),   # thinks by default
    ])
    async def test_a_provider_that_reasons_by_default_keeps_the_default(
            self, make_ctx, model, adapter, param):
        ctx = make_ctx(model=model)
        ctx.provider_adapter = adapter
        out = await self._run(ctx)
        assert param in out.params and out.reasoning_mode == "medium"

    async def test_a_platform_default_above_the_provider_s_is_capped(self, make_ctx):
        ctx = make_ctx(model="o1")
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "high"
        out = await self._run(ctx)
        assert out.params.get("reasoning_effort") == "medium" and out.reasoning_mode == "medium"

    async def test_without_a_suppression_prompt_the_cap_is_still_recorded(self, make_ctx):
        ctx = self._claude(make_ctx)
        del ctx.config["groups"]["G12_reasoning"]["reasoning_suppression_prompts"]
        out = await self._run(ctx)
        steps = [s.description for s in out.savings.step_savings if s.group == "G12"]
        assert len(steps) == 1 and "effort=off" in steps[0] and "capped" in steps[0]

    async def test_an_effort_g12_does_not_know_is_passed_on_as_before(self, make_ctx):
        ctx = make_ctx(model="o1")
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "minimal"
        try:
            out = await self._run(ctx)
        except Exception as exc:
            pytest.fail(f"G12 raised {exc!r}")
        assert "reasoning_effort" not in out.params   # the adapter emits nothing for it

    @pytest.mark.parametrize("own", [{"reasoning_effort": "high"},
                                     {"thinking": {"type": "enabled", "budget_tokens": 3000}},
                                     {"thinking": {"type": "disabled"}}],
                             ids=["reasoning_effort", "thinking on", "thinking off"])
    async def test_a_request_s_own_setting_is_used_as_it_is(self, make_ctx, own):
        out = await self._run(self._claude(make_ctx, **own))
        expected = own.get("thinking") or {"type": "enabled", "budget_tokens": 20000}
        assert out.params.get("thinking") == expected

    @pytest.mark.parametrize("source", ["portal", "operator"])
    async def test_a_default_the_tenant_chose_is_used_as_it_is(self, make_ctx, source):
        ctx = self._claude(make_ctx)
        ctx.tenant_id = "acme"
        chosen = {"groups": {"G12_reasoning": {"default_effort": "high"}}}
        if source == "portal":
            ctx.tenant_config_overrides = chosen
        else:
            ctx.config["tenants"] = {"acme": chosen}
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "high"   # merged in
        out = await self._run(ctx)
        assert out.params.get("thinking") == {"type": "enabled", "budget_tokens": 20000}

    async def test_a_tenant_who_set_other_g12_knobs_still_gets_the_capped_default(self, make_ctx):
        ctx = self._claude(make_ctx)
        ctx.tenant_config_overrides = {"groups": {"G12_reasoning": {"enabled": True}}}
        out = await self._run(ctx)
        assert "thinking" not in out.params

    async def test_an_operator_may_raise_a_provider_s_own_default(self, make_ctx):
        ctx = self._claude(make_ctx)
        ctx.config["providers"] = [{"name": "anthropic", "default_reasoning_effort": "medium"}]
        out = await self._run(ctx)
        assert out.params.get("thinking") == {"type": "enabled", "budget_tokens": 5000}

    async def test_a_provider_that_cannot_say_leaves_the_default_alone(self, make_ctx):
        class _Mute(OpenAIAdapter):
            def default_reasoning_effort(self, model, config=None):
                raise RuntimeError("no idea")

        ctx = make_ctx(model="o1")
        ctx.provider_adapter = _Mute()
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "high"
        try:
            out = await self._run(ctx)
        except Exception as exc:
            pytest.fail(f"G12 raised {exc!r}")
        assert out.params.get("reasoning_effort") == "high"
