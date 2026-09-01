"""An auto-EXECUTION site must authorize on its own, not inherit a guarantee from ordering.

`g15_server_compute` and `g28_ccr` both dispatch server-side handlers by bare tool-name
match. Until now the only thing between a prompt-injected model and the proxy acting was
that G32 happens to run earlier in the response chain — a property of `pipeline.py`, not
of the dispatch site. Three concrete holes that ordering did not close:

1. **`flag` is the shipped default**, and in `flag` G32 records a call as denied and
   deliberately leaves it in the response (`g32_tool_eligibility.py:220-221`). G15 then
   executed it anyway. A control that logs "denied" and acts regardless is worse than no
   control — the audit row testifies that we knew.
2. **Name collision.** G15 matched `headroom_*` by bare name without consulting whether
   G28 was even enabled, so a tenant declaring its own tool with one of those names had
   it intercepted and run server-side.
3. **Un-advertised names.** Only G28's `expose_mcp_tools` path puts these tools in front
   of a model. A call to one we never offered is an injection or a hallucination.

The refusal leaves the tool call in the response, unexecuted — stripping is the response
gate's job, and G15 has never stripped anything.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from middleware import RequestContext
from middleware.g15_server_compute import G15ServerCompute
from middleware.g28_ccr import _local_store
from middleware.g32_tool_eligibility import (
    REASON_EVALUATION_ERROR,
    REASON_NOT_INJECTED,
    REASON_POLICY_DENIED,
    authorize_dispatch,
)
from savings.models import SavingsRecord


@pytest.fixture(autouse=True)
def _clean_store():
    _local_store.clear()
    yield
    _local_store.clear()


def _ctx(*, injected=True, mode="flag", policy=None, tools=None, tenant_id="acme"):
    return RequestContext(
        request_id="req-authz", user_id="u", original_messages=[], messages=[],
        model="gpt-4o-mini", routed_model="gpt-4o-mini",
        params={"tools": tools} if tools is not None else {},
        config={"groups": {
            "G15_server_compute": {"enabled": True, "headroom_mcp_server": True},
            "G28_ccr": {"ttl_seconds": 60},
            "G32_tool_eligibility": {"enabled": True, "mode": mode, "policy": policy or {}},
        }},
        tenant_id=tenant_id, redis_prefix=f"t:{tenant_id}:",
        ccr_tools_injected=injected,
        savings=SavingsRecord(request_id="req-authz", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


async def _dispatch(ctx, name="headroom_compress", args=None):
    """Returns (executed: bool, the tool_call dict as the caller would receive it)."""
    call = {"id": "c1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args or {"text": "x" * 50})}}
    response = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [call]}}]}
    out = await G15ServerCompute().process_response(ctx, response)
    served = out["choices"][0]["message"]["tool_calls"][0]
    return "result" in served["function"], served


class TestNotInjected:
    """Rule 1 — never execute a name the proxy did not itself advertise."""

    async def test_refuses_when_ccr_tools_were_never_injected(self):
        ctx = _ctx(injected=False)
        executed, _ = await _dispatch(ctx)
        assert not executed, "executed a CCR tool the proxy never advertised"
        assert ctx.tool_dispatch_blocked == ["headroom_compress"]

    async def test_refusal_applies_even_with_policy_off(self):
        """`mode: off` is a kill switch for POLICY. It is not a licence to run tools we
        never offered — that is identity, not policy."""
        executed, _ = await _dispatch(_ctx(injected=False, mode="off"))
        assert not executed

    async def test_tenants_own_same_named_tool_is_not_hijacked(self):
        """The collision case. G28 is off, so nothing was injected; the tenant's own
        `headroom_compress` must reach their client untouched rather than being executed
        against the proxy's CCR store."""
        ctx = _ctx(injected=False, tools=[
            {"type": "function", "function": {"name": "headroom_compress", "parameters": {}}}])
        executed, served = await _dispatch(ctx)
        assert not executed
        assert served["function"]["name"] == "headroom_compress"
        assert _local_store == {}, "the proxy wrote to its store for a tenant's own tool"

    async def test_allows_when_injected_and_permitted(self):
        """The guard must not break the feature it protects."""
        executed, _ = await _dispatch(_ctx(injected=True))
        assert executed


class TestPolicyDenial:
    """Rule 3 — a denied call is not executed, in EVERY mode."""

    @pytest.mark.parametrize("mode", ["flag", "block"])
    async def test_denied_tool_is_not_dispatched(self, mode):
        ctx = _ctx(mode=mode, policy={"deny": ["headroom_*"]})
        executed, _ = await _dispatch(ctx)
        assert not executed, f"policy-denied tool executed in {mode} mode"
        assert ctx.tool_dispatch_blocked == ["headroom_compress"]

    async def test_flag_mode_is_the_one_that_regressed(self):
        """`flag` ships as the default and previously executed denied calls. It still
        leaves the RESPONSE untouched — the call is returned, just not acted on."""
        ctx = _ctx(mode="flag", policy={"deny": ["headroom_compress"]})
        executed, served = await _dispatch(ctx)
        assert not executed
        assert served["function"]["name"] == "headroom_compress", (
            "the call must still reach the caller — G15 does not strip, that is G32's job"
        )

    async def test_default_deny_makes_it_an_allowlist(self):
        executed, _ = await _dispatch(_ctx(policy={"allow": ["db_*"], "default": "deny"}))
        assert not executed

    async def test_allowed_by_policy_still_dispatches(self):
        executed, _ = await _dispatch(_ctx(policy={"allow": ["headroom_*"], "default": "deny"}))
        assert executed

    async def test_mode_off_skips_the_policy_check(self):
        """Operators keep a real kill switch: `off` means the policy is not evaluated."""
        executed, _ = await _dispatch(_ctx(mode="off", policy={"deny": ["headroom_*"]}))
        assert executed


class TestFailClosed:
    """Unlike the cache/bypass hoist, this site fails CLOSED — refusing to act costs the
    caller nothing but a side effect they did not ask for."""

    async def test_evaluation_error_refuses_to_dispatch(self):
        ctx = _ctx()
        with patch("middleware.g32_tool_eligibility.normalize_policy",
                   side_effect=RuntimeError("policy engine down")):
            assert authorize_dispatch(ctx, "headroom_compress") == REASON_EVALUATION_ERROR

    async def test_reasons_are_distinct(self):
        """Each refusal reason is a separate metric label; conflating them would hide
        which of the three holes is being hit in production."""
        assert len({REASON_NOT_INJECTED, REASON_POLICY_DENIED, REASON_EVALUATION_ERROR}) == 3


class TestObservabilityIsSeparateFromG32:
    """The dispatch site must not write G32's response-path state."""

    async def test_does_not_clobber_g32_verdict_fields(self):
        """`ctx.tool_eligibility_*` are ASSIGNED by G32's process_response. Writing them
        here would erase G32's own `denied` list from the audit row."""
        ctx = _ctx(injected=False)
        ctx.tool_eligibility_action = "flag"
        ctx.tool_eligibility_denied = ["something_g32_denied"]
        ctx.tool_eligibility_count = 1

        await _dispatch(ctx)

        assert ctx.tool_eligibility_action == "flag"
        assert ctx.tool_eligibility_denied == ["something_g32_denied"]
        assert ctx.tool_eligibility_count == 1
        assert ctx.tool_dispatch_blocked == ["headroom_compress"]

    async def test_does_not_double_emit_the_g32_denial_counter(self):
        """A call denied at BOTH sites must count once on G32's counter. `record_tool_denied`
        is a bare Counter.inc with no dedupe, so the dispatch site uses its own."""
        from middleware.quality_metrics import TOOL_ELIGIBILITY_DENIED_TOTAL

        def _val():
            for m in TOOL_ELIGIBILITY_DENIED_TOTAL.collect():
                for smp in m.samples:
                    if (smp.labels.get("tenant_id") == "acme"
                            and smp.labels.get("mode") == "flag"
                            and smp.name.endswith("_total")):
                        return smp.value
            return 0.0

        before = _val()
        await _dispatch(_ctx(mode="flag", policy={"deny": ["headroom_*"]}))
        assert _val() == before, "the dispatch site incremented G32's response-path counter"

    async def test_emits_its_own_counter_with_the_reason(self):
        from middleware.quality_metrics import TOOL_DISPATCH_BLOCKED_TOTAL

        def _val(reason):
            for m in TOOL_DISPATCH_BLOCKED_TOTAL.collect():
                for smp in m.samples:
                    if (smp.labels.get("tenant_id") == "acme"
                            and smp.labels.get("reason") == reason
                            and smp.name.endswith("_total")):
                        return smp.value
            return 0.0

        before = _val(REASON_NOT_INJECTED)
        await _dispatch(_ctx(injected=False))
        assert _val(REASON_NOT_INJECTED) == before + 1

    async def test_repeated_refusals_record_the_name_once(self):
        ctx = _ctx(injected=False)
        await _dispatch(ctx)
        await _dispatch(ctx)
        assert ctx.tool_dispatch_blocked == ["headroom_compress"]
