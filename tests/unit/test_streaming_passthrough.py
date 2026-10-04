"""Row 14 — streaming pass-through: relay provider SSE chunks, skip the response pipeline,
capture usage from the final chunk, and bill once on completion.
"""
import types

import pytest

import main


class _Ctx:
    def __init__(self):
        from datetime import datetime, timezone
        from savings.models import SavingsRecord
        self.messages = [{"role": "user", "content": "hi"}]
        self.config = {}
        self.tenant_id = "default"
        self.request_id = "req-1"
        # Mirror RequestContext: the streaming path accumulates the stream's
        # wall-time into this (via +=), so it must exist as a number.
        self.llm_elapsed_ms = 0.0
        # Fields the resilience layer (#1) reads when establishing the stream.
        self.routed_model = "gpt-4o-mini"
        self.provider_adapter = None
        self.redis_prefix = ""
        self.provider_attempts = []
        # Fields the stream's pricing (G18's price_response) reads and writes.
        self.params = {}
        self.provider_calls = []
        self.savings = SavingsRecord(
            request_id="req-1", user_id="u", timestamp=datetime.now(timezone.utc),
            model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=40,
        )
        self.savings.proxy_optimised_tokens = 30  # y: the proxy's count of what it sent


async def _collect(streaming_response):
    chunks = []
    async for part in streaming_response.body_iterator:
        chunks.append(part if isinstance(part, str) else part.decode())
    return "".join(chunks)


@pytest.mark.asyncio
async def test_stream_relays_chunks_and_bills_once(monkeypatch):
    fake_chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]

    async def fake_acompletion(**kwargs):
        async def gen():
            for c in fake_chunks:
                yield c
        return gen()

    billed = {}

    def fake_record(ctx, start, status, response=None):
        billed["status"] = status
        billed["response"] = response

    monkeypatch.setattr(main.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(main, "_record_outcome", fake_record)

    resp = main._stream_response(_Ctx(), "gpt-4o-mini", {"api_key": "sk"}, {"stream": True}, "req-1", 0.0,
                                 provider="openai", provider_key="sk")
    body = await _collect(resp)

    # SSE framing + content + terminal [DONE]
    assert "data: " in body
    assert "Hel" in body and "lo" in body
    assert body.strip().endswith("data: [DONE]")
    # Billed exactly once, with usage captured from the final chunk
    assert billed["status"] == "200"
    assert billed["response"]["usage"]["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_stream_failure_before_first_chunk_not_billed_as_200(monkeypatch):
    """Review S1+S2: a stream that never produced a chunk records the REAL error
    status (not a billable 200), and the client sees a generic redacted message —
    never the raw upstream exception."""
    async def boom(**kwargs):
        raise RuntimeError("provider down api_key=sk-SECRET")

    recorded = {}

    def fake_record(ctx, start, status, response=None):
        recorded["status"] = status

    monkeypatch.setattr(main.litellm, "acompletion", boom)
    monkeypatch.setattr(main, "_record_outcome", fake_record)

    resp = main._stream_response(_Ctx(), "gpt-4o-mini", {}, {"stream": True}, "req-2", 0.0,
                                 provider="openai", provider_key="sk")
    body = await _collect(resp)
    assert "error" in body                    # error surfaced in the stream
    assert "SECRET" not in body               # raw exception never echoed to the client
    assert recorded["status"] == "502"        # real status recorded — NOT a billable 200


def test_apply_stream_g23_measures_without_a_savings_step():
    """Chunk-aware G23 (P9): reassembled streamed text is measured as the (skipped) response
    pipeline would measure it. The client got the whole stream, so nothing was saved: the
    repetition is counted, and no savings step is recorded."""
    from middleware.g23_streaming_compression import COMPRESSIBLE_OUTPUT_TOKENS

    class _Sav:
        def __init__(self):
            self.steps = []

        def add_step(self, group, *a, **k):
            self.steps.append(group)

    class _C:
        request_id = "r"
        tenant_id = "stream-g23-measured"

        def __init__(self):
            self.config = {"groups": {"G23_streaming_compression": {"enabled": True}}}
            self.savings = _Sav()

    c = _C()
    counter = COMPRESSIBLE_OUTPUT_TOKENS.labels(tenant_id="stream-g23-measured")
    before = counter._value.get()
    phrase = "alpha beta gamma delta epsilon "  # one 5-gram, >20 chars
    main._apply_stream_g23(c, phrase * 4 + "and a unique tail to finish")
    assert c.savings.steps == []
    assert counter._value.get() > before


def test_apply_stream_g23_noop_when_disabled():
    class _C:
        request_id = "r"
        config = {"groups": {"G23_streaming_compression": {"enabled": False}}}
        savings = None  # must not be touched when disabled
    main._apply_stream_g23(_C(), "alpha beta gamma delta epsilon " * 4)  # no exception


@pytest.mark.asyncio
async def test_stream_records_cache_tokens_and_cost(monkeypatch):
    """#34 — a STREAMED call must record provider cache tokens and an actual cost.

    The response pipeline (and therefore G18, the single source of truth for cost) is
    skipped for streamed calls, so before this a streamed request recorded no cached-read
    tokens, no cache-write tokens and cost_actual_usd = 0. That matters most for agentic
    clients, which stream nearly everything and are the heaviest users of prompt caching —
    precisely the traffic whose cache bill was invisible.
    """
    from datetime import datetime
    from savings.models import SavingsRecord
    from providers.openai_adapter import OpenAIAdapter

    fake_chunks = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}}], "usage": {
            "prompt_tokens": 1000, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 200},
        }},
    ]

    async def fake_acompletion(**kwargs):
        async def gen():
            for c in fake_chunks:
                yield c
        return gen()

    ctx = _Ctx()
    ctx.savings = SavingsRecord(
        request_id="req-cache", user_id="u", timestamp=datetime.now(),
        model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=1200,
    )
    ctx.provider_adapter = OpenAIAdapter()
    ctx.params = {}

    monkeypatch.setattr(main.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(main, "_record_outcome", lambda *a, **k: None)

    resp = main._stream_response(ctx, "gpt-4o-mini", {"api_key": "sk"}, {"stream": True},
                                 "req-cache", 0.0, provider="openai", provider_key="sk")
    await _collect(resp)

    assert ctx.savings.cache_read_tokens == 600
    assert ctx.savings.cache_write_tokens == 200
    assert ctx.savings.cost_actual_usd > 0, "a streamed call must record a real cost"
    assert ctx.savings.cost_cache_write_usd is not None


@pytest.mark.asyncio
async def test_stream_cache_accounting_never_breaks_the_stream(monkeypatch):
    """Accounting is best-effort: a broken adapter must not cost the caller their stream."""
    from datetime import datetime
    from savings.models import SavingsRecord

    class Exploding:
        name = "boom"
        def extract_usage(self, response):
            raise RuntimeError("adapter blew up")

    fake_chunks = [
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ]

    async def fake_acompletion(**kwargs):
        async def gen():
            for c in fake_chunks:
                yield c
        return gen()

    ctx = _Ctx()
    ctx.savings = SavingsRecord(
        request_id="req-boom", user_id="u", timestamp=datetime.now(),
        model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=10,
    )
    ctx.provider_adapter = Exploding()
    ctx.params = {}

    monkeypatch.setattr(main.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(main, "_record_outcome", lambda *a, **k: None)

    resp = main._stream_response(ctx, "gpt-4o-mini", {}, {"stream": True}, "req-boom", 0.0,
                                 provider="openai", provider_key="sk")
    body = await _collect(resp)
    assert body.strip().endswith("data: [DONE]")
    assert ctx.savings.cache_write_tokens is None


# ── Streamed calls are priced like any other ─────────────────────────────────────
# A stream was priced by a separate copy of G18's code that needed the provider's usage
# chunk: a client's own include_usage=false (or a disconnect before that chunk) left the
# call at $0, outside the spend cap; judge calls and the reasoning surcharge were ignored;
# final_tokens_sent stayed the proxy's estimate; and no Prometheus counter moved.

_USAGE = {"prompt_tokens": 5, "completion_tokens": 2}
_CHUNKS = [
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}}]},
    {"choices": [], "usage": _USAGE},   # the usage-only chunk include_usage adds
]


def _stream_of(chunks, seen=None):
    async def fake_acompletion(**kwargs):
        if seen is not None:
            seen.append(kwargs)

        async def gen():
            for c in chunks:
                yield c
        return gen()
    return fake_acompletion


async def _run(monkeypatch, chunks, outgoing=None, ctx=None, seen=None):
    ctx = ctx or _Ctx()
    monkeypatch.setattr(main.litellm, "acompletion", _stream_of(chunks, seen))
    monkeypatch.setattr(main, "_record_outcome", lambda *a, **k: None)
    resp = main._stream_response(ctx, "gpt-4o-mini", {}, outgoing or {"stream": True},
                                 "req-s", 0.0, provider="openai", provider_key="sk")
    return ctx, await _collect(resp)


@pytest.mark.parametrize("requested,sent,withhold", [
    (None, {"include_usage": True}, False),                     # none sent: gets it, as before
    ({"include_usage": False}, {"include_usage": True}, True),  # opted out: still metered
    ({}, {"include_usage": True}, True),
    ({"include_usage": True}, {"include_usage": True}, False),
    ({"include_usage": False, "x": 1}, {"include_usage": True, "x": 1}, True),
])
def test_stream_options_always_request_usage(requested, sent, withhold):
    assert main._stream_options_with_usage(requested) == (sent, withhold)


@pytest.mark.asyncio
async def test_a_client_opting_out_of_usage_is_still_metered(monkeypatch):
    seen = []
    ctx, body = await _run(monkeypatch, _CHUNKS,
                           {"stream": True, "stream_options": {"include_usage": False}}, seen=seen)
    assert seen[0]["stream_options"]["include_usage"] is True   # the provider was asked
    assert '"usage"' not in body and "Hel" in body              # the client got what it asked
    assert ctx.savings.provider_prompt_tokens == 5 and ctx.savings.cost_actual_usd > 0


@pytest.mark.asyncio
async def test_a_client_without_stream_options_still_gets_the_usage_chunk(monkeypatch):
    _, body = await _run(monkeypatch, _CHUNKS)
    assert '"usage"' in body


@pytest.mark.asyncio
async def test_a_stream_without_a_usage_chunk_is_priced_from_an_estimate(monkeypatch):
    from middleware.g18_observability import STREAM_USAGE_ESTIMATED
    from savings.calculator import estimate_tokens
    before = STREAM_USAGE_ESTIMATED.labels(tenant_id="default")._value.get()
    ctx, _ = await _run(monkeypatch, _CHUNKS[:2])                 # no usage chunk at all
    assert ctx.savings.cost_actual_usd > 0
    assert ctx.savings.provider_prompt_tokens is None             # recorded as unreported usage
    assert ctx.savings.final_tokens_sent == 30                    # y, the proxy's own count
    assert ctx.savings.response_tokens == estimate_tokens("Hello", "gpt-4o-mini")
    assert STREAM_USAGE_ESTIMATED.labels(tenant_id="default")._value.get() == before + 1


@pytest.mark.asyncio
async def test_tool_call_text_counts_toward_the_estimate(monkeypatch):
    chunks = [{"choices": [{"delta": {"tool_calls": [{"function": {
        "name": "lookup", "arguments": '{"city": "Pune", "days": 5}'}}]}}]}]
    ctx, _ = await _run(monkeypatch, chunks)
    assert ctx.savings.response_tokens > 0


@pytest.mark.asyncio
async def test_a_client_disconnecting_before_the_usage_chunk_is_still_priced(monkeypatch):
    ctx, outcome = _Ctx(), {}
    monkeypatch.setattr(main.litellm, "acompletion", _stream_of(_CHUNKS))
    monkeypatch.setattr(main, "_record_outcome",
                        lambda c, start, status, response=None: outcome.update(status=status))
    resp = main._stream_response(ctx, "gpt-4o-mini", {}, {"stream": True}, "req-d", 0.0,
                                 provider="openai", provider_key="sk")
    await resp.body_iterator.__anext__()   # one chunk reaches the client...
    await resp.body_iterator.aclose()      # ...then it disconnects
    assert outcome["status"] == "200" and ctx.savings.cost_actual_usd > 0


@pytest.mark.asyncio
async def test_a_judge_call_is_part_of_a_streamed_calls_cost(monkeypatch):
    from savings.calculator import estimate_cost
    alone, _ = await _run(monkeypatch, _CHUNKS)
    judged = _Ctx()
    judged.provider_calls.append({"model": "gpt-4o-mini", "prompt_tokens": 1000,
                                  "completion_tokens": 100})           # G06's llm_judge
    judged, _ = await _run(monkeypatch, _CHUNKS, ctx=judged)
    assert judged.savings.cost_actual_usd == pytest.approx(
        alone.savings.cost_actual_usd + estimate_cost(1000, 100, "gpt-4o-mini"))


@pytest.mark.asyncio
async def test_the_reasoning_surcharge_applies_to_streams(monkeypatch):
    usage = {"prompt_tokens": 5, "completion_tokens": 200,
             "completion_tokens_details": {"reasoning_tokens": 150}}
    chunks = _CHUNKS[:2] + [{"choices": [], "usage": usage}]
    plain, _ = await _run(monkeypatch, chunks)
    surcharged = _Ctx()
    surcharged.config = {"groups": {"G18_observability": {"reasoning_rate_multiplier": 2.0}}}
    surcharged, _ = await _run(monkeypatch, chunks, ctx=surcharged)
    assert surcharged.savings.cost_actual_usd > plain.savings.cost_actual_usd


@pytest.mark.asyncio
async def test_final_tokens_sent_is_the_providers_count_for_streams(monkeypatch):
    ctx, _ = await _run(monkeypatch, _CHUNKS)
    assert ctx.savings.final_tokens_sent == 5   # z, as for a non-streamed call; not y (30)


@pytest.mark.asyncio
async def test_streamed_calls_move_the_token_and_cost_counters(monkeypatch):
    from middleware.g18_observability import COST_USD, PROMPT_TOKENS, REQUESTS_TOTAL
    labels = dict(model="gpt-4o-mini", team="default", feature="default", tenant_id="t-stream")
    before = [m.labels(**labels)._value.get() for m in (REQUESTS_TOTAL, PROMPT_TOKENS, COST_USD)]
    ctx = _Ctx()
    ctx.tenant_id = "t-stream"
    ctx.config = {"groups": {"G18_observability": {"enabled": True}}}
    ctx, _ = await _run(monkeypatch, _CHUNKS, ctx=ctx)
    assert REQUESTS_TOTAL.labels(**labels)._value.get() == before[0] + 1
    assert PROMPT_TOKENS.labels(**labels)._value.get() == before[1] + 5
    assert COST_USD.labels(**labels)._value.get() == pytest.approx(
        before[2] + ctx.savings.cost_actual_usd)


@pytest.mark.asyncio
async def test_with_g18_off_a_stream_is_priced_but_moves_no_counter(monkeypatch):
    from middleware.g18_observability import REQUESTS_TOTAL
    labels = dict(model="gpt-4o-mini", team="default", feature="default", tenant_id="t-off")
    before = REQUESTS_TOTAL.labels(**labels)._value.get()
    ctx = _Ctx()
    ctx.tenant_id = "t-off"                 # config {}: G18 disabled
    ctx, _ = await _run(monkeypatch, _CHUNKS, ctx=ctx)
    assert ctx.savings.cost_actual_usd > 0  # the spend cap still needs the cost
    assert REQUESTS_TOTAL.labels(**labels)._value.get() == before


@pytest.mark.asyncio
async def test_a_pricing_failure_is_logged_and_counted_not_swallowed(monkeypatch, caplog):
    import logging
    import middleware.g18_observability as g18
    before = g18.STREAM_ACCOUNTING_ERRORS.labels(tenant_id="default")._value.get()

    def broken(*a, **k):
        raise RuntimeError("pricing blew up")

    monkeypatch.setattr(g18, "price_response", broken)
    with caplog.at_level(logging.WARNING):
        _, body = await _run(monkeypatch, _CHUNKS)
    assert body.strip().endswith("data: [DONE]")                  # the stream is unaffected
    assert "could not be priced" in caplog.text
    assert g18.STREAM_ACCOUNTING_ERRORS.labels(tenant_id="default")._value.get() == before + 1


_TOOL_CALL_CHUNKS = [
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                            "function": {"name": "lookup", "arguments": ""}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}]},
    {"choices": [], "usage": _USAGE},
]


@pytest.mark.asyncio
async def test_a_streamed_tool_call_is_recorded_for_g08(monkeypatch):
    """G08's pruning reads which offered tools the model called; a streamed call counts."""
    from unittest.mock import AsyncMock
    import middleware.g08_tool_loading as g08
    record = AsyncMock()
    monkeypatch.setattr(g08, "record_called_tools", record)
    ctx = _Ctx()
    ctx.g08_offered_tools = ["lookup"]
    await _run(monkeypatch, _TOOL_CALL_CHUNKS, ctx=ctx)
    await main._drain_after_response()
    record.assert_awaited_once()
    assert record.await_args.args[0] is ctx and record.await_args.args[1] == {"lookup"}


@pytest.mark.asyncio
async def test_a_stream_that_offered_no_registry_tool_records_nothing(monkeypatch):
    from unittest.mock import AsyncMock
    import middleware.g08_tool_loading as g08
    record = AsyncMock()
    monkeypatch.setattr(g08, "record_called_tools", record)
    await _run(monkeypatch, _TOOL_CALL_CHUNKS)                    # _Ctx offers nothing
    await main._drain_after_response()
    record.assert_not_awaited()

