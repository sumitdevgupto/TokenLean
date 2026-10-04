"""Unit tests for G06 per-provider routing (`tiers_by_provider`).

Covers the core resolver (`_resolve_tiers`), the never-cross-provider-misroute
pass-through, the byte-identical legacy flat-`tiers` fallback (so the calibrated
savings baseline is untouched), and the shipped template's ladders (openai ladder
== flat tiers for calibration parity; every ladder model resolves to its provider).
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from middleware import g06_routing  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "config" / "config.yaml.template"

_ANTHRO_LADDER = {"simple": ["claude-haiku-4-5"], "medium": ["claude-sonnet-4-5"], "complex": ["claude-opus-4"]}
_OPENAI_LADDER = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4-5"]}


# --------------------------------------------------------------------------- #
# _resolve_tiers — core routing selection
# --------------------------------------------------------------------------- #
def test_resolve_tiers_legacy_flat_is_byte_identical(monkeypatch):
    # No tiers_by_provider → the flat `tiers` map is returned verbatim (the SAME object),
    # so every existing calibration/benchmark config hits the identical pre-existing path.
    flat = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4-5"]}
    cfg = {"tiers": flat}
    got = g06_routing._resolve_tiers(cfg, "gpt-4o-mini")
    assert got is flat  # identity, not just equality → provably unchanged behaviour


def test_resolve_tiers_empty_when_no_config():
    # No tiers and no tiers_by_provider → {} (caller's existing no-op rule fires).
    assert g06_routing._resolve_tiers({}, "gpt-4o-mini") == {}


def test_resolve_tiers_routes_within_requested_provider(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", lambda m: "anthropic")
    cfg = {"tiers": _OPENAI_LADDER, "tiers_by_provider": {"openai": _OPENAI_LADDER, "anthropic": _ANTHRO_LADDER}}
    # A Claude request resolves to the ANTHROPIC ladder, never the OpenAI flat tiers.
    assert g06_routing._resolve_tiers(cfg, "claude-haiku-4-5") == _ANTHRO_LADDER


def test_resolve_tiers_openai_matches_flat_when_by_provider_present(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", lambda m: "openai")
    cfg = {"tiers": _OPENAI_LADDER, "tiers_by_provider": {"openai": _OPENAI_LADDER, "anthropic": _ANTHRO_LADDER}}
    # OpenAI request under tiers_by_provider resolves to the same models as the flat tiers.
    assert g06_routing._resolve_tiers(cfg, "gpt-4o-mini") == _OPENAI_LADDER


def test_resolve_tiers_pass_through_when_family_has_no_ladder(monkeypatch):
    # tiers_by_provider present but the requested model's provider has NO ladder → None,
    # which the caller treats as 'keep the requested model' (never cross-provider misroute).
    monkeypatch.setattr(g06_routing, "_tier_provider", lambda m: "mistral")
    cfg = {"tiers_by_provider": {"openai": _OPENAI_LADDER, "anthropic": _ANTHRO_LADDER}}
    assert g06_routing._resolve_tiers(cfg, "mistral-small-latest") is None


def test_resolve_tiers_pass_through_when_family_ladder_is_empty(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", lambda m: "anthropic")
    cfg = {"tiers_by_provider": {"anthropic": {"simple": [], "medium": [], "complex": []}}}
    assert g06_routing._resolve_tiers(cfg, "claude-haiku-4-5") is None


def test_resolve_tiers_pass_through_when_provider_unresolved(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", lambda m: None)
    cfg = {"tiers_by_provider": {"openai": _OPENAI_LADDER}}
    assert g06_routing._resolve_tiers(cfg, "some-unknown-model") is None


# --------------------------------------------------------------------------- #
# A tenant's tier picks (portal model preferences, or the operator's tenants.<id> block)
# win over the provider ladder. They were stored and shown as in effect, and ignored.
# --------------------------------------------------------------------------- #
_BY_PROVIDER = {"tiers": _OPENAI_LADDER,
                "tiers_by_provider": {"openai": _OPENAI_LADDER, "anthropic": _ANTHRO_LADDER}}


def _family(model):
    return ("anthropic" if model.startswith("claude")
            else "openai" if model.startswith("gpt") else "mistral")


def _g6_tiers(tiers):
    return {"groups": {"G6_routing": {"tiers": tiers}}}


class _Ctx:
    def __init__(self, config=None, overrides=None, tenant_id="acme"):
        self.config = config or {}
        self.tenant_config_overrides = overrides
        self.tenant_id = tenant_id


def test_a_tenant_pick_wins_within_the_requested_family(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", _family)
    got = g06_routing._resolve_tiers(_BY_PROVIDER, "gpt-4o", {"simple": ["gpt-4.1-nano"]})
    assert got == {**_OPENAI_LADDER, "simple": ["gpt-4.1-nano"]}


def test_a_tenant_pick_wins_for_another_providers_request(monkeypatch):
    """The tenant chose that model; untouched tiers keep the request's own ladder."""
    monkeypatch.setattr(g06_routing, "_tier_provider", _family)
    got = g06_routing._resolve_tiers(_BY_PROVIDER, "claude-sonnet-4-5", {"simple": ["gpt-4.1-nano"]})
    assert got == {**_ANTHRO_LADDER, "simple": ["gpt-4.1-nano"]}


def test_picks_route_a_family_that_has_no_ladder(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", _family)
    got = g06_routing._resolve_tiers(_BY_PROVIDER, "mistral-small-latest", {"medium": ["gpt-4o"]})
    assert got == {"medium": ["gpt-4o"]}


def test_without_picks_nothing_changes(monkeypatch):
    monkeypatch.setattr(g06_routing, "_tier_provider", _family)
    assert g06_routing._resolve_tiers(_BY_PROVIDER, "claude-haiku-4-5", {}) is _ANTHRO_LADDER
    assert g06_routing._resolve_tiers(_BY_PROVIDER, "mistral-small-latest", {}) is None
    # The flat path already carries the picks, merged into `tiers` by the loader.
    flat = {"tiers": _OPENAI_LADDER}
    assert g06_routing._resolve_tiers(flat, "gpt-4o", {"simple": ["gpt-4.1-nano"]}) is _OPENAI_LADDER


def test_tenant_picks_come_from_the_portal_settings():
    ctx = _Ctx(config=_g6_tiers(_OPENAI_LADDER), overrides=_g6_tiers({"simple": ["gpt-4.1-nano"]}))
    assert g06_routing._tenant_tier_picks(ctx) == {"simple": ["gpt-4.1-nano"]}


def test_the_platform_ladder_is_not_a_pick():
    assert g06_routing._tenant_tier_picks(_Ctx(config=_g6_tiers(_OPENAI_LADDER))) == {}


def test_the_operator_block_is_applied_last_and_wins():
    config = {"tenants": {"acme": _g6_tiers({"simple": ["gpt-4o"], "complex": " claude-opus-4 "})}}
    ctx = _Ctx(config=config,
               overrides=_g6_tiers({"simple": ["gpt-4.1-nano"], "medium": ["gpt-4o"]}))
    assert g06_routing._tenant_tier_picks(ctx) == {
        "simple": ["gpt-4o"], "medium": ["gpt-4o"], "complex": ["claude-opus-4"]}


def test_another_tenants_block_does_not_count():
    config = {"tenants": {"other": _g6_tiers({"simple": ["gpt-4o"]})}}
    assert g06_routing._tenant_tier_picks(_Ctx(config=config)) == {}


@pytest.mark.parametrize("overrides", [
    {"groups": "oops"}, {"groups": {"G6_routing": ["x"]}}, _g6_tiers("simple"),
    _g6_tiers({"simple": 5, "medium": [1, " ", None], "fast": ["gpt-4o"]}),
], ids=["groups-not-a-dict", "g6-not-a-dict", "tiers-not-a-dict", "bad-values"])
def test_malformed_settings_are_ignored(overrides):
    assert g06_routing._tenant_tier_picks(_Ctx(config={"tenants": ["x"]}, overrides=overrides)) == {}


@pytest.mark.parametrize("pick, routed", [
    ({"simple": ["gpt-4.1-nano"]}, "gpt-4.1-nano"),
    (None, "claude-haiku-4-5"),
], ids=["tenant-pick", "platform-ladder"])
async def test_a_request_is_routed_to_the_tenants_pick(make_ctx, minimal_config, monkeypatch,
                                                      pick, routed):
    monkeypatch.setattr(g06_routing, "_tier_provider", _family)
    monkeypatch.setattr(g06_routing, "_tier_reachable", lambda m: True)
    minimal_config["groups"]["G6_routing"] = {"enabled": True, "classifier": "heuristic",
                                              **_BY_PROVIDER}
    ctx = make_ctx(model="claude-sonnet-4-5", params={"x_complexity": "simple"},
                   config=minimal_config)
    ctx.tenant_config_overrides = _g6_tiers(pick) if pick else None
    out = await g06_routing.G06Routing().process_request(ctx)
    assert out.routed_model == routed


# --------------------------------------------------------------------------- #
# Shipped template — ladders are well-formed + calibration-safe
# --------------------------------------------------------------------------- #
def _template_g06():
    c = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return c["groups"]["G6_routing"], c["providers"]


def test_template_openai_ladder_equals_flat_tiers():
    # The OpenAI ladder MUST mirror the flat tiers so OpenAI routing (and the published
    # savings baseline) is byte-identical whether or not tiers_by_provider is consulted.
    g6, _ = _template_g06()
    tbp = g6["tiers_by_provider"]
    for tier in ("simple", "medium", "complex"):
        assert tbp["openai"][tier] == g6["tiers"][tier]


def test_template_ladders_cover_ten_native_providers():
    g6, _ = _template_g06()
    tbp = g6["tiers_by_provider"]
    assert set(tbp) == {
        "openai", "anthropic", "gemini", "azure", "bedrock",
        "mistral", "groq", "cohere", "deepseek", "xai",
    }


def test_template_every_ladder_model_resolves_to_its_provider():
    # A ladder model must be owned by the provider it's filed under (matched by that
    # provider's model_prefixes) — else G06 would route it to the WRONG provider.
    g6, providers = _template_g06()
    prefixes = {p["name"]: p.get("model_prefixes", []) for p in providers}
    for provider, ladder in g6["tiers_by_provider"].items():
        for tier in ("simple", "medium", "complex"):
            for model in ladder.get(tier, []):
                assert any(model.startswith(px) for px in prefixes[provider]), (
                    f"{model!r} in tiers_by_provider[{provider!r}][{tier!r}] does not match "
                    f"any of {provider!r}'s model_prefixes {prefixes[provider]}"
                )
