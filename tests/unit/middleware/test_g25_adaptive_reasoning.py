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

    async def test_a_genuinely_complex_question_is_capped_at_the_ceiling(
            self, make_ctx, minimal_config):
        """Re-expressed under leader decision D-060 (2026-09-08). This used to assert
        that a complex question reaches `high`. The classifier still says high -- see
        the direct `_classify_complexity` test above -- but the APPLIED effort is now
        capped at the default ceiling `medium`, which IS the o-series provider default.
        G25 is non-increasing by default: measured on DS18, escalating to `high` spent
        2.7x the reasoning tokens (+37.8% output) while the arm's own facts gate passed
        30/30, so the customer paid for reasoning that bought no checked fact. An
        operator who wants escalation opts in with `effort_ceiling: high` (asserted
        below), which is exactly the point -- the default no longer decides it."""
        assert await self._effort(
            make_ctx, minimal_config,
            "Design an algorithm and analyse its time complexity.", _POLICY_SYSTEM) == "medium"

    async def test_an_operator_can_still_opt_in_to_escalation(
            self, make_ctx, minimal_config):
        """Re-expressed again under leader decision D-076 (2026-09-08). Escalation now
        takes TWO opt-ins on a provider whose own default is lower than the ceiling: the
        ceiling itself, and `escalate_above_provider_default`. The subject of this test —
        that an operator CAN still reach `high` — is unchanged; what changed is that
        raising the config ceiling alone no longer pushes a request above what the
        provider would have served, which was the bug F2 reported."""
        assert await self._effort(
            make_ctx, minimal_config,
            "Design an algorithm and analyse its time complexity.", _POLICY_SYSTEM,
            effort_ceiling="high", escalate_above_provider_default=True) == "high"

    async def test_the_config_ceiling_alone_no_longer_escalates(
            self, make_ctx, minimal_config):
        """The other half of D-076, pinned so it cannot regress silently: `high` without
        the escalation opt-in is clamped to the o-series provider default."""
        assert await self._effort(
            make_ctx, minimal_config,
            "Design an algorithm and analyse its time complexity.", _POLICY_SYSTEM,
            effort_ceiling="high") == "medium"

    async def test_scan_roles_is_honoured(self, make_ctx, minimal_config):
        """Opting the system prompt back in must actually change the outcome, or the
        knob is decorative."""
        effort = await self._effort(make_ctx, minimal_config, "prove the time complexity",
                                    None, scan_roles=["system"])
        assert effort == "medium", "with only system scanned, the user turn is ignored"

    async def test_an_empty_scan_roles_falls_back_to_user(self, make_ctx, minimal_config):
        """The point of this test is that an empty `scan_roles` still scores the USER
        turn. Re-expressed under D-060: it now pins the escalation at the ceiling, so
        raise the ceiling here to keep proving the fallback rather than the clamp (the
        clamp has its own test above). D-076 added the second opt-in the same way, for
        the same reason: the subject here is the scan_roles fallback, not the clamp."""
        assert await self._effort(
            make_ctx, minimal_config,
            "Design an algorithm and analyse its time complexity.",
            None, scan_roles=[], effort_ceiling="high",
            escalate_above_provider_default=True) == "high"

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


# ── The clamp: two ceilings, both of which must hold ────────────────────────────────
# Review W20-R-G25 findings F1 / F2 / F6, leader decision D-076 (2026-09-08).

_COMPLEX_USER = "Design an algorithm and analyse its time complexity."
_MEDIUM_USER = "Explain why the checkout latency regressed."

# (label, adapter factory, a model that provider actually reasons with)
_PROVIDER_CASES = [
    ("openai-o-series", "providers.openai_adapter:OpenAIAdapter", "o4-mini"),
    ("anthropic", "providers.anthropic_adapter:AnthropicAdapter", "claude-sonnet-5"),
    ("gemini", "providers.gemini_adapter:GeminiAdapter", "gemini-2.5-flash"),
]


def _adapter(path):
    mod, cls = path.split(":")
    import importlib
    return getattr(importlib.import_module(mod), cls)()


async def _apply(make_ctx, minimal_config, *, user, model, adapter, **over):
    """Run G25 over one request and return the effort it applied (or None)."""
    cfg = {"enabled": True}
    cfg.update(over)
    minimal_config["groups"]["G25_adaptive_reasoning"] = cfg
    ctx = make_ctx([{"role": "user", "content": user}], model=model, config=minimal_config)
    ctx.routed_model = model
    ctx.provider_adapter = adapter
    out = await G25AdaptiveReasoning().process_request(ctx)
    return out.params.get("reasoning_effort")


def test_the_premise_a_bare_off_is_the_boolean_false():
    """Not a hypothetical: this is what PyYAML does to the idiom the config file itself
    invites (`effort_floor`'s comment tells operators to quote `off`)."""
    import yaml
    assert yaml.safe_load("effort_ceiling: off") == {"effort_ceiling": False}


@pytest.mark.asyncio
class TestAMalformedCeilingFailsSafe:
    """F1: an unreadable `effort_ceiling` used to fail OPEN to `high`.

    `_order.get(effort_ceiling, _last)` fell back to the LAST tier, so a typo, a null, or
    the bare `off` the config's own floor comment invites (YAML parses it as the boolean
    False) silently selected the most expensive setting the group can pick — on billed
    traffic, with nothing logged. A ceiling that cannot be read must fall back to the code
    default, which is the whole point of having one.
    """

    async def _effort(self, make_ctx, minimal_config, ceiling):
        return await _apply(
            make_ctx, minimal_config, user=_COMPLEX_USER, model="o4-mini",
            adapter=_adapter(_PROVIDER_CASES[0][1]), effort_ceiling=ceiling,
            escalate_above_provider_default=True,   # isolate the CONFIG ceiling
        )

    @pytest.mark.parametrize("ceiling", [False, None, "", "hgih", "HIGH ", 3, ["high"],
                                         {"tier": "high"}])
    async def test_an_unreadable_ceiling_uses_the_code_default_not_high(
            self, make_ctx, minimal_config, ceiling):
        effort = await self._effort(make_ctx, minimal_config, ceiling)
        assert effort == g25._CODE_DEFAULT_CEILING, (
            f"effort_ceiling={ceiling!r} produced {effort!r}. A ceiling the proxy cannot "
            f"read must fall back to the code default, never open to the most expensive "
            f"tier the group can select."
        )

    async def test_a_readable_ceiling_is_still_honoured(self, make_ctx, minimal_config):
        assert await self._effort(make_ctx, minimal_config, "high") == "high"
        assert await self._effort(make_ctx, minimal_config, "LOW") == "low"

    async def test_the_bad_value_is_logged_once_per_distinct_value(
            self, make_ctx, minimal_config, caplog):
        """A silent fallback is how the old one survived; a per-request WARNING is how a
        deployment drowns. Once per distinct value is the compromise."""
        import logging
        g = G25AdaptiveReasoning()
        minimal_config["groups"]["G25_adaptive_reasoning"] = {
            "enabled": True, "effort_ceiling": False,
        }
        with caplog.at_level(logging.WARNING, logger=g25.__name__):
            for _ in range(3):
                ctx = make_ctx([{"role": "user", "content": _COMPLEX_USER}],
                               model="o4-mini", config=minimal_config)
                ctx.routed_model = "o4-mini"
                ctx.provider_adapter = _adapter(_PROVIDER_CASES[0][1])
                await g.process_request(ctx)
        warnings = [r for r in caplog.records if "effort_ceiling" in r.getMessage()]
        assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"

    async def test_an_unreadable_floor_still_fails_to_the_bottom_rung(
            self, make_ctx, minimal_config):
        """The floor's fallback is deliberately the OPPOSITE direction and stays as it
        was: a floor that cannot be read must not RAISE anything."""
        effort = await _apply(
            make_ctx, minimal_config, user=_TRIVIAL_USER, model="o4-mini",
            adapter=_adapter(_PROVIDER_CASES[0][1]), effort_floor="nonsense",
            default_effort="low")
        assert effort == "low"


@pytest.mark.asyncio
class TestTheProviderDefaultIsTheOtherCeiling:
    """F2: "medium is the provider default" was only ever true of the OpenAI o-series.

    Anthropic's extended thinking is opt-in — omitting `thinking` is off — so G25 selecting
    `medium` there does not lower a bill, it turns thinking ON and raises one, under a knob
    documented as non-increasing. The provider now answers for itself
    (`ProviderAdapter.default_reasoning_effort`) and G25 never selects above that answer.
    """

    async def test_anthropic_is_not_pushed_into_extended_thinking(
            self, make_ctx, minimal_config):
        effort = await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                              model="claude-sonnet-5",
                              adapter=_adapter(_PROVIDER_CASES[1][1]))
        assert effort == "off", (
            "G25 must not turn Anthropic extended thinking on for a caller who never "
            "asked for it — that is a cost INCREASE, not an optimisation"
        )

    async def test_the_openai_o_series_is_unchanged(self, make_ctx, minimal_config):
        """The regression check for the other direction: this fix must not quietly cut
        OpenAI reasoning, whose default genuinely is `medium`."""
        assert await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                            model="o4-mini",
                            adapter=_adapter(_PROVIDER_CASES[0][1])) == "medium"
        assert await _apply(make_ctx, minimal_config, user=_MEDIUM_USER,
                            model="o4-mini",
                            adapter=_adapter(_PROVIDER_CASES[0][1])) == "medium"

    async def test_gemini_keeps_its_default_thinking(self, make_ctx, minimal_config):
        """Gemini's thinking models think by DEFAULT and `off` here is an explicit
        `thinking_budget: 0`, so reporting `off` as this provider's default would have
        G25 hard-disable thinking the provider would have done — a quality loss dressed
        up as non-increasing. Its adapter answers `medium`."""
        assert await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                            model="gemini-2.5-flash",
                            adapter=_adapter(_PROVIDER_CASES[2][1])) == "medium"

    async def test_lowering_still_works_on_every_provider(self, make_ctx, minimal_config):
        """The clamp is a ceiling, not a floor: the G06 bridge and the classifier can
        still take effort DOWN, which is where the group's savings come from."""
        for _, path, model in _PROVIDER_CASES:
            effort = await _apply(make_ctx, minimal_config, user=_TRIVIAL_USER,
                                  model=model, adapter=_adapter(path),
                                  default_effort="low")
            assert effort in ("off", "low"), f"{model}: {effort!r}"

    async def test_the_operator_can_opt_into_escalation(self, make_ctx, minimal_config):
        effort = await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                              model="claude-sonnet-5",
                              adapter=_adapter(_PROVIDER_CASES[1][1]),
                              effort_ceiling="high",
                              escalate_above_provider_default=True)
        assert effort == "high"

    async def test_the_opt_in_does_not_lift_the_config_ceiling(
            self, make_ctx, minimal_config):
        """Two independent ceilings: opting out of the provider one leaves the operator's
        own in force."""
        effort = await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                              model="claude-sonnet-5",
                              adapter=_adapter(_PROVIDER_CASES[1][1]),
                              escalate_above_provider_default=True)
        assert effort == "medium"

    async def test_an_explicit_floor_still_wins(self, make_ctx, minimal_config):
        """Documented and deliberate: `effort_floor` is an operator saying "every request
        gets at least this much", which is an instruction, not the proxy deciding."""
        effort = await _apply(make_ctx, minimal_config, user=_TRIVIAL_USER,
                              model="claude-sonnet-5",
                              adapter=_adapter(_PROVIDER_CASES[1][1]),
                              effort_floor="low")
        assert effort == "low"

    async def test_a_provider_entry_can_state_its_own_default(
            self, make_ctx, minimal_config):
        """A provider that changes its default must be expressible in config, not only in
        a code release."""
        minimal_config["providers"] = [
            {"name": "anthropic", "default_reasoning_effort": "medium"}]
        effort = await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                              model="claude-sonnet-5",
                              adapter=_adapter(_PROVIDER_CASES[1][1]))
        assert effort == "medium"

    async def test_no_adapter_means_no_provider_clamp(self, make_ctx, minimal_config):
        """Nothing is known about the provider, so nothing is claimed about it: the
        config ceiling applies alone rather than a guessed provider default."""
        cfg = {"enabled": True, "extra_reasoning_prefixes": ["o4"],
               "effort_ceiling": "high"}
        minimal_config["groups"]["G25_adaptive_reasoning"] = cfg
        ctx = make_ctx([{"role": "user", "content": _COMPLEX_USER}], model="o4-mini",
                       config=minimal_config)
        ctx.routed_model = "o4-mini"
        ctx.provider_adapter = None
        out = await G25AdaptiveReasoning().process_request(ctx)
        assert out.params.get("reasoning_effort") == "high"

    async def test_an_adapter_that_raises_does_not_break_the_request(
            self, make_ctx, minimal_config):
        class _Broken:
            def supports_reasoning(self, model, config=None):
                return True

            def default_reasoning_effort(self, model, config=None):
                raise RuntimeError("provider entry malformed")

        effort = await _apply(make_ctx, minimal_config, user=_COMPLEX_USER,
                              model="o4-mini", adapter=_Broken(), effort_ceiling="high")
        assert effort == "high", "a broken adapter must not take the request down"


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [_TRIVIAL_USER, _MEDIUM_USER, _COMPLEX_USER,
                                  "prove the time complexity", "summarise this thread"])
@pytest.mark.parametrize("ceiling", list(__import__("providers").REASONING_TIERS))
@pytest.mark.parametrize("case", _PROVIDER_CASES, ids=[c[0] for c in _PROVIDER_CASES])
async def test_g25_never_selects_above_either_ceiling(
        make_ctx, minimal_config, user, ceiling, case):
    """THE property (F6), over classifier result x configured ceiling x provider default.

    Supersedes an earlier test of the same name that parametrised only the user text and
    asserted a literal `in ("off", "low", "medium")` tuple — it could not fail if the
    ceiling logic changed, because it never varied the ceiling and never consulted the
    provider. This one derives the expected bound from the same two sources the code
    reads and asserts the inequality that IS the contract:

        order[applied] <= min(order[configured ceiling], order[provider default])

    with the floor left at its default (`off`) so the ceiling is the only bound in play.
    """
    from providers import REASONING_TIERS

    _, path, model = case
    adapter = _adapter(path)
    order = {tier: i for i, tier in enumerate(REASONING_TIERS)}
    applied = await _apply(make_ctx, minimal_config, user=user, model=model,
                           adapter=adapter, effort_ceiling=ceiling)
    provider_default = adapter.default_reasoning_effort(model, minimal_config)
    bound = min(order[ceiling], order[provider_default])
    assert applied is not None
    assert order[applied] <= bound, (
        f"{model}: applied={applied!r} exceeds min(ceiling={ceiling!r}, "
        f"provider default={provider_default!r}) for {user!r}"
    )
