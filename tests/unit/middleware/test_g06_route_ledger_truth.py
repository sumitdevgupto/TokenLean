"""G06 ledger truth — the recorded route must be the route that actually happened.

Guards the defect found on 2026-09-08 (tracker 23.25 / leader decision D-073): G06 wrote
its savings step at PLAN time, so when the deferred cascade failed and main.py reverted
``routed_model`` to the caller's model, the step for a route that never happened stayed on
the ledger — 54 of 54 rows in each of two consecutive mints, flowing into ``_token_opt``,
``usage_events.group_savings``, the portal and the learning loop.

These tests pin the four properties that make that impossible to reintroduce:
  1. the step is written from ``savings.routed_model`` on the RESPONSE, never at plan time;
  2. every site that changes which model serves also updates ``savings.routed_model``;
  3. the step text carries no second, input-only dollar figure beside G18's cost fields;
  4. the cost the proxy reports covers EVERY provider call it paid for, not just the last.
"""
import os
import sys

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy"))
)

from middleware.g06_routing import G06Routing, record_routing_step  # noqa: E402


def _g06_steps(ctx):
    return [s for s in ctx.savings.step_savings if s.group == "G06"]


class TestStepFollowsTheServedModel:
    def test_no_step_when_the_route_was_reverted(self, make_ctx):
        """The F1 shape: G06 planned a cheap tier, the cascade errored, main.py put the
        caller's model back. Nothing was routed, so nothing is credited."""
        ctx = make_ctx(model="o4-mini")
        ctx.savings.model_requested = "o4-mini"
        # G06 plans gpt-4o-mini...
        ctx.routed_model = "gpt-4o-mini"
        ctx.savings.routed_model = "gpt-4o-mini"
        ctx.routing_detail = "classifier=cascade, complexity=simple"
        # ...main.py's cascade-error branch reverts both.
        ctx.routed_model = "o4-mini"
        ctx.savings.routed_model = "o4-mini"
        ctx.savings.routing_mode = "cascade_planned+exec_error"

        record_routing_step(ctx)
        assert _g06_steps(ctx) == []

    def test_step_names_the_model_that_answered(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.savings.routed_model = "gpt-4o-mini"
        ctx.routing_detail = "classifier=cascade, complexity=simple"

        record_routing_step(ctx)
        steps = _g06_steps(ctx)
        assert len(steps) == 1
        assert "gpt-4o → gpt-4o-mini" in steps[0].description
        assert "classifier=cascade" in steps[0].description

    def test_step_escalated_by_the_cascade_names_the_final_tier(self, make_ctx):
        """A cascade that escalates settles on a PRICIER model than G06 planned. The step
        must name what served, not the tier-1 probe the plan picked."""
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.routing_detail = "classifier=cascade, complexity=simple"
        ctx.savings.routed_model = "gpt-4o-mini"   # plan
        ctx.savings.routed_model = "gpt-4-5"       # what actually answered

        record_routing_step(ctx)
        assert "gpt-4-5" in _g06_steps(ctx)[0].description

    def test_no_dollar_figure_in_the_step_text(self, make_ctx):
        """Cost is disclosed once, by G18, on an input+output basis. The old input-only
        figure could be wrong in SIGN when the cheaper-per-input model is more verbose."""
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.savings.routed_model = "gpt-4o-mini"

        record_routing_step(ctx)
        assert "$" not in _g06_steps(ctx)[0].description

    def test_recording_is_idempotent(self, make_ctx):
        """G06.process_response and G18 both call it on the cascade-served path."""
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.savings.routed_model = "gpt-4o-mini"

        record_routing_step(ctx)
        record_routing_step(ctx)
        record_routing_step(ctx)
        assert len(_g06_steps(ctx)) == 1

    async def test_process_response_records_it(self, make_ctx):
        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.savings.routed_model = "gpt-4o-mini"

        out = await G06Routing().process_response(ctx, {"id": "x"})
        assert out == {"id": "x"}
        assert len(_g06_steps(ctx)) == 1

    async def test_process_request_does_not_record_it(self, make_ctx):
        """Plan time is too early — the cascade may still fail after this point."""
        ctx = make_ctx([{"role": "user", "content": "What is 2+2?"}], model="gpt-4o")
        await G06Routing().process_request(ctx)
        assert _g06_steps(ctx) == []


class TestSubstitutedModelIsDisclosed:
    """G06 disabled + an unconfigured model → the proxy serves the configured default.
    The substitution is unchanged; being silent about it is the defect."""

    async def _run_disabled(self, ctx):
        ctx.config["groups"]["G6_routing"] = {"enabled": False}
        return await G06Routing().process_request(ctx)

    async def test_ledger_names_the_substitute(self, make_ctx, monkeypatch):
        import middleware.g06_routing as g06

        monkeypatch.setattr(g06, "_is_configured_model", lambda m: False)
        monkeypatch.setattr(g06, "get_default_model", lambda: "gpt-4o-mini")
        ctx = make_ctx(model="o4-mini")
        ctx.savings.model_requested = "o4-mini"

        await self._run_disabled(ctx)

        assert ctx.routed_model == "gpt-4o-mini"
        # The caller's ask is preserved — overwriting it would erase the evidence.
        assert ctx.savings.model_requested == "o4-mini"
        assert ctx.savings.routed_model == "gpt-4o-mini"
        assert ctx.model_substituted == "gpt-4o-mini"

    async def test_configured_model_is_never_substituted(self, make_ctx, monkeypatch):
        import middleware.g06_routing as g06

        monkeypatch.setattr(g06, "_is_configured_model", lambda m: True)
        monkeypatch.setattr(g06, "get_default_model", lambda: "gpt-4o-mini")
        ctx = make_ctx(model="o4-mini")
        ctx.savings.model_requested = "o4-mini"

        await self._run_disabled(ctx)

        assert ctx.routed_model == "o4-mini"
        assert ctx.savings.routed_model == "o4-mini"
        assert ctx.model_substituted == ""


class TestFailoverPinsTheLedger:
    def test_pin_winner_updates_savings_routed_model(self, make_ctx):
        """G18 prices from ctx.routed_model so cost was right, but every disclosure
        surface kept naming the model that had just FAILED."""
        sys.path.insert(
            0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy"))
        )
        from main import _make_pin_winner

        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.savings.routed_model = "gpt-4o"

        class _T:
            model = "claude-sonnet-4-20250514"
            adapter = None

        _make_pin_winner(ctx, "req-1")(_T())

        assert ctx.routed_model == "claude-sonnet-4-20250514"
        assert ctx.savings.routed_model == "claude-sonnet-4-20250514"


class TestStreamsStillDiscloseTheRoute:
    """Streamed responses skip the whole response pipeline, G18 included. Moving the step
    onto the response would otherwise have made every streamed request disclose no route
    at all — the same silence, in the other direction."""

    def test_the_stream_finaliser_records_the_step(self):
        import inspect

        import main

        src = inspect.getsource(main.stream_response) if hasattr(main, "stream_response") else ""
        if not src:
            # The finaliser lives in the streaming handler; find it by the G23 hook it
            # sits beside rather than by a name that may be refactored.
            src = inspect.getsource(main)
        g23_at = src.find("_apply_stream_g23(ctx,")
        assert g23_at != -1, "streaming finaliser moved — re-point this guard"
        window = src[g23_at:g23_at + 900]
        assert "record_routing_step(ctx)" in window, (
            "the streamed path must record G06's routing step; without it a streamed "
            "request discloses no route at all"
        )


class TestEveryProviderCallIsPriced:
    """A cascade that escalates sends the prompt two or three times. Pricing only the
    last response under-stated the request's real cost, in the direction that flattered
    the savings figure."""

    async def _record(self, ctx, response):
        from middleware.g18_observability import G18Observability

        ctx.config.setdefault("groups", {})["G18_observability"] = {
            "enabled": True, "prometheus_enabled": False,
        }
        await G18Observability().record(ctx, response)

    def _resp(self, prompt=100, completion=50):
        return {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        }

    async def test_single_call_path_is_unchanged(self, make_ctx):
        from middleware import record_provider_call

        ctx = make_ctx(model="gpt-4o")
        one = self._resp()
        record_provider_call(ctx, "gpt-4o", one)
        await self._record(ctx, one)
        single = ctx.savings.cost_actual_usd

        ctx2 = make_ctx(model="gpt-4o")
        await self._record(ctx2, self._resp())

        assert ctx.savings.cost_actual_usd == pytest.approx(ctx2.savings.cost_actual_usd)
        assert single > 0
        # Not disclosed when there was only ever one call — absence means "one", not
        # "unknown".
        assert "provider_call_count" not in ctx.savings.to_langfuse_metadata()

    async def test_escalating_cascade_costs_more_than_the_served_call(self, make_ctx):
        from middleware import record_provider_call

        served = self._resp(prompt=100, completion=50)

        baseline = make_ctx(model="gpt-4o")
        record_provider_call(baseline, "gpt-4o", served)
        await self._record(baseline, served)

        ctx = make_ctx(model="gpt-4o")
        record_provider_call(ctx, "gpt-4o-mini", self._resp(prompt=100, completion=40))  # tier-1 probe
        record_provider_call(ctx, "gpt-4o", served)                                      # escalation
        await self._record(ctx, served)

        assert ctx.savings.cost_actual_usd > baseline.savings.cost_actual_usd
        meta = ctx.savings.to_langfuse_metadata()
        assert meta["provider_call_count"] == 2
        assert meta["provider_call_prompt_tokens"] == 200
        # y stays the FINAL call's prompt, so it remains comparable with past mints.
        assert ctx.savings.final_tokens_sent == 100

    async def test_g18_records_the_routing_step_even_when_disabled(self, make_ctx):
        """Turning observability off must not turn the routing ledger back into the
        plan-time claim this replaced."""
        from middleware.g18_observability import G18Observability

        ctx = make_ctx(model="gpt-4o")
        ctx.savings.model_requested = "gpt-4o"
        ctx.savings.routed_model = "gpt-4o-mini"
        ctx.config.setdefault("groups", {})["G18_observability"] = {"enabled": False}

        await G18Observability().record(ctx, self._resp())
        assert len(_g06_steps(ctx)) == 1
