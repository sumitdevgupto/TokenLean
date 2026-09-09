"""G11 — containment of a cap G11 itself applied (2026-09-09).

The 2026-09-08 repair stopped G11 capping from a bucket the caller never identified. This
file covers what that repair left behind: an answer G11 cut can still be CACHED and replayed
to a request that can never be capped; the sticky floor could fail open on a read error and
could be lowered by a concurrent raise; a streamed cap can never be observed or corrected;
and evidence was written for a reader that is switched off.

Every test here fails against the code as it stood before this file was written.
"""
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from middleware.g11_output_format import (
    G11OutputFormat,
    _FLOOR_CAS_LUA,
    _FLOOR_UNREADABLE,
    _get_sticky_floor,
    _raise_sticky_floor,
)

pytestmark = pytest.mark.asyncio


def _g11_cfg(ctx):
    return ctx.config["groups"]["G11_output"]


def _answer(finish_reason, completion_tokens=300, content="text"):
    return {
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"completion_tokens": completion_tokens},
    }


class TestTruncatedAnswersNeverEnterTheCache:
    """G05 refuses only EMPTY answers, and its key carries neither `max_tokens` nor the
    workflow bucket. So a 300-token answer G11 cut, stored under prompt P, is served for
    the L2 TTL (24h) to a later request with the same prompt and NO workflow_id — which by
    design is never capped. That user gets a mid-sentence answer nobody asked the provider
    for, and G11 cannot correct it: the cache short-circuit returns without running the
    response pipeline, so nothing is recorded and no floor rises."""

    async def test_a_cap_we_applied_that_truncated_marks_the_answer_uncacheable(self, make_ctx):
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["max_tokens"] = 300
        ctx.params["_g11_max_tokens_set"] = True

        with patch("middleware.g11_output_format._get_redis", return_value=AsyncMock()):
            await G11OutputFormat().process_response(ctx, _answer("length"))

        assert getattr(ctx, "no_cache", False) is True

    async def test_a_completed_answer_stays_cacheable(self, make_ctx):
        """The flag must be narrow: only an answer WE cut is withheld from the cache."""
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["max_tokens"] = 300
        ctx.params["_g11_max_tokens_set"] = True

        with patch("middleware.g11_output_format._get_redis", return_value=AsyncMock()):
            await G11OutputFormat().process_response(ctx, _answer("stop"))

        assert getattr(ctx, "no_cache", False) is False

    async def test_a_caller_set_cap_that_truncated_is_not_ours_to_withhold(self, make_ctx):
        """The caller chose the cap and knows the answer is capped; caching it is the
        caller's own contract. Only G11-set caps set the flag."""
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["max_tokens"] = 300   # no _g11_max_tokens_set marker

        with patch("middleware.g11_output_format._get_redis", return_value=AsyncMock()):
            await G11OutputFormat().process_response(ctx, _answer("length"))

        assert getattr(ctx, "no_cache", False) is False

    async def test_the_flag_is_set_even_with_the_feedback_loop_off(self, make_ctx):
        """`fallback_max_tokens` can truncate with the loop off, so the cache guard must
        run BEFORE the feedback-loop early return — a truncated answer is poison to the
        cache whether or not we are learning from it."""
        ctx = make_ctx()
        _g11_cfg(ctx)["max_tokens_feedback_loop"] = False
        _g11_cfg(ctx)["max_tokens_auto_tighten"] = False
        ctx.params["max_tokens"] = 128
        ctx.params["_g11_max_tokens_set"] = True

        with patch("middleware.g11_output_format._get_redis", return_value=AsyncMock()):
            await G11OutputFormat().process_response(ctx, _answer("length"))

        assert getattr(ctx, "no_cache", False) is True


class TestTheStickyFloorFailsClosed:
    """The floor exists because the percentile cap has already been proven to truncate
    THIS bucket. A read failure that silently becomes "no floor" reinstates exactly that
    cap — fail-open in the one direction that cuts a customer's answer."""

    async def test_a_read_error_is_distinguishable_from_an_absent_floor(self):
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=ConnectionError("Memorystore unreachable"))
        assert await _get_sticky_floor(redis, "k") is _FLOOR_UNREADABLE

        redis.get = AsyncMock(return_value=None)
        assert await _get_sticky_floor(redis, "k") is None

    async def test_an_unparseable_stored_value_is_treated_as_absent_not_as_a_failure(self):
        """Garbage in the key is not a read failure: there is no floor to honour and the
        bucket's next truncation rewrites it."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"not-a-number")
        assert await _get_sticky_floor(redis, "k") is None

    async def test_g11_declines_to_cap_when_the_floor_cannot_be_read(self, make_ctx):
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        redis = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[
            json.dumps({"max_tokens": 1000, "completion_tokens": 800})] * 10)
        redis.get = AsyncMock(side_effect=ConnectionError("Memorystore unreachable"))

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            await G11OutputFormat().process_request(ctx)

        assert "max_tokens" not in ctx.params, (
            "the percentile alone has already been proven to truncate this bucket; "
            "without the floor G11 must decline"
        )

    async def test_a_readable_floor_still_caps(self, make_ctx):
        """The negative control: fail-closed must not become never-cap."""
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        redis = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[
            json.dumps({"max_tokens": 1000, "completion_tokens": 800})] * 10)
        redis.get = AsyncMock(return_value=None)

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 1600   # p95 800 x the 2.0 default


class TestTheFloorRaiseIsAtomic:
    """Write-side monotonicity — the invariant the docstring asserted and nothing tested.
    Two workers read 100; one raises to 464, the other to 150; last write wins and the
    floor is LOWERED. Reachable on `max_instances > 1` and on multiple uvicorn workers."""

    async def test_the_raise_is_one_round_trip_with_no_read_modify_write_window(self):
        redis = AsyncMock()
        await _raise_sticky_floor(redis, "floor:k", 464, 604800)

        redis.eval.assert_called_once()
        args = redis.eval.call_args.args
        assert args[0] is _FLOOR_CAS_LUA
        assert args[1] == 1 and args[2] == "floor:k" and args[3] == 464
        redis.get.assert_not_called()
        redis.set.assert_not_called()

    async def test_the_script_compares_before_it_writes(self):
        """A script that just SET the value would be atomic and still wrong."""
        assert "GET" in _FLOOR_CAS_LUA
        assert ">=" in _FLOOR_CAS_LUA
        assert "SET" in _FLOOR_CAS_LUA
        assert "EXPIRE" in _FLOOR_CAS_LUA

    async def test_the_fallback_refuses_to_write_when_it_cannot_read_the_current_floor(self):
        """If EVAL is unavailable AND the current value is unreadable, writing blindly
        could lower a higher floor. Leaving it costs one slower convergence."""
        redis = AsyncMock()
        redis.eval = AsyncMock(side_effect=Exception("EVAL not supported"))
        redis.get = AsyncMock(side_effect=ConnectionError("down"))

        await _raise_sticky_floor(redis, "floor:k", 464, 604800)

        redis.set.assert_not_called()

    async def test_the_fallback_still_refreshes_a_higher_existing_floor_without_lowering_it(self):
        redis = AsyncMock()
        redis.eval = AsyncMock(side_effect=Exception("EVAL not supported"))
        redis.get = AsyncMock(return_value=b"900")

        await _raise_sticky_floor(redis, "floor:k", 464, 604800)

        redis.set.assert_not_called()
        redis.expire.assert_called_once_with("floor:k", 604800)


class TestStreamedRequestsAreNotCapped:
    """A streamed response never reaches `process_response`, so a cap applied to it can
    never be observed: no truncation is recorded, no floor rises, and non-streaming
    answers keep feeding the same bucket's p95 — the stream is cut on every request,
    permanently, with nothing able to correct it."""

    @pytest.mark.parametrize("stream_value", [True, "true", "True", 1])
    async def test_a_streamed_request_gets_no_learned_cap(self, make_ctx, stream_value):
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["stream"] = stream_value
        redis = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[
            json.dumps({"max_tokens": 1000, "completion_tokens": 800})] * 10)
        redis.get = AsyncMock(return_value=None)

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            await G11OutputFormat().process_request(ctx)

        assert "max_tokens" not in ctx.params

    async def test_a_non_streamed_request_with_the_same_evidence_is_capped(self, make_ctx):
        """Negative control: the skip is about the stream, not about the evidence."""
        ctx = make_ctx()
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["stream"] = False
        redis = AsyncMock()
        redis.zrevrange = AsyncMock(return_value=[
            json.dumps({"max_tokens": 1000, "completion_tokens": 800})] * 10)
        redis.get = AsyncMock(return_value=None)

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 1600


class TestEvidenceIsOnlyWrittenForAReaderThatIsOn:
    """`workflow_id` is caller-supplied, so a client stamping a unique one per request
    makes one ZSET + EXPIRE + trim per request on shared Memorystore, held 7 days. With
    the cap off — the shipped default — nothing ever reads any of it."""

    async def test_nothing_is_recorded_while_the_cap_is_off(self, make_ctx):
        ctx = make_ctx()
        _g11_cfg(ctx)["max_tokens_auto_tighten"] = False
        _g11_cfg(ctx)["max_tokens_feedback_loop"] = True
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["max_tokens"] = 500
        redis = AsyncMock()

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            await G11OutputFormat().process_response(ctx, _answer("stop"))

        redis.zadd.assert_not_called()

    async def test_recording_resumes_when_an_operator_opts_in(self, make_ctx):
        ctx = make_ctx()
        _g11_cfg(ctx)["max_tokens_auto_tighten"] = True
        ctx.params["workflow_id"] = "wf-support"
        ctx.params["max_tokens"] = 500
        redis = AsyncMock()

        with patch("middleware.g11_output_format._get_redis", return_value=redis):
            await G11OutputFormat().process_response(ctx, _answer("stop"))

        redis.zadd.assert_called_once()


class TestTheFallbackCapIsObservable:
    """`fallback_max_tokens` is the one cap with no escalation path: static, applied to
    catch-all traffic too, and its truncations raise no floor — the same request is cut
    identically forever. Nothing said so."""

    async def test_a_truncation_caused_by_the_fallback_cap_warns(self, make_ctx, caplog):
        ctx = make_ctx()
        _g11_cfg(ctx)["max_tokens_feedback_loop"] = False
        ctx.params["max_tokens"] = 128
        ctx.params["_g11_max_tokens_set"] = True
        ctx.params["_g11_fallback_cap"] = True

        with caplog.at_level(logging.WARNING, logger="middleware.g11_output_format"):
            with patch("middleware.g11_output_format._get_redis", return_value=AsyncMock()):
                await G11OutputFormat().process_response(ctx, _answer("length"))

        assert any("fallback_max_tokens" in r.getMessage() for r in caplog.records)

    async def test_the_fallback_cap_marks_itself_so_the_warning_can_name_it(self, make_ctx):
        ctx = make_ctx()
        _g11_cfg(ctx)["max_tokens_auto_tighten"] = False
        _g11_cfg(ctx)["fallback_max_tokens"] = 256

        with patch("middleware.g11_output_format._get_redis", return_value=AsyncMock()):
            await G11OutputFormat().process_request(ctx)

        assert ctx.params["max_tokens"] == 256
        assert ctx.params["_g11_fallback_cap"] is True
