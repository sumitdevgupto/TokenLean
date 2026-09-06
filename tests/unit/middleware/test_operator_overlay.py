"""The operator's per-tenant `tenants.<id>.groups.*` overlay must reach EVERY group.

Found by code review of 9336dfa (2026-09-06). `resolve_group_config` applied the overlay at
read time, so only a group that called it saw it — and just 9 of the 32 middleware modules
did. The other 23 read `ctx.config["groups"][key]` directly, so an operator's
`tenants.NOVA.groups.G19_headroom.enabled: false` was honoured by G29 and ignored by G19.
The mechanism the docstring calls mandatory was silently a no-op for most of the pipeline,
and `/v1/groups` — which DID apply it — reported groups as disabled that were still running,
so readiness dropped coverage of a live group.

The fix merges the overlay once at pipeline entry. These tests pin that a group which reads
the config directly now sees it, that other tenants and the process-wide config do not, and
that `resolve_group_config` still agrees (re-applying the overlay is idempotent).
"""
import copy
import sys
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

from middleware import apply_operator_overlay, operator_group_overlay, resolve_group_config  # noqa: E402


def _cfg():
    return {
        "groups": {
            "G19_headroom": {"enabled": True, "min_length_to_compress": 50},
            "G29_pii_redaction": {"enabled": True, "mode": "mask"},
        },
        "tenants": {
            "NOVA-STG-01": {"groups": {"G19_headroom": {"enabled": False}}},
        },
    }


class TestTheOverlayReachesGroupsThatReadConfigDirectly:
    def test_a_direct_reader_sees_the_operator_value(self):
        merged = apply_operator_overlay(_cfg(), "NOVA-STG-01")
        # This is exactly what g19_headroom.py does: no resolve_group_config in sight.
        assert merged["groups"]["G19_headroom"]["enabled"] is False

    def test_sibling_knobs_survive_the_merge(self):
        """A tenant overriding ONE knob must not wipe the group's other settings — the
        whole reason deep_merge exists."""
        merged = apply_operator_overlay(_cfg(), "NOVA-STG-01")
        assert merged["groups"]["G19_headroom"]["min_length_to_compress"] == 50

    def test_other_groups_are_untouched(self):
        merged = apply_operator_overlay(_cfg(), "NOVA-STG-01")
        assert merged["groups"]["G29_pii_redaction"] == {"enabled": True, "mode": "mask"}

    def test_resolve_group_config_agrees_after_the_merge(self):
        """Re-applying the same overlay at read time is idempotent, so the 9 groups that
        still call resolve_group_config get the same answer as the 23 that do not."""
        merged = apply_operator_overlay(_cfg(), "NOVA-STG-01")

        class _Ctx:
            config = merged
            tenant_id = "NOVA-STG-01"

        assert resolve_group_config(_Ctx(), "G19_headroom")["enabled"] is False


class TestTheOverlayStaysWhereItBelongs:
    def test_another_tenant_does_not_see_it(self):
        merged = apply_operator_overlay(_cfg(), "SHOP-STG-01")
        assert merged["groups"]["G19_headroom"]["enabled"] is True

    def test_the_process_wide_config_is_never_mutated(self):
        """`ctx.config` starts as the shared get_config() dict. Merging in place would
        leak one tenant's overlay to every other tenant on the instance."""
        base = _cfg()
        snapshot = copy.deepcopy(base)
        apply_operator_overlay(base, "NOVA-STG-01")
        assert base == snapshot

    def test_no_overlay_returns_the_same_object(self):
        """No copy for the common case — most tenants have no operator overlay, and the
        pipeline runs this on every request."""
        base = _cfg()
        assert apply_operator_overlay(base, "SHOP-STG-01") is base


class TestMalformedOperatorYamlDegradesInsteadOfRaising:
    @pytest.mark.parametrize("tenants", ["oops", ["a"], {"NOVA-STG-01": "oops"},
                                         {"NOVA-STG-01": {"groups": ["G19"]}}])
    def test_a_mis_indented_block_is_no_overlay(self, tenants):
        cfg = _cfg()
        cfg["tenants"] = tenants
        assert operator_group_overlay(cfg, "NOVA-STG-01") == {}
        assert apply_operator_overlay(cfg, "NOVA-STG-01") is cfg


class TestThePipelineAppliesItOnEveryRequest:
    def test_process_request_merges_the_overlay(self):
        """Source-level: the merge must sit in process_request AFTER the Postgres overlay
        loads, so the operator path keeps winning — the precedence resolve_group_config
        already gave it."""
        import inspect

        from middleware.pipeline import OptimisationPipeline

        src = inspect.getsource(OptimisationPipeline.process_request)
        assert "apply_operator_overlay(ctx.config, ctx.tenant_id)" in src
        assert src.index("_tenant_config_loader.load(ctx)") < src.index("apply_operator_overlay")
