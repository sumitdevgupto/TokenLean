"""G06 -> G25 -> G12: selecting `off`, and recording honestly what happened (backlog #42).

The shipped behaviour change is narrow and deliberate: when G06's classifier -- which
already ran, to pick a model tier -- said `simple`, G25 selects `off` instead of running a
second, differently tuned keyword classifier over the same text. Everything else falls
through unchanged.

The two classifiers disagreed materially, which is why reusing G06's answer is the fix
rather than a shortcut: `explain` / `compare` / `analyse` are COMPLEX to G06 and MEDIUM to
G25, and G06 weighs word count while G25 has no size signal at all.

G12 then records what the PROVIDER can deliver, not what was asked. That distinction is the
whole point: on an OpenAI o-series model `off` omits `reasoning_effort` and the model keeps
reasoning anyway, so crediting a reasoning saving there would be a claim we cannot support.
"""
import pytest

from middleware.g12_reasoning_budget import G12ReasoningBudget
from middleware.g25_adaptive_reasoning import G25AdaptiveReasoning
from providers.anthropic_adapter import AnthropicAdapter
from providers.openai_adapter import OpenAIAdapter

_TRIVIAL = "What are the API call limits for the Scale tier?"
_HARD = "Design an algorithm and analyse its time complexity."


def _cfg(minimal_config, g25=None, g12=None):
    minimal_config["groups"]["G25_adaptive_reasoning"] = {"enabled": True, **(g25 or {})}
    minimal_config["groups"]["G12_reasoning"] = {
        "enabled": True,
        "effort_map": {"low": {"openai": "low", "anthropic_tokens": 1000},
                       "medium": {"openai": "medium", "anthropic_tokens": 5000}},
        **(g12 or {}),
    }
    return minimal_config


def _ctx(make_ctx, minimal_config, text, model, adapter, tier=None, **over):
    ctx = make_ctx([{"role": "user", "content": text}], model=model,
                   config=_cfg(minimal_config, g25=over.pop("g25", None),
                               g12=over.pop("g12", None)))
    ctx.routed_model = model
    ctx.provider_adapter = adapter
    ctx.complexity_tier = tier
    return ctx


@pytest.mark.asyncio
class TestG25ReusesG06sDecision:
    async def test_simple_selects_off(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), "simple")
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "off"

    async def test_the_SHIPPED_effort_floor_does_not_undo_it(self, make_ctx, minimal_config):
        """Read the floor out of the REAL config template, not a synthetic fixture.

        This is the test that caught the first attempt. `effort_floor` is a rung ABOVE
        `off`, and the shipped config sets it EXPLICITLY -- so a bypass keyed on "the
        operator did not set a floor" was true in a hand-built fixture and false in
        production: the fix passed its tests and did nothing live. Sourcing the value from
        config.yaml.template is what makes this assertion mean something.
        """
        import pathlib, yaml
        tpl = pathlib.Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
        shipped = yaml.safe_load(tpl.read_text(encoding="utf-8"))[
            "groups"]["G25_adaptive_reasoning"]
        assert shipped.get("effort_floor") == "off", (
            "the shipped floor must permit `off`, or the G06 bridge is clamped away and "
            "this feature is inert in every real deployment"
        )
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), "simple",
                   g25={"effort_floor": shipped["effort_floor"]})
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "off"

    async def test_the_shipped_floor_is_not_the_yaml_boolean_false(self):
        """`off` unquoted is boolean False in YAML 1.1. As a floor value that would compare
        unequal to the string tier and silently fall back to index 0 by accident rather
        than by intent."""
        import pathlib, yaml
        tpl = pathlib.Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
        cfg = yaml.safe_load(tpl.read_text(encoding="utf-8"))["groups"]
        assert isinstance(cfg["G25_adaptive_reasoning"]["effort_floor"], str)
        assert isinstance(cfg["G25_adaptive_reasoning"]["routing_complexity_map"]["simple"], str)
        assert "off" in cfg["G12_reasoning"]["effort_map"], (
            "bare `off:` parses as the boolean False, so the tier row would never match"
        )

    async def test_an_explicit_operator_floor_is_still_respected(self, make_ctx, minimal_config):
        """The floor is applied literally, with no special case: an operator who sets
        `low` gets at least `low`, and a deployment whose config predates the `off` rung
        keeps its current behaviour until that value is changed."""
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), "simple",
                   g25={"effort_floor": "low"})
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "low"

    @pytest.mark.parametrize("tier", ["medium", "complex", None])
    async def test_every_other_tier_falls_through_unchanged(self, make_ctx, minimal_config, tier):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), tier)
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "medium"

    async def test_a_hard_question_is_untouched_even_when_g06_said_simple(
            self, make_ctx, minimal_config):
        """G06 classified the ROUTE; if it says simple we trust it. This pins that the
        mapping is what decides, so a future reader can see the coupling is deliberate."""
        ctx = _ctx(make_ctx, minimal_config, _HARD, "o4-mini", OpenAIAdapter(), "simple",
                   g25={"routing_complexity_map": {}})
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "high"

    async def test_the_bridge_can_be_switched_off(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), "simple",
                   g25={"use_routing_complexity": False})
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "medium"

    async def test_a_junk_map_value_is_ignored_not_forwarded(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), "simple",
                   g25={"routing_complexity_map": {"simple": "turbo"}})
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "medium"

    async def test_a_caller_supplied_effort_still_wins(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter(), "simple")
        ctx.params["reasoning_effort"] = "high"
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "high"


@pytest.mark.asyncio
class TestG12RealisesOff:
    async def test_anthropic_sends_no_thinking_param(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "claude-sonnet-4-5", AnthropicAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert "thinking" not in out.params

    async def test_the_stray_reasoning_effort_is_cleared(self, make_ctx, minimal_config):
        """`off` emits nothing, so `applied` stays False and the existing pop never runs.
        A surviving `reasoning_effort` would be expanded back into a thinking budget by
        litellm downstream -- re-enabling exactly what was just turned off."""
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "claude-sonnet-4-5", AnthropicAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert "reasoning_effort" not in out.params
        assert "thinking" not in out.params

    async def test_gemini_keeps_its_explicit_zero(self, make_ctx, minimal_config):
        """Gemini's `off` IS a param. Clearing it would leave thinking on by default."""
        from providers.gemini_adapter import GeminiAdapter
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "gemini-2.5-pro", GeminiAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert out.params["thinking_config"] == {"thinking_budget": 0}
        assert "reasoning_effort" not in out.params

    async def test_no_suppression_prompt_is_injected_for_off(self, make_ctx, minimal_config):
        """A suppression prompt costs input tokens to ask for less output. With reasoning
        already off there is nothing to suppress, so paying for it would be pure waste."""
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "claude-sonnet-4-5", AnthropicAdapter(),
                   g12={"reasoning_suppression_prompts": {"low": "[BUDGET] terse"}})
        ctx.params["reasoning_effort"] = "off"
        before = list(ctx.messages)
        out = await G12ReasoningBudget().process_request(ctx)
        assert out.messages == before


@pytest.mark.asyncio
class TestTheOutcomeIsRecordedHonestly:
    async def test_anthropic_records_off_honoured(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "claude-sonnet-4-5", AnthropicAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert out.reasoning_mode == "off_honoured"

    async def test_openai_o_series_records_off_unsupported(self, make_ctx, minimal_config):
        """The o-series reasons regardless. Recording this as `off_honoured` would let a
        report claim a reasoning saving on requests that still billed reasoning tokens."""
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert out.reasoning_mode == "off_unsupported"

    async def test_the_two_off_outcomes_are_never_collapsed(self, make_ctx, minimal_config):
        a = _ctx(make_ctx, minimal_config, _TRIVIAL, "claude-sonnet-4-5", AnthropicAdapter())
        a.params["reasoning_effort"] = "off"
        o = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter())
        o.params["reasoning_effort"] = "off"
        g12 = G12ReasoningBudget()
        assert (await g12.process_request(a)).reasoning_mode != \
               (await g12.process_request(o)).reasoning_mode

    async def test_an_applied_tier_records_that_tier(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "o4-mini", OpenAIAdapter())
        ctx.params["reasoning_effort"] = "medium"
        out = await G12ReasoningBudget().process_request(ctx)
        assert out.reasoning_mode == "medium"

    async def test_off_is_still_recorded_as_a_decision(self, make_ctx, minimal_config):
        """`off` applies no params, so the old `if applied or suppression` guard would
        have skipped observability entirely and the group would look like it never ran."""
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "claude-sonnet-4-5", AnthropicAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert any(s.group == "G12" for s in out.savings.step_savings)

    async def test_a_non_reasoning_model_records_nothing(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, _TRIVIAL, "gpt-4o-mini", OpenAIAdapter())
        ctx.params["reasoning_effort"] = "off"
        out = await G12ReasoningBudget().process_request(ctx)
        assert out.reasoning_mode is None
