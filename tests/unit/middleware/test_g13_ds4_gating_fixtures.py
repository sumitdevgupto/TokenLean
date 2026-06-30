"""Structural validation of the G13 TOON gating fixtures (no LLM calls).

Exercises `tests/data/g13_ds4_gating_fixtures.jsonl` against the real G13
middleware so the auto-detect / nested-fallback / net-savings paths are proven
without the live ablation stack. The same fixtures (kept in sync under the internal
`pitch-test-plan/datasets/DS4/`) feed the DS4 ablation when the Docker stack +
OpenAI key are available (with `toon_auto_detect: true`). The shipping copy lives
under `tests/data/` so this core G13 test has no dependency on the internal,
gitignored pitch-test-plan tree.
"""
import json
import os

import pytest

_FIXTURES = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "data", "g13_ds4_gating_fixtures.jsonl",
)


def _load():
    with open(_FIXTURES, "r", encoding="ascii") as f:
        return {json.loads(line)["request_id"]: json.loads(line) for line in f if line.strip()}


FIXTURES = _load()
USER = 1  # messages[1] is the user payload


def _ctx(make_ctx, fx, auto_detect):
    ctx = make_ctx(messages=[dict(m) for m in fx["messages"]])
    ctx.config["groups"]["G13_batch"]["toon_auto_detect"] = auto_detect
    return ctx


def test_fixtures_present():
    assert set(FIXTURES) == {
        "ds4-gate-auto-uniform", "ds4-gate-auto-large",
        "ds4-gate-nested", "ds4-gate-nonuniform", "ds4-gate-marker-present",
    }


@pytest.mark.asyncio
class TestG13GatingFixtures:
    async def _run(self, make_ctx, rid, auto_detect):
        from middleware.g13_batch import G13Batch
        ctx = _ctx(make_ctx, FIXTURES[rid], auto_detect)
        before = ctx.current_token_count
        ctx = await G13Batch().process_request(ctx)
        return ctx, before

    @pytest.mark.parametrize("rid", ["ds4-gate-auto-uniform", "ds4-gate-auto-large"])
    async def test_auto_detect_compresses_unmarked_tabular(self, make_ctx, rid):
        # No `schema:` marker → only fires with auto-detect on. The large one
        # (>2000 chars) also proves the lifted toon_max_block_chars ceiling.
        ctx, before = await self._run(make_ctx, rid, auto_detect=True)
        assert "schema:order_id|customer|amount|status|region" in ctx.messages[USER]["content"]
        assert ctx.current_token_count <= before

    @pytest.mark.parametrize("rid", ["ds4-gate-auto-uniform", "ds4-gate-auto-large"])
    async def test_auto_detect_off_leaves_unmarked_untouched(self, make_ctx, rid):
        original = FIXTURES[rid]["messages"][USER]["content"]
        ctx, _ = await self._run(make_ctx, rid, auto_detect=False)
        assert ctx.messages[USER]["content"] == original

    @pytest.mark.parametrize("rid", ["ds4-gate-nested", "ds4-gate-nonuniform"])
    async def test_ineligible_falls_back_to_json(self, make_ctx, rid):
        # Nested values / divergent key sets → JSON fallback, even with auto-detect on.
        original = FIXTURES[rid]["messages"][USER]["content"]
        ctx, before = await self._run(make_ctx, rid, auto_detect=True)
        assert ctx.messages[USER]["content"] == original
        assert ctx.current_token_count <= before  # never inflates

    async def test_marker_present_fires_without_auto_detect(self, make_ctx):
        # Back-compat: the legacy `schema:` marker path still triggers with auto-detect off.
        ctx, before = await self._run(make_ctx, "ds4-gate-marker-present", auto_detect=False)
        assert "schema:order_id|customer|amount|status|region" in ctx.messages[USER]["content"]
        assert ctx.current_token_count <= before

    @pytest.mark.parametrize("rid", list(FIXTURES))
    async def test_never_inflates(self, make_ctx, rid):
        # The net-savings guard guarantees G13 can only reduce or keep token count.
        ctx, before = await self._run(make_ctx, rid, auto_detect=True)
        assert ctx.current_token_count <= before
