"""FIX-1 — reasoning headroom in the output budget (tracker 23.18).

A reasoning model bills its hidden thinking inside the caller's output budget. A caller
who sizes that budget for the ANSWER gets the whole allowance spent on thinking and an
EMPTY reply, billed in full: 18 of 54 o4-mini requests at ``max_completion_tokens: 1024``
on 2026-09-07, 44.8% of that arm's spend for zero characters.

These tests pin (a) that the reservation happens at the ONE seam every call path shares,
(b) that it is silent-free — the raise is recorded on ctx for disclosure, and (c) that
every request which is not this failure stays byte-identical.
"""
import sys
import types

import pytest

from providers import ProviderAdapter, outgoing_params_for
from providers.openai_adapter import OpenAIAdapter


REASONING_MODEL = "o4-mini"
PLAIN_MODEL = "gpt-4o-mini"


def _cfg(**headroom):
    base = {
        "enabled": True,
        "answer_floor_tokens": 512,
        "allowance_tokens": {"low": 1024, "medium": 4096, "high": 16384},
        "assumed_effort": "medium",
        "max_output_tokens": 32768,
    }
    base.update(headroom)
    return {"groups": {"G12_reasoning": {"reasoning_headroom": base}}, "providers": []}


class _Ctx:
    """Minimal stand-in for RequestContext at the provider seam."""

    def __init__(self, params, config):
        self.params = dict(params)
        self.config = config
        self.tenant_id = "T1"
        self.output_budget_raised = None


# ── the adapter policy ────────────────────────────────────────────────────────


def test_raises_a_budget_that_cannot_hold_thinking_plus_answer():
    params = {"max_completion_tokens": 1024, "reasoning_effort": "medium"}
    raised = OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg())
    assert params["max_completion_tokens"] == 4096 + 512
    assert raised == {
        "param": "max_completion_tokens",
        "from": 1024,
        "to": 4608,
        "effort": "medium",
        "reason": "reasoning_headroom",
    }


@pytest.mark.parametrize("effort,expected", [("low", 1024 + 512), ("high", 16384 + 512)])
def test_allowance_follows_the_effort_tier(effort, expected):
    params = {"max_completion_tokens": 256, "reasoning_effort": effort}
    OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg())
    assert params["max_completion_tokens"] == expected


def test_absent_effort_assumes_the_configured_tier_not_the_cheapest():
    """An absent reasoning_effort means the MODEL's default, which is the middle tier.
    Assuming `low` would under-provision exactly the unlabelled requests."""
    params = {"max_completion_tokens": 100}
    OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg())
    assert params["max_completion_tokens"] == 4096 + 512


def test_off_is_provisioned_at_the_smallest_allowance_not_the_default():
    """`off` cannot actually disable reasoning on this family (can_disable_reasoning is
    False — the repo records it as `off_unsupported`), so the request still needs room.
    But it asked for as little as possible, so it gets the smallest allowance: assuming
    the default tier would inflate the budget of the one caller who asked the opposite.
    Pinned because `off` is stripped at the seam before the adapter sees params."""
    from providers import REASONING_OFF

    params = {"max_tokens": 4096, "top_p": 0.9}
    assert OpenAIAdapter().reserve_reasoning_headroom(
        params, REASONING_MODEL, _cfg(), REASONING_OFF
    ) is None
    assert params == {"max_tokens": 4096, "top_p": 0.9}

    # ...but a genuinely starved `off` request is still protected.
    params = {"max_tokens": 200}
    OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg(), REASONING_OFF)
    assert params["max_tokens"] == 1024 + 512


def test_code_defaults_match_the_template_defaults():
    """Gate 2: an empty config must behave exactly like the shipped template, or an
    operator who never wrote the block gets silently different provisioning."""
    empty = {"groups": {}, "providers": []}
    for effort, expected in (("off", 1536), ("low", 1536), ("medium", 4608), ("high", 16896)):
        params = {"max_completion_tokens": 8}
        OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, empty, effort)
        assert params["max_completion_tokens"] == expected, effort


def test_benchmark_constants_are_pinned_to_the_proxy_reasoning_headroom_policy():
    """Tracker 23.48 — examples/benchmark/run_ab.py hand-mirrors this adapter's
    reasoning-headroom policy in three hardcoded module constants
    (`_REASONING_MODEL_FRAGMENTS`, `_REASONING_ANSWER_FLOOR`, `_REASONING_ALLOWANCE`) so
    its DIRECT arm stays budget-matched with the PROXY arm — a comment says to keep them
    in step, but nothing enforced it, unlike the adapter/template pair this file already
    pins via `test_code_defaults_match_the_template_defaults`. If an operator (or a
    future code change) retunes `REASONING_MODEL_FAMILIES` or the default allowance/floor
    without updating the benchmark, the two arms silently desync and the published A/B
    number measures a budget difference instead of the proxy's actual effect.

    Imports the benchmark module directly (it is pure Python with no network calls at
    import time — the CLI/HTTP logic sits behind `if __name__ == "__main__":`) rather
    than re-typing its constants here, so a value can drift on only one side, never both
    at once by construction."""
    import sys as _sys
    from pathlib import Path as _Path

    bench_dir = _Path(__file__).resolve().parents[3] / "examples" / "benchmark"
    _sys.path.insert(0, str(bench_dir))
    import run_ab  # noqa: E402 — sys.path must be set up first

    # Same model families the adapter treats as reasoning-capable (REASONING_MODEL_FAMILIES
    # is itself pinned against config.yaml.template's model_prefixes coverage by
    # tests/unit/test_provider_model_prefix_coverage.py, so this transitively follows).
    assert run_ab._REASONING_MODEL_FRAGMENTS == OpenAIAdapter.REASONING_MODEL_FAMILIES

    # The benchmark hardcodes floor + allowance as two separate constants and sums them
    # in `reasoning_headroom_budget`; the adapter derives that same sum from config via
    # `reasoning_headroom_needed`. An EMPTY config exercises the CODE default — the exact
    # contract `test_code_defaults_match_the_template_defaults` above already pins — which
    # is what the benchmark's hardcoded numbers must mirror, since it ships no config of
    # its own.
    code_default = OpenAIAdapter().reasoning_headroom_needed(
        REASONING_MODEL, config={"groups": {}, "providers": []}, effort="medium",
    )
    assert code_default == run_ab._REASONING_ANSWER_FLOOR + run_ab._REASONING_ALLOWANCE


def test_sufficient_budget_is_left_alone():
    params = {"max_completion_tokens": 50_000, "reasoning_effort": "medium"}
    assert OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg()) is None
    assert params["max_completion_tokens"] == 50_000


def test_no_caller_budget_is_never_given_one():
    """Adding a budget where the caller set none would CAP a request that was uncapped —
    the opposite of the defect, and a quality regression of its own."""
    params = {"reasoning_effort": "high"}
    assert OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg()) is None
    assert "max_completion_tokens" not in params and "max_tokens" not in params


def test_non_reasoning_model_is_untouched():
    params = {"max_tokens": 64}
    assert OpenAIAdapter().reserve_reasoning_headroom(params, PLAIN_MODEL, _cfg()) is None
    assert params == {"max_tokens": 64}


def test_legacy_max_tokens_key_is_honoured_on_a_reasoning_model():
    params = {"max_tokens": 128, "reasoning_effort": "low"}
    raised = OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg())
    assert raised["param"] == "max_tokens"
    assert params["max_tokens"] == 1024 + 512


def test_operator_can_disable_the_reservation():
    params = {"max_completion_tokens": 16}
    assert OpenAIAdapter().reserve_reasoning_headroom(
        params, REASONING_MODEL, _cfg(enabled=False)
    ) is None
    assert params["max_completion_tokens"] == 16


def test_cap_is_never_exceeded_and_a_cap_below_need_changes_nothing():
    """A ceiling under what the effort needs must leave the caller's budget alone rather
    than raise it to a number that still cannot work — and must not claim a raise."""
    params = {"max_completion_tokens": 2048, "reasoning_effort": "high"}
    assert OpenAIAdapter().reserve_reasoning_headroom(
        params, REASONING_MODEL, _cfg(max_output_tokens=1000)
    ) is None
    assert params["max_completion_tokens"] == 2048

    params = {"max_completion_tokens": 100, "reasoning_effort": "high"}
    OpenAIAdapter().reserve_reasoning_headroom(
        params, REASONING_MODEL, _cfg(max_output_tokens=5000)
    )
    assert params["max_completion_tokens"] == 5000


def test_per_model_ceiling_wins_over_the_flat_one():
    params = {"max_completion_tokens": 10, "reasoning_effort": "high"}
    OpenAIAdapter().reserve_reasoning_headroom(
        params, REASONING_MODEL,
        _cfg(max_output_tokens=1_000, max_output_tokens_by_model={"o4-mini": 20_000}),
    )
    assert params["max_completion_tokens"] == 16384 + 512


def test_garbage_budget_does_not_raise():
    params = {"max_completion_tokens": "lots"}
    assert OpenAIAdapter().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg()) is None


def test_base_adapter_is_a_no_op():
    """Providers that bill thinking OUTSIDE the output budget must not be touched."""

    class _Plain(ProviderAdapter):
        @property
        def name(self):
            return "plain"

        def map_reasoning_effort(self, tier, config):
            return {}

        def map_structured_output(self, format_type, schema=None):
            return {}

    params = {"max_tokens": 8}
    assert _Plain().reserve_reasoning_headroom(params, REASONING_MODEL, _cfg()) is None
    assert params == {"max_tokens": 8}


# ── the seam: every call path shares it (primary, failover, cascade tier) ──────


def test_seam_applies_the_reservation_and_records_it_for_disclosure():
    ctx = _Ctx({"max_completion_tokens": 1024, "reasoning_effort": "medium"}, _cfg())
    out = outgoing_params_for(ctx, OpenAIAdapter(), REASONING_MODEL, ctx.config, "rq-1")
    assert out["max_completion_tokens"] == 4608
    assert ctx.output_budget_raised["from"] == 1024
    assert ctx.output_budget_raised["to"] == 4608
    # The caller's own params are NOT mutated — only the outgoing copy.
    assert ctx.params["max_completion_tokens"] == 1024


def test_seam_is_byte_identical_for_a_non_reasoning_model():
    ctx = _Ctx({"max_tokens": 64, "temperature": 0}, _cfg())
    out = outgoing_params_for(ctx, OpenAIAdapter(), PLAIN_MODEL, ctx.config, "rq-2")
    assert out == {"max_tokens": 64, "temperature": 0}
    assert ctx.output_budget_raised is None


def test_seam_survives_an_adapter_that_raises():
    """A budget hint must never be able to fail a request."""

    class _Exploding(OpenAIAdapter):
        def reserve_reasoning_headroom(self, params, model, config):
            raise RuntimeError("boom")

    ctx = _Ctx({"max_completion_tokens": 1024}, _cfg())
    out = outgoing_params_for(ctx, _Exploding(), REASONING_MODEL, ctx.config, "rq-3")
    assert out["max_completion_tokens"] == 1024
    assert ctx.output_budget_raised is None
