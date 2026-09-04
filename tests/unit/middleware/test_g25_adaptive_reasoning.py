"""G25 must classify the REQUEST, not the developer's static instructions.

Backlog #42, measured on DS8 2026-09-04 (`run-20260904-093125`). The all-on arm billed
7,945 output tokens of which **4,869 were reasoning** — 61% of the output bill, 41% of the
total — while delivering 3,076 tokens of answer text against the non-reasoning arm's 3,098.
Identical answers; the model was thinking, not writing. On `ds8-51` the reasoning answer
billed 122 tokens (89 reasoning) and still dropped a detail the 47-token answer included.

Root cause was two independent defects, either of which alone produced it:

A. `_extract_user_text` scored `system` messages alongside `user`. A system prompt is
   STATIC across a workload, so any medium/high keyword in it pins every request under
   that prompt to the same tier — which is the opposite of adaptive. DS8's actual question
   ("What are the API call limits for the Scale tier?") matched nothing; the tier came from
   the word 'explain' inside an 8,150-character policy document.
B. An unmatched request defaults to `medium`. For a keyword classifier "no match" is the
   COMMON case, so the default is the behaviour for most traffic.

A is fixed here (scoring user turns by default). B is deliberately left as a decision and
exposed as `default_effort` — cutting it to `low` is a quality trade, not a bug fix.

This module had NO test file before 2026-09-04, which is part of why the defect survived.
"""
import pytest

from middleware import g25_adaptive_reasoning as g25
from middleware.g25_adaptive_reasoning import (
    G25AdaptiveReasoning,
    _build_patterns,
    _classify_complexity,
    _extract_user_text,
)

_HI = _build_patterns(g25._DEFAULT_HIGH_KEYWORDS)
_ME = _build_patterns(g25._DEFAULT_MEDIUM_KEYWORDS)
_LO = _build_patterns(g25._DEFAULT_LOW_KEYWORDS)

# The real shape that caused #42: a trivial question under a long policy system prompt.
_POLICY_SYSTEM = (
    "You are a billing support assistant. Answer strictly from the policy below. "
    "Explain the tier limits when asked. " + ("Policy detail line. " * 200)
)
_TRIVIAL_USER = "What are the API call limits for the Scale tier?"


class TestTheSystemPromptMustNotDecideTheTier:
    def test_the_static_system_prompt_is_not_scored(self):
        msgs = [{"role": "system", "content": _POLICY_SYSTEM},
                {"role": "user", "content": _TRIVIAL_USER}]
        assert _extract_user_text(msgs) == _TRIVIAL_USER

    def test_the_exact_ds8_shape_no_longer_inherits_the_system_keyword(self):
        """The regression: 'explain' lives in the system prompt, not the question."""
        msgs = [{"role": "system", "content": _POLICY_SYSTEM},
                {"role": "user", "content": _TRIVIAL_USER}]
        _, reason = _classify_complexity(_extract_user_text(msgs), _HI, _ME, _LO)
        assert "explain" not in reason, (
            "the tier came from a keyword in the developer's static instructions — every "
            "request under that prompt would be classified identically"
        )

    def test_two_different_questions_can_now_differ_under_one_system_prompt(self):
        """Adaptive means adaptive. Scoring the system prompt made the tier constant."""
        def tier(user):
            msgs = [{"role": "system", "content": _POLICY_SYSTEM},
                    {"role": "user", "content": user}]
            return _classify_complexity(_extract_user_text(msgs), _HI, _ME, _LO)[0]

        simple = tier("What are the API call limits for the Scale tier?")
        hard = tier("Design an algorithm and give its time complexity analysis.")
        assert hard == "high"
        assert simple != hard, (
            "if these two collapse to one tier, the group cannot adapt to anything"
        )

    def test_an_operator_can_restore_the_old_behaviour(self):
        msgs = [{"role": "system", "content": _POLICY_SYSTEM},
                {"role": "user", "content": _TRIVIAL_USER}]
        text = _extract_user_text(msgs, ("user", "system"))
        assert _POLICY_SYSTEM in text and _TRIVIAL_USER in text


class TestDefaultEffort:
    def test_unmatched_text_still_defaults_to_medium(self):
        """Left deliberately at medium: dropping it to low is a quality decision, so it
        must not change as a side effect of the #42 fix."""
        effort, reason = _classify_complexity(_TRIVIAL_USER, _HI, _ME, _LO)
        assert effort == "medium"
        assert "no keyword match" in reason

    def test_the_default_is_configurable(self):
        effort, reason = _classify_complexity(
            _TRIVIAL_USER, _HI, _ME, _LO, default_effort="low")
        assert effort == "low"
        assert "defaulting to low" in reason

    def test_a_junk_default_falls_back_to_medium(self):
        """config.yaml is operator-edited; a typo must not invent a new effort level."""
        effort, _ = _classify_complexity(_TRIVIAL_USER, _HI, _ME, _LO, default_effort="turbo")
        assert effort == "medium"

    def test_keyword_matches_still_win_over_the_default(self):
        assert _classify_complexity("prove the time complexity", _HI, _ME, _LO)[0] == "high"


@pytest.mark.asyncio
class TestProcessRequest:
    def _cfg(self, minimal_config, **over):
        cfg = {"enabled": True}
        cfg.update(over)
        minimal_config["groups"]["G25_adaptive_reasoning"] = cfg
        return minimal_config

    async def _effort(self, make_ctx, minimal_config, user, system=None, **over):
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": user}]
        ctx = make_ctx(msgs, model="o4-mini", config=self._cfg(minimal_config, **over))
        ctx.routed_model = "o4-mini"
        # Without an adapter G25 short-circuits on "not a reasoning model" and every
        # assertion below would pass for the wrong reason.
        from providers.openai_adapter import OpenAIAdapter
        ctx.provider_adapter = OpenAIAdapter()
        out = await G25AdaptiveReasoning().process_request(ctx)
        return out.params.get("reasoning_effort")

    async def test_a_trivial_question_is_not_escalated_by_its_system_prompt(
            self, make_ctx, minimal_config):
        assert await self._effort(
            make_ctx, minimal_config, _TRIVIAL_USER, _POLICY_SYSTEM) == "medium"

    async def test_a_genuinely_complex_question_still_gets_high(
            self, make_ctx, minimal_config):
        assert await self._effort(
            make_ctx, minimal_config,
            "Design an algorithm and analyse its time complexity.", _POLICY_SYSTEM) == "high"

    async def test_scan_roles_is_honoured(self, make_ctx, minimal_config):
        """Opting the system prompt back in must actually change the outcome, or the
        knob is decorative."""
        effort = await self._effort(make_ctx, minimal_config, "prove the time complexity",
                                    None, scan_roles=["system"])
        assert effort == "medium", "with only system scanned, the user turn is ignored"

    async def test_an_empty_scan_roles_falls_back_to_user(self, make_ctx, minimal_config):
        assert await self._effort(make_ctx, minimal_config,
                                  "Design an algorithm and analyse its time complexity.",
                                  None, scan_roles=[]) == "high"

    async def test_default_effort_flows_through_to_the_request(
            self, make_ctx, minimal_config):
        assert await self._effort(make_ctx, minimal_config, _TRIVIAL_USER, _POLICY_SYSTEM,
                                  default_effort="low") == "low"

    async def test_a_caller_supplied_effort_is_never_overridden(
            self, make_ctx, minimal_config):
        from providers.openai_adapter import OpenAIAdapter
        ctx = make_ctx([{"role": "user", "content": _TRIVIAL_USER}], model="o4-mini",
                       config=self._cfg(minimal_config))
        ctx.routed_model = "o4-mini"
        ctx.provider_adapter = OpenAIAdapter()
        ctx.params["reasoning_effort"] = "high"
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params["reasoning_effort"] == "high"

    async def test_disabled_is_a_no_op(self, make_ctx, minimal_config):
        assert await self._effort(make_ctx, minimal_config, _TRIVIAL_USER,
                                  None, enabled=False) is None

    async def test_a_non_reasoning_model_is_untouched(self, make_ctx, minimal_config):
        from providers.openai_adapter import OpenAIAdapter
        ctx = make_ctx([{"role": "user", "content": _TRIVIAL_USER}], model="gpt-4o-mini",
                       config=self._cfg(minimal_config))
        ctx.routed_model = "gpt-4o-mini"
        # A REAL adapter, so this fails for the right reason: gpt-4o-mini genuinely does
        # not support reasoning. With adapter=None it would pass vacuously.
        ctx.provider_adapter = OpenAIAdapter()
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params.get("reasoning_effort") is None
