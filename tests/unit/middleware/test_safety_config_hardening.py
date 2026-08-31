"""Regressions for the config-resolution and mode-coercion defects found reviewing G32.

Five distinct bugs, all in the seam between operator-edited YAML and the trust & safety
groups that read it:

1. **Cross-tenant policy fallback.** G32's last-good policy cache was keyed by config
   signature alone, in a single slot shared by every tenant. On a malformed policy the
   fallback handed back whatever policy *another* tenant had last compiled.
2. **`mode: off` did nothing.** YAML 1.1 resolves an unquoted `off` to the boolean
   `False`, and `str(False).lower()` -> `"false"` is in no group's valid set, so a
   documented way to switch G29/G32 off silently fell back to `flag`.
3. **A mis-indented `tenants:` block 500'd the tenant.** The unguarded `.get()` chain
   raised `AttributeError` inside middleware rather than degrading to the base config.
4. **Batching bypassed G32.** A batched request is answered out-of-band and its result
   never runs the response pipeline, so the eligibility gate never saw its tool calls.
5. **G31's injection verdict left no audit row.** It was missing from both the audit
   branch and main.py's scheduling guard, so a context-trust block wrote nothing at all.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timezone

import pytest
import yaml

from middleware import RequestContext, coerce_mode, resolve_group_config
from middleware.g29_pii_redaction import G29PiiRedaction
from middleware.g32_tool_eligibility import G32ToolEligibility
from savings.models import SavingsRecord


def _ctx(config, tenant_id="acme"):
    return RequestContext(
        request_id="req-h", user_id="u", original_messages=[], messages=[],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params={}, config=config,
        tenant_id=tenant_id,
        savings=SavingsRecord(request_id="req-h", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


def _resp(tool_name="delete_everything"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"}}]}}]}


# ── 1. Cross-tenant policy isolation ─────────────────────────────────────────
class TestPolicyCacheIsolation:
    """One G32 instance serves every tenant, so its cache must be tenant-scoped."""

    def _cfg(self, policy_by_tenant):
        return {
            "groups": {"G32_tool_eligibility": {"enabled": True, "mode": "block"}},
            "tenants": {
                t: {"groups": {"G32_tool_eligibility": {"policy": p}}}
                for t, p in policy_by_tenant.items()
            },
        }

    async def test_malformed_policy_does_not_fall_back_to_another_tenants(self):
        # `acme` compiles a valid deny-all policy; `globex` ships a malformed glob.
        cfg = self._cfg({
            "acme": {"deny": ["*"], "default": "allow"},
            "globex": {"deny": ["[unclosed"], "default": "allow"},
        })
        g32 = G32ToolEligibility()

        acme = await g32.process_response(_ctx(cfg, "acme"), _resp())
        assert "tool_calls" not in acme["choices"][0]["message"], "acme's deny-all must strip"

        # globex has no last-good policy of its own. It must fall back to the documented
        # no-op — NOT to acme's deny-all, which would apply another workspace's rules.
        globex_ctx = _ctx(cfg, "globex")
        globex = await g32.process_response(globex_ctx, _resp())
        assert globex["choices"][0]["message"]["tool_calls"], (
            "globex inherited acme's compiled policy — cross-tenant isolation break"
        )
        assert globex_ctx.tool_eligibility_action is None

    async def test_each_tenant_keeps_its_own_last_good_policy(self):
        """The tenant's OWN last-good is still retained — the safeguard is not lost."""
        good = self._cfg({"acme": {"deny": ["*"], "default": "allow"}})
        g32 = G32ToolEligibility()
        await g32.process_response(_ctx(good, "acme"), _resp())

        broken = self._cfg({"acme": {"deny": ["[unclosed"], "default": "allow"}})
        out = await g32.process_response(_ctx(broken, "acme"), _resp())
        assert "tool_calls" not in out["choices"][0]["message"], (
            "a bad pattern arriving by hot-reload must not widen the gate"
        )

    async def test_two_tenants_do_not_evict_each_other(self):
        """Interleaved traffic must not recompile on every request (the old single slot
        thrashed) — and, more importantly, must stay correct while doing so."""
        cfg = self._cfg({
            "acme": {"deny": ["*"], "default": "allow"},
            "globex": {"allow": ["*"], "default": "allow"},
        })
        g32 = G32ToolEligibility()
        for _ in range(3):
            a = await g32.process_response(_ctx(cfg, "acme"), _resp())
            g = await g32.process_response(_ctx(cfg, "globex"), _resp())
            assert "tool_calls" not in a["choices"][0]["message"]
            assert g["choices"][0]["message"]["tool_calls"]
        assert set(g32._policy_cache) == {"acme", "globex"}


# ── 2. YAML boolean modes ────────────────────────────────────────────────────
class TestModeCoercion:
    """`off` is a YAML boolean, not the string an operator thinks they wrote."""

    def test_yaml_actually_parses_off_as_false(self):
        """Pin the premise — if PyYAML ever stops doing this the fix can be dropped."""
        assert yaml.safe_load("mode: off")["mode"] is False
        assert yaml.safe_load("mode: no")["mode"] is False
        assert yaml.safe_load('mode: "off"')["mode"] == "off"

    @pytest.mark.parametrize("raw,valid,default,expected", [
        (False, ("off", "flag", "block"), "flag", "off"),      # `mode: off`
        (False, ("off", "flag", "mask", "block"), "flag", "off"),
        (True, ("off", "flag", "block"), "flag", "flag"),      # `mode: on` -> default
        # G30/G31 spell passthrough `allow`, so `off` is not theirs to honour.
        (False, ("allow", "flag", "block"), "flag", "flag"),
        ("BLOCK", ("off", "flag", "block"), "flag", "block"),  # case/whitespace
        ("  flag ", ("off", "flag", "block"), "off", "flag"),
        (None, ("off", "flag", "block"), "flag", "flag"),
        ("nonsense", ("off", "flag", "block"), "flag", "flag"),
    ])
    def test_coerce_mode(self, raw, valid, default, expected):
        assert coerce_mode(raw, valid, default) == expected

    async def test_g32_mode_off_from_yaml_is_honoured(self):
        cfg = yaml.safe_load(
            "groups:\n"
            "  G32_tool_eligibility:\n"
            "    enabled: true\n"
            "    mode: off\n"
            "    policy:\n"
            "      deny: ['*']\n"
        )
        ctx = _ctx(cfg)
        out = await G32ToolEligibility().process_response(ctx, _resp())
        assert out["choices"][0]["message"]["tool_calls"], "deny-all ran despite mode: off"
        assert ctx.tool_eligibility_action is None, (
            "`mode: off` still recorded an event — the operator asked for silence"
        )

    def test_g29_mode_off_from_yaml_is_honoured(self):
        cfg = yaml.safe_load("groups:\n  G29_pii_redaction:\n    mode: off\n")
        g29 = G29PiiRedaction()
        assert g29._mode(g29._config(_ctx(cfg))) == "off"


# ── 3. Malformed tenants: block must not 500 the tenant ──────────────────────
class TestResolverTypeGuards:
    @pytest.mark.parametrize("config", [
        {"groups": {"G32_tool_eligibility": {"mode": "block"}}, "tenants": "oops"},
        {"groups": {"G32_tool_eligibility": {"mode": "block"}}, "tenants": ["a", "b"]},
        {"groups": {"G32_tool_eligibility": {"mode": "block"}},
         "tenants": {"acme": "not-a-mapping"}},
        {"groups": {"G32_tool_eligibility": {"mode": "block"}},
         "tenants": {"acme": {"groups": "mis-indented"}}},
        {"groups": {"G32_tool_eligibility": {"mode": "block"}},
         "tenants": {"acme": {"groups": {"G32_tool_eligibility": "scalar"}}}},
    ])
    def test_malformed_tenants_block_degrades_to_base(self, config):
        """A YAML typo must cost that tenant its overlay, not all of its traffic."""
        assert resolve_group_config(_ctx(config), "G32_tool_eligibility")["mode"] == "block"

    def test_malformed_groups_block_yields_empty(self):
        assert resolve_group_config(_ctx({"groups": "oops"}), "G32_tool_eligibility") == {}
        assert resolve_group_config(_ctx({}), "G32_tool_eligibility") == {}

    @pytest.mark.parametrize("key", [
        "G13_batch", "G21_cache_alignment", "G26_context_budget", "G28_ccr",
    ])
    def test_consolidated_groups_share_the_guards(self, key):
        """G13/G21/G26/G28 carried the same unguarded chain; consolidating fixed them
        all at once. Imported here so a future private resolver re-opens the hole
        loudly."""
        cfg = {"groups": {key: {"enabled": True}}, "tenants": {"acme": {"groups": "bad"}}}
        assert resolve_group_config(_ctx(cfg), key) == {"enabled": True}

    def test_overlay_deep_merges_without_mutating_base(self):
        base = {"enabled": True, "nested": {"a": 1, "b": 2}}
        cfg = {"groups": {"G28_ccr": base},
               "tenants": {"acme": {"groups": {"G28_ccr": {"nested": {"b": 99}}}}}}
        merged = resolve_group_config(_ctx(cfg), "G28_ccr")
        assert merged["nested"] == {"a": 1, "b": 99}, "sibling key dropped — shallow merge"
        assert base["nested"] == {"a": 1, "b": 2}, "base config mutated in place"
