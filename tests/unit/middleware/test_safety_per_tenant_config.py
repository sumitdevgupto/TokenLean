"""Trust & safety groups must honour the per-tenant YAML overlay.

Two per-tenant mechanisms exist. The DB path (`tenant_configs.config_overrides`, what
the portal writes) is deep-merged into `ctx.config["groups"]` before middleware runs, so
reading `groups.<key>` picks it up for free. The **operator** path,
`tenants.<id>.groups.<key>` in config.yaml, is resolved at read time — and G29/G30/G31
never did, despite `docs/config-reference.md` documenting it for all of them.

That mattered most here: a tenant is deliberately refused permission to disable a safety
group, so without this overlay an operator had nowhere to configure one tenant
differently short of a direct database write.

Absent a `tenants:` block these resolve exactly as before, so the fix is additive.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timezone

import pytest

from middleware import RequestContext, resolve_group_config
from middleware.g29_pii_redaction import G29PiiRedaction
from middleware.g30_guardrails import G30Guardrails
from middleware.g31_context_trust import G31ContextTrust
from middleware.g32_tool_eligibility import G32ToolEligibility
from savings.models import SavingsRecord

CASES = [
    ("G29_pii_redaction", G29PiiRedaction, "off"),
    ("G30_guardrails", G30Guardrails, "allow"),
    ("G31_context_trust", G31ContextTrust, "allow"),
    ("G32_tool_eligibility", G32ToolEligibility, "off"),
]


def _ctx(config, tenant_id="acme"):
    return RequestContext(
        request_id="req-pt", user_id="u", original_messages=[], messages=[],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params={}, config=config,
        tenant_id=tenant_id,
        savings=SavingsRecord(request_id="req-pt", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


@pytest.mark.parametrize("key,cls,disable_value", CASES)
def test_operator_can_configure_one_tenant_differently(key, cls, disable_value):
    cfg = {
        "groups": {key: {"enabled": True, "mode": "block"}},
        "tenants": {"acme": {"groups": {key: {"mode": disable_value}}}},
    }
    assert cls()._config(_ctx(cfg, "acme"))["mode"] == disable_value
    # A different tenant is untouched by acme's overlay.
    assert cls()._config(_ctx(cfg, "other"))["mode"] == "block"


@pytest.mark.parametrize("key,cls,_dv", CASES)
def test_no_tenants_block_is_unchanged(key, cls, _dv):
    cfg = {"groups": {key: {"enabled": True, "mode": "flag", "threshold": 0.7}}}
    assert cls()._config(_ctx(cfg)) == {"enabled": True, "mode": "flag", "threshold": 0.7}


@pytest.mark.parametrize("key,cls,_dv", CASES)
def test_overlay_merges_rather_than_replaces(key, cls, _dv):
    # Overriding one knob must not wipe its siblings — the whole reason deep_merge is used.
    cfg = {
        "groups": {key: {"enabled": True, "mode": "flag", "threshold": 0.7}},
        "tenants": {"acme": {"groups": {key: {"mode": "block"}}}},
    }
    out = cls()._config(_ctx(cfg))
    assert out["mode"] == "block" and out["threshold"] == 0.7 and out["enabled"] is True


def test_overlay_never_mutates_the_shared_base_config():
    # ctx.config is shared; a per-request overlay leaking into it would corrupt the
    # NEXT tenant's view of the same config object.
    cfg = {
        "groups": {"G32_tool_eligibility": {"mode": "flag"}},
        "tenants": {"acme": {"groups": {"G32_tool_eligibility": {"mode": "off"}}}},
    }
    resolve_group_config(_ctx(cfg), "G32_tool_eligibility")
    assert cfg["groups"]["G32_tool_eligibility"]["mode"] == "flag"


@pytest.mark.parametrize("bad", [None, "not-a-dict", 42, []])
def test_malformed_tenants_block_degrades_to_base(bad):
    cfg = {"groups": {"G32_tool_eligibility": {"mode": "flag"}}, "tenants": bad}
    assert resolve_group_config(_ctx(cfg), "G32_tool_eligibility") == {"mode": "flag"}
