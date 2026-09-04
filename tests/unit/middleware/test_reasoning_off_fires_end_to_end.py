"""Does the `off` chain actually FIRE on the workload that motivated it? (backlog #42)

Gate 8.6 of the feature-development standards: assert the mechanism fires in its own arm
before trusting any verdict. Three of this repo's measurement failures were arms that were
silently inert while reporting a number, so a fix that is correct in isolation but never
reached in production is the specific failure mode being guarded against here.

This runs the REAL G06 -> G25 -> G12 sequence over a DS8-shaped request: a one-line billing
question behind a long static policy prompt, routed to Claude. That is the exact shape that
billed 4,869 reasoning tokens (61% of the output bill) for answers no longer than the
non-reasoning arm's.

It also pins the honest negative: the same request on the OpenAI half of DS8 changes
nothing, because gpt-4o-mini never reasoned in the first place.
"""
import pytest

from middleware.g06_routing import G06Routing
from middleware.g12_reasoning_budget import G12ReasoningBudget
from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
from providers.anthropic_adapter import AnthropicAdapter
from providers.openai_adapter import OpenAIAdapter

# The real DS8 shape: a static policy prompt containing 'explain' (which used to decide the
# tier all by itself), plus a trivial question that matches no complexity keyword.
_POLICY = ("You are a billing support assistant. Explain the tier limits when asked. "
           + ("Policy detail line. " * 200))
_QUESTION = "What are the API call limits for the Scale tier?"

# DS8's own manifest pins every tier to one model per provider so cache costs are priced
# under a single model. Reproduced here, because it means routing cannot move the model --
# only the reasoning parameter can change.
_TIERS = {"simple": ["claude-sonnet-4-5"], "medium": ["claude-sonnet-4-5"],
          "complex": ["claude-sonnet-4-5"]}


def _config(minimal_config, tiers, **g25):
    minimal_config["groups"]["G6_routing"] = {
        "enabled": True, "classifier": "heuristic", "tiers": tiers,
        "cascade_execution": False,
    }
    minimal_config["groups"]["G25_adaptive_reasoning"] = {"enabled": True, **g25}
    minimal_config["groups"]["G12_reasoning"] = {
        "enabled": True, "default_effort": "medium",
        "effort_map": {"low": {"openai": "low", "anthropic_tokens": 1000},
                       "medium": {"openai": "medium", "anthropic_tokens": 5000}},
    }
    return minimal_config


async def _run(make_ctx, minimal_config, model, adapter, tiers=None, **g25):
    ctx = make_ctx(
        [{"role": "system", "content": _POLICY}, {"role": "user", "content": _QUESTION}],
        model=model, config=_config(minimal_config, tiers or _TIERS, **g25))
    ctx.provider_adapter = adapter
    ctx = await G06Routing().process_request(ctx)
    ctx.provider_adapter = adapter          # G06 may re-resolve; keep the test deterministic
    ctx.routed_model = ctx.routed_model or model
    ctx = await G25AdaptiveReasoning().process_request(ctx)
    return await G12ReasoningBudget().process_request(ctx)


@pytest.mark.asyncio
class TestTheChainFiresOnTheWorkloadThatMotivatedIt:
    async def test_g06_classifies_the_ds8_question_simple(self, make_ctx, minimal_config):
        """If this stops being `simple`, every assertion below is vacuous."""
        out = await _run(make_ctx, minimal_config, "claude-sonnet-4-5", AnthropicAdapter())
        assert out.complexity_tier == "simple"

    async def test_no_thinking_param_reaches_anthropic(self, make_ctx, minimal_config):
        """The whole point. Before this, `thinking` was enabled on every one of these."""
        out = await _run(make_ctx, minimal_config, "claude-sonnet-4-5", AnthropicAdapter())
        assert "thinking" not in out.params
        assert "reasoning_effort" not in out.params
        assert out.reasoning_mode == "off_honoured"

    async def test_the_static_system_prompt_no_longer_decides_it(
            self, make_ctx, minimal_config):
        """`explain` sits in the policy prompt. Scoring it pinned every request under that
        prompt to one tier -- and it is still there, so this proves the path is clean."""
        assert "Explain" in _POLICY
        out = await _run(make_ctx, minimal_config, "claude-sonnet-4-5", AnthropicAdapter())
        assert out.reasoning_mode == "off_honoured"

    async def test_disabling_the_bridge_restores_the_old_billing_shape(
            self, make_ctx, minimal_config):
        """The before-picture, kept executable: with the bridge off, thinking comes back
        at a 5,000-token budget on a one-line FAQ lookup."""
        out = await _run(make_ctx, minimal_config, "claude-sonnet-4-5", AnthropicAdapter(),
                         use_routing_complexity=False)
        assert out.params["thinking"] == {"type": "enabled", "budget_tokens": 5000}
        assert out.reasoning_mode == "medium"


@pytest.mark.asyncio
class TestTheHonestNegative:
    async def test_the_openai_half_of_ds8_is_unaffected(self, make_ctx, minimal_config):
        """gpt-4o-mini is not a reasoning model, so there was never anything to save here.
        Saying otherwise would inflate the measured benefit by counting 50 of DS8's 60
        requests that cannot possibly improve."""
        tiers = {k: ["gpt-4o-mini"] for k in ("simple", "medium", "complex")}
        out = await _run(make_ctx, minimal_config, "gpt-4o-mini", OpenAIAdapter(), tiers)
        assert out.reasoning_mode is None
        assert "reasoning_effort" not in out.params
        assert "thinking" not in out.params

    async def test_an_o_series_route_records_that_it_could_not_comply(
            self, make_ctx, minimal_config):
        """`off` is selected, but the o-series reasons regardless -- so this must NOT be
        recorded as a reasoning saving."""
        tiers = {k: ["o4-mini"] for k in ("simple", "medium", "complex")}
        out = await _run(make_ctx, minimal_config, "o4-mini", OpenAIAdapter(), tiers)
        assert out.reasoning_mode == "off_unsupported"
