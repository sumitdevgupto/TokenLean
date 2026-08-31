"""Unit tests for the G32 tool-call eligibility gate + its policy engine.

Covers the two things that make this a *security* control rather than a filter:
the policy must never silently stop denying (malformed globs are rejected, not
ignored), and the block path must leave the message well-formed so an SDK client
does not break on a response we edited.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timezone

import pytest

from middleware import RequestContext
from middleware.g32_tool_eligibility import G32ToolEligibility
from guardrails.tool_policy import (
    ToolPolicyError,
    evaluate_tool,
    normalize_policy,
    validate_pattern,
    REASON_DENY_MATCH,
    REASON_ALLOW_MATCH,
    REASON_DEFAULT,
    REASON_INVALID_NAME,
)
from savings.models import SavingsRecord


def _ctx(mode="flag", enabled=True, policy=None, **extra):
    cfg = {"enabled": enabled, "mode": mode, "policy": policy or {}}
    cfg.update(extra)
    return RequestContext(
        request_id="req-g32", user_id="u",
        original_messages=[], messages=[],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params={},
        config={"groups": {"G32_tool_eligibility": cfg}},
        savings=SavingsRecord(request_id="req-g32", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


def _call(name, cid="call_1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _response(*names, finish_reason="tool_calls", content=None):
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content,
                        "tool_calls": [_call(n, f"call_{i}") for i, n in enumerate(names)]},
            "finish_reason": finish_reason,
        }],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Policy engine — guardrails/tool_policy.py
# ══════════════════════════════════════════════════════════════════════════════
class TestPolicyEngine:
    def test_deny_wins_over_allow(self):
        # Adding a tool to `allow` must never re-open something explicitly denied.
        p = normalize_policy({"allow": ["db_*"], "deny": ["db_drop"]})
        assert evaluate_tool(p, "db_drop").allowed is False
        assert evaluate_tool(p, "db_drop").reason == REASON_DENY_MATCH
        assert evaluate_tool(p, "db_read").allowed is True
        assert evaluate_tool(p, "db_read").reason == REASON_ALLOW_MATCH

    def test_default_allow_lets_unmatched_through(self):
        p = normalize_policy({"deny": ["shell_exec"], "default": "allow"})
        v = evaluate_tool(p, "anything_else")
        assert v.allowed is True and v.reason == REASON_DEFAULT

    def test_default_deny_is_an_allowlist(self):
        p = normalize_policy({"allow": ["safe_*"], "default": "deny"})
        assert evaluate_tool(p, "safe_read").allowed is True
        assert evaluate_tool(p, "rm_rf").allowed is False
        assert evaluate_tool(p, "rm_rf").reason == REASON_DEFAULT

    def test_matching_is_case_sensitive(self):
        # fnmatch.fnmatch would lowercase on Windows; fnmatchcase must not.
        p = normalize_policy({"deny": ["db_drop"]})
        assert evaluate_tool(p, "db_drop").allowed is False
        assert evaluate_tool(p, "DB_DROP").allowed is True

    def test_noop_policy_detected(self):
        assert normalize_policy({}).is_noop is True
        assert normalize_policy({"default": "deny"}).is_noop is False
        assert normalize_policy({"deny": ["x"]}).is_noop is False

    @pytest.mark.parametrize("bad", ["[", "[a-", "a[b", "", 123, None, ["nested"]])
    def test_malformed_glob_is_rejected_not_ignored(self, bad):
        # The whole point: fnmatch silently returns False for these, so a typo'd DENY
        # pattern would stop denying with nothing reported anywhere.
        with pytest.raises(ToolPolicyError):
            normalize_policy({"deny": [bad]})

    @pytest.mark.parametrize("good", ["tool_[abc]_x", "[!a]bc", "[]]x", "*", "a?c"])
    def test_valid_globs_accepted(self, good):
        assert validate_pattern(good) == good

    def test_bad_default_rejected(self):
        with pytest.raises(ToolPolicyError):
            normalize_policy({"default": "maybe"})

    def test_unknown_keys_ignored_for_forward_compat(self):
        p = normalize_policy({"allow": ["x"], "future_knob": 42, "version": "9"})
        assert p.allow == ("x",)

    def test_bare_string_accepted_as_single_pattern(self):
        assert normalize_policy({"deny": "shell_exec"}).deny == ("shell_exec",)

    def test_invalid_tool_name_is_fail_closed(self):
        # Malformed provider output no dispatcher could route anyway — deny it even
        # when `default: allow`.
        p = normalize_policy({"default": "allow"})
        for name in ("", None, 123):
            v = evaluate_tool(p, name)
            assert v.allowed is False and v.reason == REASON_INVALID_NAME


# ══════════════════════════════════════════════════════════════════════════════
# Middleware — modes
# ══════════════════════════════════════════════════════════════════════════════
class TestModes:
    @pytest.mark.asyncio
    async def test_flag_records_but_never_mutates(self):
        ctx = _ctx(mode="flag", policy={"deny": ["shell_exec"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec", "db_read"))
        assert ctx.tool_eligibility_action == "flag"
        assert ctx.tool_eligibility_denied == ["shell_exec"]
        assert ctx.tool_eligibility_count == 1
        # untouched: both calls still present, finish_reason unchanged
        msg = resp["choices"][0]["message"]
        assert [c["function"]["name"] for c in msg["tool_calls"]] == ["shell_exec", "db_read"]
        assert resp["choices"][0]["finish_reason"] == "tool_calls"
        assert resp["_token_opt"]["tool_eligibility"]["stripped"] is False
        # flag mutates nothing, so caching stays safe
        assert ctx.no_cache is False

    @pytest.mark.asyncio
    async def test_off_mode_does_not_evaluate(self):
        ctx = _ctx(mode="off", policy={"deny": ["*"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        assert ctx.tool_eligibility_action is None
        assert len(resp["choices"][0]["message"]["tool_calls"]) == 1

    @pytest.mark.asyncio
    async def test_disabled_group_does_not_evaluate(self):
        ctx = _ctx(enabled=False, mode="block", policy={"deny": ["*"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        assert ctx.tool_eligibility_action is None
        assert len(resp["choices"][0]["message"]["tool_calls"]) == 1

    @pytest.mark.asyncio
    async def test_noop_policy_is_byte_identical(self):
        # The shipped default (enabled, flag, empty policy) must change nothing.
        ctx = _ctx(mode="flag", policy={})
        original = _response("shell_exec", "db_read")
        import copy
        expected = copy.deepcopy(original)
        resp = await G32ToolEligibility().process_response(ctx, original)
        assert resp == expected
        assert ctx.tool_eligibility_action is None

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_back_to_flag(self):
        ctx = _ctx(mode="banana", policy={"deny": ["shell_exec"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        assert ctx.tool_eligibility_action == "flag"
        assert len(resp["choices"][0]["message"]["tool_calls"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Middleware — block-mode well-formedness (the highest-risk path)
# ══════════════════════════════════════════════════════════════════════════════
class TestBlockWellFormedness:
    @pytest.mark.asyncio
    async def test_partial_denial_keeps_finish_reason_and_content(self):
        ctx = _ctx(mode="block", policy={"deny": ["shell_exec"]})
        resp = await G32ToolEligibility().process_response(
            ctx, _response("shell_exec", "db_read", content="thinking"))
        choice = resp["choices"][0]
        assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == ["db_read"]
        assert choice["finish_reason"] == "tool_calls"   # still has a call to make
        assert choice["message"]["content"] == "thinking"
        assert ctx.no_cache is True

    @pytest.mark.asyncio
    async def test_full_denial_drops_key_and_rewrites_finish_reason(self):
        ctx = _ctx(mode="block", policy={"deny": ["*"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec", "rm_rf"))
        choice = resp["choices"][0]
        # key dropped entirely, not left as []
        assert "tool_calls" not in choice["message"]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"]           # never left null
        assert ctx.tool_eligibility_count == 2

    @pytest.mark.asyncio
    async def test_finish_reason_already_stop_is_left_alone(self):
        # Some providers return "stop" alongside tool calls; clobbering it is its own bug.
        ctx = _ctx(mode="block", policy={"deny": ["*"]})
        resp = await G32ToolEligibility().process_response(
            ctx, _response("shell_exec", finish_reason="stop"))
        assert resp["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_existing_content_is_not_overwritten_on_full_denial(self):
        ctx = _ctx(mode="block", policy={"deny": ["*"]})
        resp = await G32ToolEligibility().process_response(
            ctx, _response("shell_exec", content="here is my reasoning"))
        assert resp["choices"][0]["message"]["content"] == "here is my reasoning"

    @pytest.mark.asyncio
    async def test_custom_block_message_used(self):
        ctx = _ctx(mode="block", policy={"deny": ["*"]}, block_message="nope.")
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        assert resp["choices"][0]["message"]["content"] == "nope."

    @pytest.mark.asyncio
    async def test_multiple_choices_each_processed(self):
        ctx = _ctx(mode="block", policy={"deny": ["shell_exec"]})
        resp = _response("shell_exec")
        resp["choices"].append({
            "index": 1,
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [_call("shell_exec", "call_9")]},
            "finish_reason": "tool_calls",
        })
        out = await G32ToolEligibility().process_response(ctx, resp)
        for choice in out["choices"]:
            assert "tool_calls" not in choice["message"]
            assert choice["finish_reason"] == "stop"
        assert ctx.tool_eligibility_count == 2      # counts calls, not distinct names

    @pytest.mark.asyncio
    async def test_response_without_tool_calls_untouched(self):
        ctx = _ctx(mode="block", policy={"deny": ["*"]})
        plain = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                              "finish_reason": "stop"}]}
        out = await G32ToolEligibility().process_response(ctx, plain)
        assert out == plain
        assert ctx.tool_eligibility_action is None

    @pytest.mark.asyncio
    async def test_malformed_response_shapes_do_not_raise(self):
        ctx = _ctx(mode="block", policy={"deny": ["*"]})
        g32 = G32ToolEligibility()
        for shape in ({}, {"choices": None}, {"choices": ["not-a-dict"]},
                      {"choices": [{"message": None}]},
                      {"choices": [{"message": {"tool_calls": "not-a-list"}}]},
                      {"choices": [{"message": {"tool_calls": [None, 42]}}]}):
            await g32.process_response(ctx, shape)


# ══════════════════════════════════════════════════════════════════════════════
# Failure semantics + policy cache
# ══════════════════════════════════════════════════════════════════════════════
class TestFailureSemantics:
    @pytest.mark.asyncio
    async def test_evaluation_error_denies_only_that_call(self, monkeypatch):
        # Fail-closed, but bounded: the other call must still be served.
        import middleware.g32_tool_eligibility as mod
        real = mod.evaluate_tool

        def boom(policy, name):
            if name == "explode":
                raise RuntimeError("engine blew up")
            return real(policy, name)

        monkeypatch.setattr(mod, "evaluate_tool", boom)
        ctx = _ctx(mode="block", policy={"deny": ["never_matches"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("explode", "db_read"))
        names = [c["function"]["name"] for c in resp["choices"][0]["message"]["tool_calls"]]
        assert names == ["db_read"]
        assert ctx.tool_eligibility_denied == ["explode"]

    @pytest.mark.asyncio
    async def test_invalid_policy_retains_last_good(self, caplog):
        g32 = G32ToolEligibility()
        good = _ctx(mode="block", policy={"deny": ["shell_exec"]})
        await g32.process_response(good, _response("shell_exec"))
        # now a broken policy arrives via hot-reload
        bad = _ctx(mode="block", policy={"deny": ["["]})
        resp = await g32.process_response(bad, _response("shell_exec", "db_read"))
        names = [c["function"]["name"] for c in resp["choices"][0]["message"]["tool_calls"]]
        assert names == ["db_read"], "last-good policy must still deny shell_exec"

    @pytest.mark.asyncio
    async def test_invalid_policy_with_no_last_good_is_inert_and_logs_error(self, caplog):
        import logging
        caplog.set_level(logging.ERROR)
        ctx = _ctx(mode="block", policy={"deny": ["["]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        # Inert rather than denying everything (that would be an outage on a typo) —
        # but it must be LOUD.
        assert len(resp["choices"][0]["message"]["tool_calls"]) == 1
        assert any("INERT" in r.message or "INERT" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_policy_cache_rebuilds_on_config_change(self):
        g32 = G32ToolEligibility()
        a = _ctx(mode="block", policy={"deny": ["tool_a"]})
        await g32.process_response(a, _response("tool_a"))
        assert a.tool_eligibility_count == 1
        # Different tenant/config in the same process must NOT reuse the cached policy.
        b = _ctx(mode="block", policy={"deny": ["tool_b"]})
        resp = await g32.process_response(b, _response("tool_a"))
        assert b.tool_eligibility_action is None, "tool_a is allowed under policy B"
        assert len(resp["choices"][0]["message"]["tool_calls"]) == 1

    @pytest.mark.asyncio
    async def test_alternating_tenant_policies_stay_isolated(self):
        # The cache holds one entry; alternating configs must each get their own answer.
        g32 = G32ToolEligibility()
        for _ in range(3):
            a = _ctx(mode="block", policy={"deny": ["tool_a"]})
            await g32.process_response(a, _response("tool_a"))
            assert a.tool_eligibility_denied == ["tool_a"]
            b = _ctx(mode="block", policy={"deny": ["tool_b"]})
            await g32.process_response(b, _response("tool_a"))
            assert b.tool_eligibility_denied == []


# ══════════════════════════════════════════════════════════════════════════════
# Observability
# ══════════════════════════════════════════════════════════════════════════════
class TestObservability:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["flag", "block"])
    async def test_metric_emitted_with_mode_label(self, mode):
        from middleware.quality_metrics import TOOL_ELIGIBILITY_DENIED_TOTAL
        before = TOOL_ELIGIBILITY_DENIED_TOTAL.labels(tenant_id="default", mode=mode)._value.get()
        ctx = _ctx(mode=mode, policy={"deny": ["shell_exec"]})
        await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        after = TOOL_ELIGIBILITY_DENIED_TOTAL.labels(tenant_id="default", mode=mode)._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_metrics_can_be_disabled(self):
        ctx = _ctx(mode="flag", policy={"deny": ["shell_exec"]}, metrics_enabled=False)
        await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        assert ctx.tool_eligibility_action == "flag"   # still recorded on ctx + audit

    @pytest.mark.asyncio
    async def test_audit_row_is_pii_free_and_names_the_tools(self):
        from audit.log import AuditLogger
        rows = []

        class _Capture(AuditLogger):
            def __init__(self):
                self._db_pool = object()

            async def log_config_change(self, **kw):
                rows.append(kw)
                return True

        ctx = _ctx(mode="block", policy={"deny": ["shell_exec"]})
        await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        await _Capture().log_security_events(ctx)
        assert len(rows) == 1
        assert rows[0]["action"] == "tool_eligibility.denied"
        assert rows[0]["details"] == {"tools": ["shell_exec"], "count": 1, "mode": "block"}

    @pytest.mark.asyncio
    async def test_audit_action_differs_for_flag(self):
        from audit.log import AuditLogger
        rows = []

        class _Capture(AuditLogger):
            def __init__(self):
                self._db_pool = object()

            async def log_config_change(self, **kw):
                rows.append(kw)
                return True

        ctx = _ctx(mode="flag", policy={"deny": ["shell_exec"]})
        await G32ToolEligibility().process_response(ctx, _response("shell_exec"))
        await _Capture().log_security_events(ctx)
        assert rows[0]["action"] == "tool_eligibility.flagged"

    @pytest.mark.asyncio
    async def test_annotation_records_denied_names(self):
        ctx = _ctx(mode="block", policy={"deny": ["shell_exec"]})
        resp = await G32ToolEligibility().process_response(ctx, _response("shell_exec", "db_read"))
        ann = resp["_token_opt"]["tool_eligibility"]
        assert ann == {"mode": "block", "denied": ["shell_exec"], "stripped": True}
