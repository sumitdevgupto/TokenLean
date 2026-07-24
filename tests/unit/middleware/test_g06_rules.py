"""Unit tests for the G06 declarative routing-rules engine (middleware/g06_rules.py)
and its integration into G06Routing.process_request.

Two layers:
  * TestRoutingRulesEngine — the pure engine (matchers, normalize, evaluate,
    effective_cfg) with no RequestContext.
  * TestRoutingRulesIntegration — G06Routing.process_request with rules configured,
    proving pin-model / pin-tier / precedence / cost-floor / reachability / overrides.
  * TestRoutingRulesRegression — the byte-identical guarantee: no `rules` key and
    `rules: []` route identically (protects the published savings baseline).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import patch

from middleware.g06_rules import (
    MAX_PATTERN_LEN,
    MAX_RULES,
    RULE_OVERRIDE_KEYS,
    effective_cfg,
    evaluate_rules,
    normalize_rules,
    rule_matches,
)

_ABSENT = object()

# Distinct pricing so the cost-floor guard can tell gpt-4-5 (pricey) from gpt-4o-mini
# (cheap) — mirrors test_g06_routing._PRICING (unit tests have no pricing table by default).
_PRICING = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4-5": {"input": 0.075, "output": 0.15},
    "default": {"input": 0.005, "output": 0.015},
}

# ~100 words, no complex/simple keywords → _classify_heuristic == "medium".
_MEDIUM_PROMPT = (
    "Our nightly export job moved about forty thousand customer records into the "
    "reporting warehouse and then paused for roughly nine minutes before finishing "
    "the remaining batches without any operator action taken at all. The dashboard "
    "showed the queue depth climbing steadily and then draining back down to zero on "
    "its own while the on call engineer was still reading the very first alert page. "
    "Walk me through what the most likely sequence of events was here and whether the "
    "pause should be treated as expected backpressure behaviour or a genuine incident "
    "worth escalating to the wider platform team for a deeper follow up review tomorrow."
)


@pytest.fixture(autouse=True)
def _g06_provider_keys():
    """Provide a providers table + keys so integration tests exercise routing, not
    key-gating (mirrors the autouse fixture in test_g06_routing.py). The unreachable
    test patches _tier_reachable directly."""
    providers = [
        {"name": "openai", "model_prefixes": ["gpt", "o1", "o3", "o4", "chatgpt"]},
        {"name": "anthropic", "model_prefixes": ["claude"]},
    ]
    with patch("middleware.g06_routing.get_providers", return_value=providers), \
         patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"):
        yield


def _m(rule, *, text="hello world", prompt_tokens=100, model="gpt-4o",
       has_tools=False, params=None, user_id=""):
    return rule_matches(
        rule, text=text, prompt_tokens=prompt_tokens, model=model,
        has_tools=has_tools, params=params or {}, user_id=user_id,
    )


class TestRoutingRulesEngine:
    # ── matchers ──────────────────────────────────────────────────────────────
    def test_keywords_any_of_case_insensitive(self):
        r = {"id": "r", "match": {"keywords": ["contract", "nda"]}, "action": {"tier": "complex"}}
        assert _m(r, text="please review this NDA today")[0] is True
        assert _m(r, text="just a friendly hello")[0] is False

    def test_patterns_regex_any_of(self):
        r = {"id": "r", "match": {"patterns": [r"\bpo-\d+\b"]}, "action": {"tier": "simple"}}
        assert _m(r, text="ticket PO-4471 is open")[0] is True
        assert _m(r, text="no ticket number here")[0] is False

    def test_min_and_max_prompt_tokens(self):
        r = {"id": "r", "match": {"min_prompt_tokens": 500}, "action": {"tier": "complex"}}
        assert _m(r, prompt_tokens=600)[0] is True
        ok, failed = _m(r, prompt_tokens=100)
        assert ok is False and failed == "min_prompt_tokens"
        r2 = {"id": "r", "match": {"max_prompt_tokens": 80}, "action": {"tier": "simple"}}
        assert _m(r2, prompt_tokens=50)[0] is True
        assert _m(r2, prompt_tokens=200)[0] is False

    def test_models_exact_any_of(self):
        r = {"id": "r", "match": {"models": ["gpt-4o", "gpt-4o-mini"]}, "action": {"tier": "simple"}}
        assert _m(r, model="gpt-4o")[0] is True
        assert _m(r, model="claude-haiku-4-5")[0] is False

    def test_has_tools_both_directions(self):
        r_true = {"id": "r", "match": {"has_tools": True}, "action": {"tier": "complex"}}
        assert _m(r_true, has_tools=True)[0] is True
        assert _m(r_true, has_tools=False)[0] is False
        r_false = {"id": "r", "match": {"has_tools": False}, "action": {"tier": "simple"}}
        assert _m(r_false, has_tools=False)[0] is True
        assert _m(r_false, has_tools=True)[0] is False

    def test_params_and_across_keys_any_of_within(self):
        r = {"id": "r", "match": {"params": {"x_team": ["frontend", "mobile"]}}, "action": {"tier": "simple"}}
        assert _m(r, params={"x_team": "frontend"})[0] is True
        assert _m(r, params={"x_team": "backend"})[0] is False
        assert _m(r, params={})[0] is False
        r2 = {"id": "r", "match": {"params": {"x_team": ["frontend"], "x_env": ["prod"]}}, "action": {"tier": "simple"}}
        assert _m(r2, params={"x_team": "frontend", "x_env": "prod"})[0] is True
        assert _m(r2, params={"x_team": "frontend", "x_env": "dev"})[0] is False

    def test_user_ids_any_of(self):
        r = {"id": "r", "match": {"user_ids": ["u1", "u2"]}, "action": {"tier": "complex"}}
        assert _m(r, user_id="u1")[0] is True
        assert _m(r, user_id="u9")[0] is False

    def test_and_across_fields(self):
        r = {"id": "r", "match": {"keywords": ["deploy"], "min_prompt_tokens": 50}, "action": {"tier": "complex"}}
        assert _m(r, text="deploy now", prompt_tokens=100)[0] is True
        assert _m(r, text="deploy now", prompt_tokens=10)[0] is False   # fails size
        assert _m(r, text="hello there", prompt_tokens=100)[0] is False  # fails keyword

    def test_empty_match_never_matches(self):
        ok, failed = _m({"id": "r", "match": {}, "action": {"tier": "simple"}})
        assert ok is False and failed == "match"

    def test_invalid_regex_cannot_match_and_never_raises(self):
        r = {"id": "r", "match": {"patterns": ["(unclosed"]}, "action": {"tier": "simple"}}
        ok, failed = _m(r, text="(unclosed paren in text")
        assert ok is False and failed == "patterns"

    def test_overlong_regex_skipped(self):
        r = {"id": "r", "match": {"patterns": ["a" * (MAX_PATTERN_LEN + 1)]}, "action": {"tier": "simple"}}
        assert _m(r, text="a" * 600)[0] is False

    # ── normalize / priority ────────────────────────────────────────────────────
    def test_priority_desc_with_stable_ties(self):
        cfg = {"rules": [
            {"id": "a", "priority": 1, "match": {"keywords": ["x"]}, "action": {"tier": "simple"}},
            {"id": "b", "priority": 10, "match": {"keywords": ["x"]}, "action": {"tier": "complex"}},
            {"id": "c", "priority": 10, "match": {"keywords": ["x"]}, "action": {"tier": "medium"}},
        ]}
        assert [r["id"] for r in normalize_rules(cfg)] == ["b", "c", "a"]

    def test_disabled_rules_dropped(self):
        cfg = {"rules": [
            {"id": "a", "enabled": False, "match": {"keywords": ["x"]}, "action": {"tier": "simple"}},
            {"id": "b", "match": {"keywords": ["x"]}, "action": {"tier": "complex"}},
        ]}
        assert [r["id"] for r in normalize_rules(cfg)] == ["b"]

    def test_more_than_max_rules_capped_keeping_top_priority(self):
        cfg = {"rules": [
            {"id": f"r{i}", "priority": i, "match": {"keywords": ["x"]}, "action": {"tier": "simple"}}
            for i in range(MAX_RULES + 50)
        ]}
        out = normalize_rules(cfg)
        assert len(out) == MAX_RULES
        ids = {r["id"] for r in out}
        assert f"r{MAX_RULES + 49}" in ids and "r0" not in ids

    def test_normalize_does_not_mutate_input_list(self):
        rules = [
            {"id": "a", "priority": 1, "match": {"keywords": ["x"]}, "action": {"tier": "simple"}},
            {"id": "b", "priority": 9, "match": {"keywords": ["x"]}, "action": {"tier": "complex"}},
        ]
        cfg = {"rules": rules}
        normalize_rules(cfg)
        assert [r["id"] for r in rules] == ["a", "b"]  # original order untouched (sorted, not .sort)

    def test_no_rules_returns_empty(self):
        assert normalize_rules({}) == []
        assert normalize_rules({"rules": []}) == []
        assert normalize_rules({"rules": None}) == []

    # ── evaluate ────────────────────────────────────────────────────────────────
    def test_evaluate_first_match_wins_with_trace(self):
        rules = normalize_rules({"rules": [
            {"id": "a", "match": {"keywords": ["zzz"]}, "action": {"tier": "simple"}},
            {"id": "b", "match": {"keywords": ["deploy"]}, "action": {"tier": "complex"}},
        ]})
        rule, trace = evaluate_rules(
            rules, text="please deploy the service", prompt_tokens=10, model="gpt-4o",
            has_tools=False, params={}, user_id="", explain=True,
        )
        assert rule["id"] == "b"
        assert trace == [
            {"id": "a", "matched": False, "failed_on": "keywords"},
            {"id": "b", "matched": True, "failed_on": ""},
        ]

    # ── effective_cfg ───────────────────────────────────────────────────────────
    def test_effective_cfg_applies_whitelisted_overrides_without_mutation(self):
        cfg = {"strategy": "priority", "classifier": "cascade", "canary_pct": 0}
        rule = {"id": "r", "action": {"tier": "simple", "overrides": {"strategy": "canary", "canary_pct": 50}}}
        eff = effective_cfg(cfg, rule)
        assert eff["strategy"] == "canary" and eff["canary_pct"] == 50
        assert cfg["strategy"] == "priority" and cfg["canary_pct"] == 0  # original untouched
        assert eff is not cfg

    def test_effective_cfg_identity_fast_path_when_no_overrides(self):
        cfg = {"strategy": "priority"}
        rule = {"id": "r", "action": {"model": "gpt-4o-mini"}}
        assert effective_cfg(cfg, rule) is cfg  # zero-copy → no-op byte-identical

    def test_effective_cfg_drops_non_whitelisted_keys(self):
        cfg = {"strategy": "priority"}
        rule = {"id": "r", "action": {"overrides": {"strategy": "weighted", "enabled": False, "tiers": {}}}}
        eff = effective_cfg(cfg, rule)
        assert eff["strategy"] == "weighted"
        assert "enabled" not in eff and "tiers" not in eff

    def test_effective_cfg_allow_escalation_sets_flag(self):
        cfg = {"strategy": "priority"}
        rule = {"id": "r", "action": {"model": "gpt-4-5", "allow_escalation": True}}
        eff = effective_cfg(cfg, rule)
        assert eff.get("allow_escalation_above_requested") is True
        assert eff is not cfg and "allow_escalation_above_requested" not in cfg

    def test_override_whitelist_membership(self):
        # Guard the contract: least_latency alpha is intentionally NOT overridable.
        assert "least_latency_alpha" not in RULE_OVERRIDE_KEYS
        assert {"strategy", "canary_pct", "max_escalation_cost_usd"} <= RULE_OVERRIDE_KEYS


def _rules_cfg(ctx, rules, **overrides):
    g = ctx.config["groups"]["G6_routing"]
    g["enabled"] = True
    g["classifier"] = "heuristic"
    g["cascade_execution"] = False
    g["tiers"] = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4-5"]}
    g["rules"] = rules
    g.update(overrides)
    return g


@pytest.mark.asyncio
class TestRoutingRulesIntegration:
    async def test_rule_pins_model(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "anything at all here please"}], model="gpt-4o")
        _rules_cfg(ctx, [{"id": "pin-mini", "match": {"keywords": ["anything"]}, "action": {"model": "gpt-4o-mini"}}])
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "rules:pin-mini"

    async def test_rule_pins_tier_and_applies_strategy_override(self, make_ctx):
        # tier=simple with a 2-model list + canary_pct=100 must pick models[1] (gpt-4o),
        # proving the rule's strategy override reached _select_from_tier. Caller gpt-4-5 →
        # routing down to gpt-4o is cheaper, so the cost-floor does not interfere.
        ctx = make_ctx([{"role": "user", "content": "route me to a tier now"}], model="gpt-4-5")
        g = _rules_cfg(ctx, [{"id": "canary", "match": {"keywords": ["route"]},
                              "action": {"tier": "simple", "overrides": {"strategy": "canary", "canary_pct": 100}}}])
        g["tiers"]["simple"] = ["gpt-4o-mini", "gpt-4o"]
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o"
        assert ctx.savings.routing_mode == "rules:canary"

    async def test_caller_x_complexity_beats_rule(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "deploy this service"}], model="gpt-4o",
                       params={"x_complexity": "simple"})
        _rules_cfg(ctx, [{"id": "pin-complex", "match": {"keywords": ["deploy"]}, "action": {"tier": "complex"}}])
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.savings.routing_mode == "user_override"
        assert ctx.routed_model == "gpt-4o-mini"  # simple tier, NOT the rule's complex tier

    async def test_cost_floor_reverts_pinned_pricier_model(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "a cheap little request"}], model="gpt-4o-mini")
        _rules_cfg(ctx, [{"id": "pin-45", "match": {"keywords": ["cheap"]}, "action": {"model": "gpt-4-5"}}])
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"  # reverted by cost-floor
        assert ctx.savings.routing_mode == "rules:pin-45+cost_floor"

    async def test_allow_escalation_keeps_pinned_pricier_model(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "a cheap little request"}], model="gpt-4o-mini")
        _rules_cfg(ctx, [{"id": "esc", "match": {"keywords": ["cheap"]},
                          "action": {"model": "gpt-4-5", "allow_escalation": True}}])
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4-5"  # kept — opt-out of the cost-floor
        assert ctx.savings.routing_mode == "rules:esc"

    async def test_unreachable_pinned_model_falls_back_to_caller(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "pin me somewhere"}], model="gpt-4o")
        _rules_cfg(ctx, [{"id": "pin-claude", "match": {"keywords": ["pin"]}, "action": {"model": "claude-opus-4"}}])
        from middleware.g06_routing import G06Routing
        with patch("middleware.g06_routing._tier_reachable", side_effect=lambda m: m == "gpt-4o"), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o"  # fell back to the caller's reachable model
        assert ctx.savings.routing_mode == "rules:pin-claude+tier_unreachable_fallback"

    async def test_overrides_only_rule_runs_classifier_under_override(self, make_ctx):
        # No tier/model → does NOT pin. The classifier still runs, but the rule's strategy
        # override reaches its _select_from_tier: "what is python" → simple tier; canary_pct=100
        # over a 2-model simple tier picks models[1] (gpt-4o), which the default priority would not.
        ctx = make_ctx([{"role": "user", "content": "what is python"}], model="gpt-4-5")
        g = _rules_cfg(ctx, [{"id": "ov", "match": {"keywords": ["python"]},
                              "action": {"overrides": {"strategy": "canary", "canary_pct": 100}}}])
        g["tiers"]["simple"] = ["gpt-4o-mini", "gpt-4o"]
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o"
        assert ctx.savings.routing_mode == "heuristic"  # classifier ran; overrides-only never pins

    async def test_pinning_rule_skips_cascade_execution(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "pin me now please"}], model="gpt-4o")
        _rules_cfg(ctx, [{"id": "pin", "match": {"keywords": ["pin"]}, "action": {"model": "gpt-4o-mini"}}],
                   classifier="cascade", cascade_execution=True)
        called = {"v": False}

        async def _spy(*a, **k):
            called["v"] = True
            return ("gpt-4o", {})

        from middleware.g06_routing import G06Routing
        with patch("middleware.g06_routing._execute_three_tier_cascade", side_effect=_spy), \
             patch("config_loader.get_pricing_table", return_value=_PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert called["v"] is False  # cascade probe LLM calls never happened
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "rules:pin"


@pytest.mark.asyncio
class TestRoutingRulesRegression:
    async def test_no_rules_key_and_empty_rules_route_identically(self, make_ctx):
        """The savings-baseline guard: absent `rules` and `rules: []` must produce an
        identical routing decision (routed_model, routing_mode, and G06 savings steps)."""
        from middleware.g06_routing import G06Routing

        async def _run(rules_val):
            ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o-mini")
            g = ctx.config["groups"]["G6_routing"]
            g["enabled"] = True
            g["classifier"] = "heuristic"
            g["tiers"] = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4-5"]}
            if rules_val is not _ABSENT:
                g["rules"] = rules_val
            with patch("config_loader.get_pricing_table", return_value=_PRICING):
                ctx = await G06Routing().process_request(ctx)
            return (ctx.routed_model, ctx.savings.routing_mode,
                    tuple(s.group for s in ctx.savings.step_savings))

        absent = await _run(_ABSENT)
        empty = await _run([])
        assert absent == empty
        # And the decision is the expected classifier behaviour (medium → gpt-4o reverted by
        # the cost-floor to the cheaper caller model), i.e. rules did not perturb it.
        assert absent[0] == "gpt-4o-mini"
        assert absent[1] == "heuristic+cost_floor"
