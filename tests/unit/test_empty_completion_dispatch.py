"""Tracker 23.41 / 23.45 / 23.46 / 23.52 — the empty-completion signal must actually
reach every surface it claims.

Detection (23.18) landed with six green tests, and every one of them called
``AuditLogger.log_security_events`` directly. On the production path nothing calls it
directly: ``main._schedule_security_audit`` is the only dispatcher, and it early-returned
unless a trust & safety action was set. So the ``completion.empty`` audit row could only
ever appear when an UNRELATED guardrail fired on the same request — the audit trail for
the one outcome a customer is entitled to see evidenced was silent, and the tests
certified it because they bypassed the gate.

The rest of this file pins the other three claims made about the signal:
  * a cached empty answer is refused on READ, not only on write (the entry stored before
    the fix has an hour/a day of TTL left and nothing can evict it);
  * the metric's help text does not claim a coverage it does not have (streamed responses
    skip the response pipeline entirely, so the DETECTOR never runs on them);
  * failover and the deferred cascade both build their params through
    ``providers.outgoing_params_for`` — the single seam that makes the output-budget
    reservation apply to every provider call, and a structural claim a refactor could
    break in silence.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# -- 23.41 - the dispatcher gate ----------------------------------------------
def _audit_ctx(**kw):
    ctx = SimpleNamespace(
        request_id="req-empty-1",
        tenant_id="NOVA-STG-01",
        user_id="u@x.test",
        guardrail_action=None,
        pii_action=None,
        context_trust_action=None,
        context_trust_pii_action=None,
        tool_eligibility_action=None,
        tool_dispatch_blocked=None,
        empty_completion=None,
        routed_model="o4-mini",
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def test_schedule_security_audit_dispatches_an_empty_completion_on_its_own():
    """The production dispatcher, driven with ONLY empty_completion set.

    This is the test the original six were missing: they called log_security_events
    directly and so never touched the gate that decides whether it is called at all.
    """
    import main as main_mod

    ctx = _audit_ctx(empty_completion={
        "finish_reason": "length", "reason": "length",
        "completion_tokens": 1024, "reasoning_tokens": 1024,
    })
    logger = MagicMock()
    logger.log_security_events = MagicMock(return_value=None)
    created = []
    with patch.object(main_mod, "_audit_logger", logger), \
            patch.object(main_mod.asyncio, "create_task", side_effect=created.append):
        main_mod._schedule_security_audit(ctx)

    assert created, (
        "an empty billed completion produced NO audit task - the completion.empty row "
        "can only appear when an unrelated trust & safety event fires on the same request"
    )
    logger.log_security_events.assert_called_once_with(ctx)


def test_schedule_security_audit_still_skips_a_clean_request():
    """The gate must stay a gate: nothing flagged, no task, no wasted row."""
    import main as main_mod

    logger = MagicMock()
    created = []
    with patch.object(main_mod, "_audit_logger", logger), \
            patch.object(main_mod.asyncio, "create_task", side_effect=created.append):
        main_mod._schedule_security_audit(_audit_ctx())
    assert created == []


# -- 23.45 - a cached empty answer is refused on READ --------------------------
def test_cache_short_circuit_refuses_an_empty_cached_answer():
    """An empty answer cached BEFORE the store-side guard existed is still live for its
    full TTL (L1 1h / L2 24h) and the short-circuit returns without the response
    pipeline, so nothing on that path can detect it. The read side must refuse it."""
    import main as main_mod

    empty = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ""},
                     "finish_reason": "length"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 1024, "total_tokens": 1064},
    }
    from savings.models import SavingsRecord

    rec = SavingsRecord(
        request_id="req-cache-empty", user_id="u@x.test",
        timestamp=__import__("datetime").datetime.now(),
        model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=40,
    )
    rec.cache_hit = True
    rec.cache_level = "L1"
    # The step G05 writes at LOOKUP time, before anything has looked at the body.
    rec.add_step("G05", "L1 exact-match cache hit", 40, 0)
    ctx = SimpleNamespace(
        request_id="req-cache-empty", cache_hit=True, bypassed=False,
        cache_response=empty, cache_level="L1", no_cache=False, savings=rec,
    )
    assert main_mod._refuse_empty_cache_hit(ctx) is True
    # A refused hit must leave NO cache saving on the ledger: the request is about to be
    # served by the provider in full, and a surviving G05 step would claim a 100% saving
    # for it — the plan-time phantom D-073 removed from G06, through a different door.
    assert [s.group for s in rec.step_savings] == []
    assert rec.cache_level is None
    assert ctx.cache_hit is False
    assert ctx.cache_response is None
    assert ctx.savings.cache_hit is False
    # `no_cache` stays OFF on purpose: the fresh answer overwrites the poisoned key, so
    # the path self-heals on the first refusal instead of paying a provider call for
    # every request until the TTL expires. G05's store-side guard stops a bad answer
    # being written back, so nothing can re-poison it.
    assert ctx.no_cache is False


def test_cache_short_circuit_keeps_a_real_cached_answer():
    import main as main_mod

    good = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris"},
                     "finish_reason": "stop"}],
    }
    ctx = SimpleNamespace(
        request_id="req-cache-ok", cache_hit=True, bypassed=False,
        cache_response=good, cache_level="L2", no_cache=False,
        savings=SimpleNamespace(cache_hit=True, cache_level="L2"),
    )
    assert main_mod._refuse_empty_cache_hit(ctx) is False
    assert ctx.cache_hit is True
    assert ctx.cache_response is good


def test_a_bypass_response_is_not_treated_as_a_cache_hit():
    """``bypassed`` shares the same branch but never came from the cache - refusing it
    would send a deliberately answer-less bypass to the provider."""
    import main as main_mod

    ctx = SimpleNamespace(
        request_id="req-bypass", cache_hit=False, bypassed=True,
        cache_response={"choices": []}, no_cache=False,
        savings=SimpleNamespace(),
    )
    assert main_mod._refuse_empty_cache_hit(ctx) is False
    assert ctx.bypassed is True


# -- 23.46 - the metric's help text must not over-claim ------------------------
def test_empty_completion_help_text_does_not_claim_stream_coverage():
    """The detector runs in G18's response path, and streamed responses skip that path
    entirely. The reservation at the provider seam still applies to a stream, so the
    HARM is prevented - but the claim 'never bypassable' was false, and a false claim
    about a trust signal is itself the defect."""
    from middleware.g18_observability import EMPTY_COMPLETIONS

    help_text = EMPTY_COMPLETIONS._documentation.lower()
    assert "never bypassable" not in help_text
    assert "stream" in help_text, (
        "the help text must state the streaming limitation the way G32's is documented"
    )


def test_config_reference_documents_the_streaming_limitation():
    from pathlib import Path

    doc = Path(__file__).resolve().parents[2] / "docs" / "config-reference.md"
    text = doc.read_text(encoding="utf-8", errors="replace").lower()
    assert "token_opt_empty_completion_total" in text
    idx = text.index("token_opt_empty_completion_total")
    window = text[max(0, idx - 2500):idx + 2500]
    assert "stream" in window, (
        "the empty-completion section must carry the non-streaming caveat, the way the "
        "G32 tool-policy section documents its own"
    )


# -- 23.43 - ONE policy, ONE derivation ----------------------------------------
def test_g06_routing_floor_agrees_with_the_adapter_on_a_bare_config():
    """One policy must not have two derivations.

    G06 read `reasoning_headroom` from config ALONE while the adapter merged config over
    its own `_DEFAULT_ALLOWANCES`. On a deployment carrying no `reasoning_headroom` block
    — the shipped default — G06 fell back to medium/4096 for every tier and demanded
    4,608 tokens for an `off` request, while the adapter would have asked for 1,536 and
    left the budget alone. G06 therefore refused routes the reservation would never have
    needed to touch. It must ASK the adapter instead of re-deriving.
    """
    from middleware.g06_routing import _reasoning_budget_starved
    from providers import get_adapter

    bare = {"providers": [], "groups": {}}          # no reasoning_headroom block at all
    ctx = SimpleNamespace(config=bare, params={"reasoning_effort": "off"})
    adapter = get_adapter("o4-mini", [])
    needed = adapter.reasoning_headroom_needed("o4-mini", bare, "off")
    assert needed is not None

    # A budget that already clears the adapter's bar must not be refused by G06.
    ok_budget = needed + 1
    assert adapter.reserve_reasoning_headroom(
        {"max_completion_tokens": ok_budget}, "o4-mini", bare, "off") is None
    assert _reasoning_budget_starved(ctx, "o4-mini", ok_budget) is False, (
        "G06 refused a route the provider seam would have served unchanged - the two "
        "sides of one policy disagree"
    )
    # And a budget below it must still be refused, by both.
    assert _reasoning_budget_starved(ctx, "o4-mini", needed - 1) is True


def test_g06_does_not_re_derive_the_allowance_table():
    """Structural: middleware must carry no allowance numbers and no provider names."""
    import inspect

    from middleware.g06_routing import _reasoning_budget_starved

    src = inspect.getsource(_reasoning_budget_starved)
    assert "reasoning_headroom_needed" in src
    for leaked in ("allowance_tokens", "answer_floor_tokens", "assumed_effort", "4096"):
        assert leaked not in src, (
            f"{leaked!r} is the adapter's business - re-deriving it here is how the two "
            "sides drifted apart"
        )


# -- 23.52 - every call path builds params through the one seam ----------------
def test_failover_targets_build_params_through_outgoing_params_for():
    """A structural claim with no test: if a refactor gives failover its own param
    assembly, the output-budget reservation silently stops applying to it."""
    import main as main_mod

    # `_lazy_fallback_target` builds EVERY failover target (resolution is deferred into
    # `invoke` so it only happens once the primary has failed).
    body = inspect.getsource(main_mod._lazy_fallback_target)
    assert "_outgoing_params_for" in body, (
        "failover builds its own outgoing params - the reasoning-headroom reservation "
        "would not apply to a failover target"
    )
    # And it must not hand-roll a params dict from ctx.params instead.
    assert "ctx.params.items()" not in body


def test_cascade_tiers_build_params_through_outgoing_params_for():
    """`_tier_params` is a closure inside the cascade executor, so this reads the
    executor's source: every tier call must be built from the shared provider seam."""
    from middleware import g06_routing

    body = inspect.getsource(g06_routing._execute_three_tier_cascade)
    assert "def _tier_params" in body and "outgoing_params_for(" in body, (
        "the cascade's per-tier params must come from the shared provider seam"
    )
    # All three tiers go through it - none may assemble params of its own.
    assert body.count("_tier_params(") >= 3
