"""G06 must publish the complexity tier it decided, on every path that decides one.

`ctx.complexity_tier` is what G25 reuses instead of running a second, differently tuned
keyword classifier over the same text (backlog #42). It is deliberately left `None` when
no classifier ran — a caller `x_complexity` override, a routing rule, or G06 disabled —
because those express ROUTING intent and treating them as a reasoning decision would mean
`x_complexity: simple` silently disabled extended thinking.

The cascade-execution branch is the trap: it DOES classify, into a local `request_tier`
used for the escalation cap, and until 2026-09-05 it never assigned `complexity`. The
shipped config has `cascade_execution: false`, so the default path was unaffected and
every test passed — but a tenant turning that flag on would have silently lost the
behaviour, with nothing reporting it had gone. That is the same "green in tests, inert in
production" shape this feature has already hit twice, so it is pinned here.
"""
import pytest

from middleware.g06_routing import G06Routing

_SIMPLE = "What are the API call limits for the Scale tier?"
_TIERS = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4o"]}


def _cfg(minimal_config, **over):
    g6 = {"enabled": True, "classifier": "heuristic", "tiers": _TIERS}
    g6.update(over)
    minimal_config["groups"]["G6_routing"] = g6
    return minimal_config


async def _tier(make_ctx, minimal_config, text=_SIMPLE, **over):
    ctx = make_ctx([{"role": "user", "content": text}], model="gpt-4o",
                   config=_cfg(minimal_config, **over))
    out = await G06Routing().process_request(ctx)
    return out.complexity_tier


@pytest.mark.asyncio
class TestEveryClassifyingPathPublishesIt:
    async def test_the_heuristic_path_publishes(self, make_ctx, minimal_config):
        assert await _tier(make_ctx, minimal_config) == "simple"

    async def test_the_cascade_execution_path_publishes(self, make_ctx, minimal_config):
        """The regression. This branch computes `request_tier` for its escalation cap and
        used to discard it, so G25's reuse went quietly dead whenever an operator enabled
        cascade execution."""
        tier = await _tier(make_ctx, minimal_config,
                           classifier="cascade", cascade_execution=True)
        assert tier == "simple", (
            "cascade execution classified the request for its escalation cap and then "
            "threw the answer away — G25 would fall back to its own classifier and the "
            "routing decision would be made twice, differently"
        )

    async def test_a_complex_request_publishes_complex(self, make_ctx, minimal_config):
        """Guards against the tier being hardcoded rather than actually classified."""
        assert await _tier(
            make_ctx, minimal_config,
            "Design an algorithm and analyse its time complexity in detail.",
            classifier="cascade", cascade_execution=True) == "complex"


@pytest.mark.asyncio
class TestPathsThatDecideNothingPublishNothing:
    async def test_a_caller_override_does_not_become_a_reasoning_decision(
            self, make_ctx, minimal_config):
        """`x_complexity` is the caller steering the ROUTE. Letting it also switch off
        extended thinking would be a coupling no caller asked for."""
        ctx = make_ctx([{"role": "user", "content": _SIMPLE}], model="gpt-4o",
                       config=_cfg(minimal_config))
        ctx.params["x_complexity"] = "simple"
        out = await G06Routing().process_request(ctx)
        assert out.complexity_tier is None

    async def test_g06_disabled_publishes_nothing(self, make_ctx, minimal_config):
        assert await _tier(make_ctx, minimal_config, enabled=False) is None
