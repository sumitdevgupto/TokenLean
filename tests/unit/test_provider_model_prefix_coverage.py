"""Guard: every model name a config mentions must be matched by some provider's
`model_prefixes`, or the proxy treats it as UNKNOWN and silently downgrades it.

`config_loader.get_known_models()` reads `providers[].models`, and provider detection
reads `providers[].model_prefixes`. `g06_routing.G06Routing.process_request` uses that
to decide whether a caller's model is "configured": when G06 is disabled or has no tier
ladder, a model it does not recognise is replaced with `proxy.default_model` — the code
comment there even says the guard exists so "the developer's model, e.g. o4-mini, [is
preserved] so reasoning stays measurable".

On 2026-09-08 that guard was defeated by a missing three-character prefix. The ablation
config listed `o4-mini` in `G6_routing.tiers.complex` but its openai `model_prefixes`
were `[gpt, o1, o3, text-, ...]` — no `o4`. Every DS11 arm with G06 off therefore ran on
gpt-4o-mini instead of o4-mini, with the reasoning params stripped by the adapter, so
both reasoning groups (G12, G25) were inert in their own isolated arms and the dataset's
baseline was not a reasoning baseline at all. Nothing failed; the numbers were simply
about a different model than the one the requests asked for.

Two invariants, both cheap and both generic:
  1. every `providers[].models` entry is covered by that SAME provider's prefixes
     (a model advertised by a provider that its own prefix list cannot match);
  2. every model named in `G6_routing.tiers` / `tiers_by_provider` is covered by SOME
     provider's prefixes (the shape that actually bit us — routing knows the model,
     provider detection does not).

Only `config/config.yaml.template` ships; `config/config.yaml` and
`config/config.roi-openai.yaml` are gitignored, so they are checked when present and
skipped in a clean OSS checkout.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

CONFIGS = [
    ROOT / "config" / "config.yaml.template",   # tracked — always present
    ROOT / "config" / "config.yaml",            # local dev — gitignored
    ROOT / "config" / "config.roi-openai.yaml",  # ablation harness — gitignored
]


def _load(path):
    if not path.exists():
        pytest.skip(f"{path.name} not present (gitignored / clean checkout)")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _matches(model, prefixes):
    m = str(model).lower()
    return any(m.startswith(str(p).lower()) for p in prefixes if p)


def _all_prefixes(cfg):
    out = []
    for prov in cfg.get("providers") or []:
        out += list(prov.get("model_prefixes") or [])
    return out


def _tier_models(cfg):
    """Every model named by G06's routing ladders, in either supported shape."""
    routing = (cfg.get("groups") or {}).get("G6_routing") or {}
    models = []
    for tier_models in (routing.get("tiers") or {}).values():
        models += list(tier_models or [])
    for ladder in (routing.get("tiers_by_provider") or {}).values():
        for tier_models in (ladder or {}).values():
            models += list(tier_models or [])
    return models


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
class TestModelPrefixCoverage:
    def test_every_advertised_model_matches_its_own_provider_prefixes(self, path):
        cfg = _load(path)
        uncovered = [
            (prov.get("name"), model)
            for prov in (cfg.get("providers") or [])
            for model in (prov.get("models") or [])
            if not _matches(model, prov.get("model_prefixes") or [])
        ]
        assert not uncovered, (
            f"{path.name}: provider(s) advertise models their own model_prefixes cannot "
            f"match {uncovered}. Such a model is UNKNOWN to provider detection and G06's "
            f"disabled path will downgrade it to proxy.default_model. Add the prefix."
        )

    def test_every_routed_model_matches_some_provider_prefix(self, path):
        cfg = _load(path)
        prefixes = _all_prefixes(cfg)
        uncovered = [m for m in _tier_models(cfg) if not _matches(m, prefixes)]
        assert not uncovered, (
            f"{path.name}: G06 routes to model(s) {uncovered} that match no provider's "
            f"model_prefixes. G06's disabled/no-ladder path treats them as unknown and "
            f"serves proxy.default_model instead — silently swapping the model the caller "
            f"asked for. This is exactly how o4-mini became gpt-4o-mini (2026-09-08)."
        )


class TestTheCodeAndTheTemplateAgreeOnPrefixes:
    """Config-INDEPENDENT invariants (review W20-R-G25 finding F3, decision D-076).

    The two tests above have almost no regression power over what actually ships: the
    template routes `complex` to `gpt-4-5` and names no o-series model at all, so the
    `o4` prefix could be deleted from it tomorrow and both would still pass. The configs
    that WOULD have caught the 2026-09-08 defect (`config.yaml`, `config.roi-openai.yaml`)
    are gitignored, so they skip in a clean checkout — i.e. in `verify-oss-gates.sh` and
    in public CI, the only places this guard has to hold.

    These two compare the shipped template against the CODE, which is always present:

      1. every prefix in `config_loader._DEFAULT_PROVIDER_PREFIXES` — the fallback
         provider detection uses before config loads — must also be in the template, or
         the same model resolves to a different provider depending on whether config has
         loaded yet;
      2. every model family `OpenAIAdapter` treats as reasoning-capable must be a prefix
         the template's openai entry advertises. This is the exact coupling that broke:
         the adapter knew `o4` reasons, provider detection had never heard of it, so G06's
         disabled path swapped the model out and BOTH reasoning groups went inert with
         nothing failing anywhere.
    """

    def _template(self):
        path = ROOT / "config" / "config.yaml.template"
        assert path.exists(), "the tracked template must always be present"
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def test_the_code_fallback_prefixes_are_all_in_the_template(self):
        import sys
        sys.path.insert(0, str(ROOT / "src" / "proxy"))
        from config_loader import _DEFAULT_PROVIDER_PREFIXES

        cfg = self._template()
        by_provider = {
            (p.get("name") or "").lower(): [str(x).lower()
                                            for x in (p.get("model_prefixes") or [])]
            for p in (cfg.get("providers") or [])
        }
        missing = [
            (prefix, provider)
            for prefix, provider in _DEFAULT_PROVIDER_PREFIXES.items()
            if str(prefix).lower() not in by_provider.get(str(provider).lower(), [])
        ]
        assert not missing, (
            f"config_loader._DEFAULT_PROVIDER_PREFIXES maps {missing} but the shipped "
            f"template's provider entries do not list those prefixes. Provider detection "
            f"falls back to that dict only until config loads, so a model would resolve "
            f"to a provider before load and to NOTHING after it — the state in which G06's "
            f"disabled path silently downgrades it to proxy.default_model."
        )

    def test_every_reasoning_family_the_adapter_knows_is_an_advertised_prefix(self):
        import sys
        sys.path.insert(0, str(ROOT / "src" / "proxy"))
        from providers.openai_adapter import OpenAIAdapter

        cfg = self._template()
        openai = next((p for p in (cfg.get("providers") or [])
                       if (p.get("name") or "").lower() == "openai"), {})
        prefixes = [str(x).lower() for x in (openai.get("model_prefixes") or [])]
        tiktoken = [str(x).lower() for x in (openai.get("tiktoken_prefixes") or [])]
        missing = [f for f in OpenAIAdapter.REASONING_MODEL_FAMILIES
                   if f.lower() not in prefixes]
        assert not missing, (
            f"OpenAIAdapter treats {missing} as reasoning-capable but the template's "
            f"openai model_prefixes do not advertise them. Such a model is UNKNOWN to "
            f"provider detection: G06's disabled/no-ladder path replaces it with "
            f"proxy.default_model and the adapter then strips the reasoning params, so "
            f"G12 and G25 both go inert while every test still passes. That is precisely "
            f"the o4-mini → gpt-4o-mini defect of 2026-09-08."
        )
        untokenised = [f for f in OpenAIAdapter.REASONING_MODEL_FAMILIES
                       if f.lower() not in tiktoken]
        assert not untokenised, (
            f"openai tiktoken_prefixes omit {untokenised}: those models would be counted "
            f"with the chars/4 estimate instead of tiktoken, so the reasoning workloads "
            f"whose budgets these groups manage are the ones measured least accurately."
        )
