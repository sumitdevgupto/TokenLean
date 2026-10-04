"""A response stage that fails no longer turns a paid answer into an unrecorded 500.

The response stages run after the provider has answered and billed. They ran with no error
handling, so one that raised ended the request in a 500, and no usage row, counters or security
audit row were written. The stages that make an answer safe to serve (G29 masking, G30
guardrails, G32 tool eligibility) still end the request when they fail: the answer must not go
out unmasked or unchecked. Every other response stage is skipped when it fails: the error is
logged and counted, and the answer goes on with whatever edits that stage had completed.
"""
import logging
import pathlib
import sys
from unittest.mock import AsyncMock, patch

import pytest

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

import middleware.g18_observability as g18  # noqa: E402
from middleware.pipeline import OptimisationPipeline  # noqa: E402
from savings.calculator import estimate_cost  # noqa: E402

PROMPT, COMPLETION = 2000, 500
ANSWER = "The capital of France is Paris."

# (stage, pipeline attribute, method)
SAFETY_STAGES = [
    ("G29-pii-redaction-resp", "g29", "process_response"),
    ("G30-guardrails-resp", "g30", "process_response"),
    ("G32-tool-eligibility", "g32", "process_response"),
]
OPTIONAL_STAGES = [
    ("G14-tool-output", "g14", "process_response"),
    ("G28-ccr-resp", "g28", "process_response"),
    ("G23-streaming-compression", "g23", "process_response"),
    ("G19-headroom-resp", "g19", "process_response"),
    ("G15-server-compute", "g15", "process_response"),
    ("G11-output-format-resp", "g11", "process_response"),
    ("G18-observability", "g18", "record"),
    ("G05-store-response", "g05", "store_response"),
]
CONFIG = {"groups": {"G18_observability": {"enabled": True, "prometheus_enabled": False,
                                           "turn_efficiency_enabled": False,
                                           "tool_governance_enabled": False}}}


def _provider_response():
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": PROMPT, "completion_tokens": COMPLETION,
                      "total_tokens": PROMPT + COMPLETION}}


def _errors(stage):
    return g18.RESPONSE_STAGE_ERRORS.labels(stage=stage)._value.get()


def _fail(pipeline, attr, method):
    setattr(getattr(pipeline, attr), method, AsyncMock(side_effect=RuntimeError("stage bug")))


@pytest.fixture(autouse=True)
def _no_trace():
    with patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock):
        yield


@pytest.fixture
def pipeline():
    pipe = OptimisationPipeline()
    pipe.g05.store_response = AsyncMock()   # records that the cache store was reached
    return pipe


@pytest.fixture
def ctx(make_ctx):
    return make_ctx([{"role": "user", "content": "What is the capital of France?"}],
                    model="gpt-4o", config=CONFIG)


class TestAnOptimisationStageThatFailsIsSkipped:

    @pytest.mark.parametrize("stage, attr, method", OPTIONAL_STAGES,
                             ids=[s[0] for s in OPTIONAL_STAGES])
    async def test_the_answer_is_served_priced_and_the_error_counted(
            self, pipeline, ctx, caplog, stage, attr, method):
        _fail(pipeline, attr, method)
        before = _errors(stage)
        with caplog.at_level(logging.WARNING, logger="middleware.pipeline"):
            try:
                ctx, served = await pipeline.process_response(ctx, _provider_response())
            except RuntimeError as exc:
                pytest.fail(f"{stage}'s error reached the caller: {exc}")
        assert served["choices"][0]["message"]["content"] == ANSWER
        assert _errors(stage) == before + 1
        assert any(stage in r.getMessage() and "stage bug" in r.getMessage()
                   for r in caplog.records), caplog.text
        # Priced at what the provider billed, by G18 or, when G18 is the stage that
        # failed, by the pipeline in its place.
        assert ctx.savings.cost_actual_usd == pytest.approx(
            estimate_cost(PROMPT, COMPLETION, "gpt-4o"))
        assert ctx.savings.cost_actual_usd > 0
        if stage != "G05-store-response":
            pipeline.g05.store_response.assert_awaited_once()   # the stages after it ran

    async def test_the_edits_a_stage_completed_before_it_failed_are_kept(self, pipeline, ctx):
        async def edit_then_fail(ctx, response):
            response["choices"][0]["message"]["content"] = "Paris."
            raise RuntimeError("stage bug")

        pipeline.g19.process_response = edit_then_fail
        try:
            ctx, served = await pipeline.process_response(ctx, _provider_response())
        except RuntimeError as exc:
            pytest.fail(f"G19's error reached the caller: {exc}")
        assert served["choices"][0]["message"]["content"] == "Paris."


class TestASafetyStageThatFailsEndsTheRequest:

    @pytest.mark.parametrize("stage, attr, method", SAFETY_STAGES,
                             ids=[s[0] for s in SAFETY_STAGES])
    async def test_no_later_stage_sees_the_answer(self, pipeline, ctx, caplog, stage, attr, method):
        _fail(pipeline, attr, method)
        pipeline.g15.process_response = AsyncMock(side_effect=lambda ctx, response: response)
        pipeline.g18.record = AsyncMock()
        before = _errors(stage)
        with caplog.at_level(logging.WARNING, logger="middleware.pipeline"):
            with pytest.raises(RuntimeError, match="stage bug"):
                await pipeline.process_response(ctx, _provider_response())
        pipeline.g15.process_response.assert_not_awaited()   # executes no unchecked tool call
        pipeline.g18.record.assert_not_awaited()
        pipeline.g05.store_response.assert_not_awaited()     # caches no unchecked answer
        assert _errors(stage) == before + 1
        assert any(stage in r.getMessage() for r in caplog.records), caplog.text
