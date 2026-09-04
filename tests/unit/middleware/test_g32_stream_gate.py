"""G32 must enforce the tool policy on STREAMED responses too (backlog #25).

Before this, `main._stream_response` relayed provider chunks and never called the response
pipeline, so a tenant's DENY rule simply did not apply to streaming: the call was relayed
and the caller's own agent loop executed it. A security control that silently did nothing
on the most common agentic path.

What it is NOT: streaming also skips G15/G28, so the proxy never server-side-executes a
streamed tool call. The exposure was policy NON-ENFORCEMENT, not proxy execution — these
tests pin that distinction so a future reader does not "fix" the wrong thing.
"""
import pytest

from middleware.g32_tool_eligibility import G32ToolEligibility, StreamToolGate


def _cfg(minimal_config, **over):
    g32 = {"enabled": True, "mode": "block",
           "policy": {"deny": ["delete_*"], "allow": ["*"], "default": "allow"}}
    g32.update(over)
    minimal_config["groups"]["G32_tool_eligibility"] = g32
    return minimal_config


def _ctx(make_ctx, minimal_config, **over):
    return make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o",
                    config=_cfg(minimal_config, **over))


def _chunk(*calls, content=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if calls:
        delta["tool_calls"] = list(calls)
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _call(index, name=None, args=None, cid=None):
    entry = {"index": index}
    if cid:
        entry["id"] = cid
    fn = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    if fn:
        entry["function"] = fn
    return entry


def _gate(make_ctx, minimal_config, **over):
    return G32ToolEligibility().stream_gate(_ctx(make_ctx, minimal_config, **over))


class TestGateConstruction:
    def test_no_gate_on_a_default_install(self, make_ctx, minimal_config):
        """No policy configured → no per-chunk work at all, streaming byte-identical."""
        assert _gate(make_ctx, minimal_config,
                     policy={"deny": [], "allow": [], "default": "allow"}) is None

    def test_no_gate_when_mode_is_off(self, make_ctx, minimal_config):
        assert _gate(make_ctx, minimal_config, mode="off") is None

    def test_no_gate_when_disabled(self, make_ctx, minimal_config):
        assert _gate(make_ctx, minimal_config, enabled=False) is None

    def test_gate_built_when_a_policy_exists(self, make_ctx, minimal_config):
        assert isinstance(_gate(make_ctx, minimal_config), StreamToolGate)


class TestBlockMode:
    def test_a_denied_call_never_reaches_the_client(self, make_ctx, minimal_config):
        """The whole point: without this the client receives — and runs — the call."""
        gate = _gate(make_ctx, minimal_config)
        out = gate.filter(_chunk(_call(0, "delete_everything", cid="c1")))
        assert out is None, "a chunk carrying only the denied call must be dropped"
        assert gate.denied == ["delete_everything"]
        assert gate.stripped is True

    def test_an_allowed_call_passes_through_untouched(self, make_ctx, minimal_config):
        gate = _gate(make_ctx, minimal_config)
        chunk = _chunk(_call(0, "search_docs", cid="c1"))
        out = gate.filter(chunk)
        assert out is chunk
        assert gate.denied == []

    def test_later_argument_deltas_for_a_denied_call_are_also_dropped(
            self, make_ctx, minimal_config):
        """The name arrives once; arguments stream after it. Dropping only the first
        delta would leak a partial call the client could still act on."""
        gate = _gate(make_ctx, minimal_config)
        assert gate.filter(_chunk(_call(0, "delete_everything", cid="c1"))) is None
        assert gate.filter(_chunk(_call(0, args='{"path"'))) is None
        assert gate.filter(_chunk(_call(0, args=': "/"}'))) is None

    def test_a_mixed_chunk_keeps_the_allowed_call_only(self, make_ctx, minimal_config):
        gate = _gate(make_ctx, minimal_config)
        out = gate.filter(_chunk(_call(0, "search_docs", cid="a"),
                                 _call(1, "delete_everything", cid="b")))
        assert out is not None
        kept = out["choices"][0]["delta"]["tool_calls"]
        assert [c["index"] for c in kept] == [0]
        assert gate.denied == ["delete_everything"]

    def test_content_in_the_same_chunk_survives(self, make_ctx, minimal_config):
        """Stripping a tool call must not swallow assistant text riding along with it."""
        gate = _gate(make_ctx, minimal_config)
        out = gate.filter(_chunk(_call(0, "delete_everything"), content="one moment"))
        assert out is not None
        assert out["choices"][0]["delta"]["content"] == "one moment"
        assert "tool_calls" not in out["choices"][0]["delta"]

    def test_finish_reason_is_corrected_when_the_only_call_is_dropped(
            self, make_ctx, minimal_config):
        gate = _gate(make_ctx, minimal_config)
        out = gate.filter(_chunk(_call(0, "delete_everything"), finish="tool_calls"))
        assert out is not None, "a chunk carrying finish_reason must still be relayed"
        assert out["choices"][0]["finish_reason"] == "stop"

    def test_a_non_tool_calls_finish_reason_is_left_alone(self, make_ctx, minimal_config):
        """Providers sometimes send `stop` alongside tool calls; clobbering it is its
        own bug — the same care the non-streaming path takes."""
        gate = _gate(make_ctx, minimal_config)
        out = gate.filter(_chunk(_call(0, "delete_everything"), finish="length"))
        assert out["choices"][0]["finish_reason"] == "length"


class TestFlagMode:
    def test_flag_records_but_relays_untouched(self, make_ctx, minimal_config):
        """flag governs the RECORD, not the response — identical to non-streaming, and
        the distinction that made `flag` dangerous at the G15 dispatch site."""
        gate = _gate(make_ctx, minimal_config, mode="flag")
        chunk = _chunk(_call(0, "delete_everything"))
        out = gate.filter(chunk)
        assert out is chunk
        assert gate.denied == ["delete_everything"]
        assert gate.stripped is False


class TestUnevaluableCalls:
    def test_a_call_whose_name_never_arrives_is_withheld(self, make_ctx, minimal_config):
        """Fail-CLOSED, matching evaluate_tool's posture for a missing name. Providers
        do not currently emit this shape; it is handled so a future one cannot slip
        past unevaluated."""
        gate = _gate(make_ctx, minimal_config)
        assert gate.filter(_chunk(_call(0, args='{"a":1}'))) is None
        gate.finish()
        assert gate.denied == ["<unnamed>"]

    def test_a_name_arriving_late_is_evaluated_then(self, make_ctx, minimal_config):
        gate = _gate(make_ctx, minimal_config)
        assert gate.filter(_chunk(_call(0, args='{"a":'))) is None      # held
        out = gate.filter(_chunk(_call(0, "search_docs")))              # now evaluable
        assert out is not None
        assert gate.denied == []


class TestRecording:
    def test_finish_records_the_verdict_on_the_context(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config)
        gate = G32ToolEligibility().stream_gate(ctx)
        gate.filter(_chunk(_call(0, "delete_everything")))
        gate.finish()
        assert ctx.tool_eligibility_action == "block"
        assert ctx.tool_eligibility_denied == ["delete_everything"]
        assert ctx.tool_eligibility_count == 1

    def test_stripping_marks_the_response_uncacheable(self, make_ctx, minimal_config):
        """Otherwise the cached artifact is POLICY-SPECIFIC: loosen the policy and G05
        keeps serving the stripped answer until TTL. Same reason the non-streaming path
        sets it."""
        ctx = _ctx(make_ctx, minimal_config)
        gate = G32ToolEligibility().stream_gate(ctx)
        gate.filter(_chunk(_call(0, "delete_everything")))
        gate.finish()
        assert ctx.no_cache is True

    def test_flag_does_not_mark_uncacheable(self, make_ctx, minimal_config):
        """flag mutates nothing, so the artifact is not policy-specific."""
        ctx = _ctx(make_ctx, minimal_config, mode="flag")
        gate = G32ToolEligibility().stream_gate(ctx)
        gate.filter(_chunk(_call(0, "delete_everything")))
        gate.finish()
        assert getattr(ctx, "no_cache", False) is False

    def test_a_clean_stream_records_nothing(self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config)
        gate = G32ToolEligibility().stream_gate(ctx)
        gate.filter(_chunk(_call(0, "search_docs")))
        gate.finish()
        assert not getattr(ctx, "tool_eligibility_denied", [])


class TestChunksItMustNotDisturb:
    @pytest.mark.parametrize("chunk", [
        {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        {"choices": []},
        {},
    ])
    def test_ordinary_chunks_pass_through_identically(self, make_ctx, minimal_config, chunk):
        gate = _gate(make_ctx, minimal_config)
        assert gate.filter(chunk) is chunk

    def test_malformed_choices_do_not_raise(self, make_ctx, minimal_config):
        gate = _gate(make_ctx, minimal_config)
        for bad in ({"choices": "nope"}, {"choices": [None]},
                    {"choices": [{"delta": "nope"}]},
                    {"choices": [{"delta": {"tool_calls": "nope"}}]},
                    {"choices": [{"delta": {"tool_calls": [None]}}]}):
            gate.filter(bad)   # must not raise
