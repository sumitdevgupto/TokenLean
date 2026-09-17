"""FIX-2 / FIX-3 — an empty billed answer is detected, disclosed and never cached
(tracker 23.18).

The worst thing this proxy can do is bill a customer in full for a response that
contains nothing. On 2026-09-07 it did exactly that 18 times in 54 requests and NOTHING
noticed: billed as a normal 200, and the savings ledger recorded a small POSITIVE saving
on each one. These tests pin the three layers that now stop it:

  * detection is unconditional (no enable knob, outside the observability gate);
  * the empty answer is refused by the cache at the store, independently of ordering;
  * G06 will not route INTO a reasoning model whose thinking cannot fit even after the
    provider-seam reservation raises the caller's budget (backlog #58, corrected
    2026-09-16 — see the tests below for what the ORIGINAL, wrong version pinned).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from middleware.g05_cache import is_empty_answer as _is_empty_answer
from middleware.g18_observability import G18Observability, detect_empty_completion


def _response(content, finish_reason="length", completion=1024, reasoning=1024,
              tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 48,
            "completion_tokens": completion,
            "total_tokens": 48 + completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def _ctx(config=None):
    return SimpleNamespace(
        request_id="rq-1", tenant_id="T1", routed_model="o4-mini",
        config=config if config is not None else {"groups": {}},
        no_cache=False, empty_completion=None, params={}, savings=None,
    )


# ── detection ─────────────────────────────────────────────────────────────────


def test_the_exact_production_shape_is_detected():
    """content "", finish_reason "length", 1024 reasoning tokens — the DS11 shape."""
    ctx = _ctx()
    detect_empty_completion(ctx, _response(""))
    assert ctx.empty_completion == {
        "finish_reason": "length",
        "completion_tokens": 1024,
        "reasoning_tokens": 1024,
        "reason": "length",
    }
    assert ctx.no_cache is True


def test_detection_forbids_caching():
    """Without this, G05 stores the empty answer and replays it to every look-alike
    question for the L2 TTL (24h by default) without ever calling the provider again."""
    ctx = _ctx()
    detect_empty_completion(ctx, _response(None))
    assert ctx.no_cache is True


@pytest.mark.parametrize("content", ["", "   ", "\n\t", None, []])
def test_every_empty_shape_counts(content):
    ctx = _ctx()
    detect_empty_completion(ctx, _response(content))
    assert ctx.empty_completion is not None


def test_an_answer_is_not_flagged():
    ctx = _ctx()
    detect_empty_completion(ctx, _response("42", finish_reason="stop", reasoning=200))
    assert ctx.empty_completion is None
    assert ctx.no_cache is False


def test_a_tool_call_is_an_answer():
    """An empty content with tool_calls is the normal agentic shape, not a failure."""
    ctx = _ctx()
    detect_empty_completion(
        ctx, _response(None, finish_reason="tool_calls",
                       tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    )
    assert ctx.empty_completion is None
    assert ctx.no_cache is False


def test_a_guardrail_refusal_is_not_counted_as_a_defect():
    """G29/G30 blocks are deliberate and already disclosed on their own surfaces;
    counting them here would report a working safety feature as customer harm."""
    ctx = _ctx()
    detect_empty_completion(ctx, _response("", finish_reason="content_filter",
                                           completion=0, reasoning=0))
    assert ctx.empty_completion is None
    assert ctx.no_cache is False


def test_an_empty_stop_is_reported_under_its_own_reason():
    ctx = _ctx()
    detect_empty_completion(ctx, _response("", finish_reason="stop", reasoning=0))
    assert ctx.empty_completion["reason"] == "stop_empty"


def test_no_choices_at_all_is_not_our_case():
    """A response with no choices is a streamed/non-chat shape, not a billed empty
    answer. Flagging it would put a false empty on every streaming turn."""
    ctx = _ctx()
    for shape in ({}, {"choices": []}, {"choices": "nope"}):
        detect_empty_completion(ctx, shape)
        assert ctx.empty_completion is None, shape


def test_a_choice_that_exists_but_carries_nothing_IS_flagged():
    """Deliberate: a malformed choice is still a billed 200 with no answer in it, and it
    must not be cached either. Conservative on purpose."""
    ctx = _ctx()
    detect_empty_completion(ctx, {"choices": [None], "usage": {}})
    assert ctx.empty_completion["reason"] == "empty"
    assert ctx.no_cache is True


def test_detection_never_raises():
    ctx = _ctx()
    for bad in (None, {"choices": [{"message": "not-a-dict"}]}, {"usage": "nope"}):
        detect_empty_completion(ctx, bad)  # must not propagate


@pytest.mark.asyncio
async def test_detection_runs_even_when_observability_is_disabled():
    """An honesty signal a deployment can switch off is not a signal. G18's own enable
    gate must not gate the thing that refuses to cache an empty answer."""
    ctx = _ctx(config={"groups": {"G18_observability": {"enabled": False}}})
    await G18Observability().record(ctx, _response(""))
    assert ctx.empty_completion is not None
    assert ctx.no_cache is True


# ── the cache store guard (belt AND braces — ordering alone is not a guarantee) ─


@pytest.mark.parametrize("content,expected", [
    ("", True), (None, True), ("   ", True), ([], True),
    ("an answer", False), ([{"type": "text", "text": "hi"}], False),
])
def test_is_empty_answer(content, expected):
    assert _is_empty_answer(_response(content)) is expected


def test_is_empty_answer_ignores_a_real_tool_call():
    assert _is_empty_answer(_response(
        None, tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]
    )) is False


def test_a_zero_choices_body_is_empty_backlog_64():
    """Corrected 2026-09-17. `_is_empty_answer` used to return False - "not empty" -
    for a body with no choices at all, which was backwards: this function gates BOTH
    the cache write (`g05_cache.store_response`) and the cache read
    (`main._refuse_empty_cache_hit`, same function under an alias), and neither call
    site is ever reached by a streaming response (it returns via `_stream_response`
    before the response pipeline runs). So a zero-choices body reaching here is always
    a malformed provider reply, never a legitimate streaming shape - and it is exactly
    what this guard exists to keep out of the cache: it would otherwise be replayed to
    every look-alike question for a full TTL (L1 1h, L2 24h).

    NOTE this is the opposite of `test_no_choices_at_all_is_not_our_case` above, which
    pins `detect_empty_completion` (g18_observability.py) - a DIFFERENT function with a
    documented reason to treat a no-choices body as out of scope. The two functions
    serve different purposes (a billing/disclosure counter vs. a cache-poisoning guard)
    and are not required to agree; only this one is backlog #64's OSS `g05_cache.py`
    scope."""
    assert _is_empty_answer({}) is True
    assert _is_empty_answer({"choices": []}) is True
    assert _is_empty_answer({"choices": "nope"}) is True


def test_is_empty_answer_never_raises_backlog_79():
    """Found writing the #64 fix: the old code indexed `choices[0]` and called `.get()`
    on it with no type checking, so 5 of 6 malformed shapes tried here crashed with
    AttributeError. Nothing between this function and its two call sites
    (`g05_cache.store_response`, `main._refuse_empty_cache_hit`) catches an exception -
    `pipeline._run_timed` wraps the call in try/FINALLY, not try/except - so a crash here
    used to propagate into the response pipeline and 500 an otherwise-successful request.
    Anything this function cannot parse is treated as empty: too malformed to make sense
    of is too malformed to trust."""
    for bad in ({"choices": "nope"}, {"choices": [None]}, {"choices": ["x"]},
                {"choices": [{"message": "not-a-dict"}]}, {"choices": [{"message": 5}]},
                "not-a-dict-at-all", None, 42, [], {"choices": None}):
        assert _is_empty_answer(bad) is True, f"should be treated as empty: {bad!r}"


def test_is_empty_answer_import_contract_backlog_59():
    """`main._refuse_empty_cache_hit` reuses THIS function (aliased on import) so the
    cache write-gate and read-gate can never drift apart — but that only holds while
    the import resolves to the exact same object. Before backlog #59 it crossed the
    module boundary under a private (`_`-prefixed) name with nothing pinning the
    coupling, so a routine rename inside g05_cache would break main.py at import time
    with no test naming why. Promoted to a public name; this test is the second half —
    it fails LOUDLY, at the coupling, if a future rename ever breaks it."""
    import main as proxy_main
    from middleware.g05_cache import is_empty_answer

    assert proxy_main._is_empty_cached_answer is is_empty_answer


@pytest.mark.asyncio
async def test_g05_refuses_to_store_an_empty_answer_even_if_no_cache_was_cleared():
    """The independent half of the guard: even with ctx.no_cache reset (a future stage,
    a different code path), the empty answer must not reach Redis."""
    from middleware.g05_cache import G05Cache

    ctx = SimpleNamespace(
        request_id="rq-2", tenant_id="T1", redis_prefix="t:T1:",
        cache_hit=False, bypassed=False, no_cache=False, agent_dispatched=False,
        config={"groups": {"G5_cache": {"enabled": True}}},
        params={}, messages=[{"role": "user", "content": "hi"}], model="o4-mini",
    )
    redis = AsyncMock()
    with patch("middleware.g05_cache._get_redis", return_value=redis):
        await G05Cache().store_response(ctx, _response(""))
    redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_g05_still_stores_a_real_answer():
    from middleware.g05_cache import G05Cache

    ctx = SimpleNamespace(
        request_id="rq-3", tenant_id="T1", redis_prefix="t:T1:",
        cache_hit=False, bypassed=False, no_cache=False, agent_dispatched=False,
        config={"groups": {"G5_cache": {"enabled": True, "step_cache_enabled": False}}},
        params={"_g05_l1_cache_key": "t:T1:k"},
        messages=[{"role": "user", "content": "hi"}], model="o4-mini",
    )
    redis = AsyncMock()
    with patch("middleware.g05_cache._get_redis", return_value=redis), \
            patch("middleware.g05_cache._l2_store", AsyncMock()):
        await G05Cache().store_response(ctx, _response("42", finish_reason="stop"))
    redis.set.assert_awaited()


# ── FIX-3: G06 will not route into a reasoning model the budget cannot fund ────


def _headroom_config():
    return {
        "providers": [],
        "groups": {"G12_reasoning": {"reasoning_headroom": {
            "enabled": True,
            "answer_floor_tokens": 512,
            "allowance_tokens": {"low": 1024, "medium": 4096, "high": 16384},
            "assumed_effort": "medium",
        }}},
    }


def test_caller_output_budget_reads_the_key_the_caller_actually_used():
    from middleware.g06_routing import _caller_output_budget

    assert _caller_output_budget({"max_completion_tokens": 1024}) == 1024
    assert _caller_output_budget({"max_tokens": 256}) == 256
    # The reasoning key wins — it is the one the provider honours on those models.
    assert _caller_output_budget({"max_completion_tokens": 8, "max_tokens": 99}) == 8
    assert _caller_output_budget({}) is None
    assert _caller_output_budget({"max_tokens": 0}) is None
    assert _caller_output_budget({"max_tokens": "lots"}) is None


def test_a_low_current_budget_alone_is_not_starvation_backlog_58():
    """THE regression test. The original version of this guard compared the caller's
    CURRENT budget to `needed` and refused to route whenever budget < needed — but
    `reserve_reasoning_headroom` (providers/openai_adapter.py) raises that exact budget
    to `needed` on the very next step, for exactly this reason. So the old guard fired on
    precisely the cases the reservation already handles, and refused a legitimately
    cheaper route for no reason. With no operator `max_output_tokens` override (defaults
    to 32768, comfortably above any `needed` this test config can produce), a budget the
    caller sized far too small for reasoning must NOT be reported as starved."""
    from middleware.g06_routing import _reasoning_budget_starved

    ctx = SimpleNamespace(config=_headroom_config(), params={"reasoning_effort": "medium"})
    assert _reasoning_budget_starved(ctx, "o4-mini", 1024) is False


def test_reasoning_budget_starved_is_true_only_when_the_operators_cap_cannot_fit_it():
    """The one case the seam genuinely cannot fix: an OPERATOR ceiling below `needed`.
    `needed` here is 512 (answer_floor) + 4096 (medium allowance) = 4608; a cap of 2000
    means the seam can raise the caller's budget only as far as 2000, still insufficient."""
    from middleware.g06_routing import _reasoning_budget_starved

    cfg = _headroom_config()
    cfg["groups"]["G12_reasoning"]["reasoning_headroom"]["max_output_tokens"] = 2000
    ctx = SimpleNamespace(config=cfg, params={"reasoning_effort": "medium"})
    assert _reasoning_budget_starved(ctx, "o4-mini", 1024) is True
    # Already sufficient → never starved, regardless of the cap.
    assert _reasoning_budget_starved(ctx, "o4-mini", 8192) is False
    # Not a reasoning model → not our concern.
    assert _reasoning_budget_starved(ctx, "gpt-4o-mini", 16) is False
    # No caller budget → the provider's own default applies.
    assert _reasoning_budget_starved(ctx, "o4-mini", None) is False


def test_a_cap_that_exactly_meets_the_need_is_not_starvation():
    """Boundary: the seam raises to min(needed, cap); cap == needed means it reaches
    `needed` exactly, which is sufficient by the same rule as `budget >= needed`."""
    from middleware.g06_routing import _reasoning_budget_starved

    cfg = _headroom_config()
    cfg["groups"]["G12_reasoning"]["reasoning_headroom"]["max_output_tokens"] = 4608
    ctx = SimpleNamespace(config=cfg, params={"reasoning_effort": "medium"})
    assert _reasoning_budget_starved(ctx, "o4-mini", 1024) is False


def test_reasoning_budget_floor_honours_the_operator_switch():
    from middleware.g06_routing import _reasoning_budget_starved

    cfg = _headroom_config()
    cfg["groups"]["G12_reasoning"]["reasoning_headroom"]["enabled"] = False
    ctx = SimpleNamespace(config=cfg, params={})
    assert _reasoning_budget_starved(ctx, "o4-mini", 16) is False


def test_reasoning_budget_floor_fails_open():
    """A routing guard that cannot answer must not block routing — the provider-seam
    reservation and the response-path detector are the other two layers."""
    from middleware.g06_routing import _reasoning_budget_starved

    ctx = SimpleNamespace(config=None, params={})
    assert _reasoning_budget_starved(ctx, "o4-mini", 8) is False
