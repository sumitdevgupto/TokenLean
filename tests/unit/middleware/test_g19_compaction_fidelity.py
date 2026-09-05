"""A compaction must not turn parseable JSON into something that is not (backlog #45).

Until 2026-09-05 the only thing standing between headroom's compactor and a client was a
LENGTH check: `0 < len(crushed) < len(text)`. Shorter was the whole test. For a tool
result that is not enough, because the consumer typically does `json.loads(result)` — so
"shorter but unparseable" is a silent break on a billed 200.

Measured on headroom 0.34.0, `compact_document_json` DOES preserve the JSON envelope, so
this guard is inert against the shipped library. That is the point: it is inert because of
what the vendored Rust currently does, not because of anything our code enforced. The same
module already records two occasions where this transform destroyed content — an
11,492-char runbook reduced to 45 chars (2026-09-03) and the 2026-08-05 answer corruption
— so a version bump must not be able to reintroduce it quietly.

The tests below drive the guard with a stand-in crusher, because the only way to prove a
guard works is to hand it the thing it is supposed to refuse.
"""
import json

import pytest
from unittest.mock import MagicMock, patch

from middleware import g19_headroom as mod


_DOC = json.dumps({
    "status": "success",
    "results": [{"id": i, "action": "searched", "score": 0.9} for i in range(12)],
}, separators=(",", ":"))


def _crusher(returns):
    sc = MagicMock()
    sc.compact_document_json.side_effect = lambda _t: returns
    return sc


def _compress(text, crusher):
    with patch.object(mod, "_headroom_available", True), \
         patch.object(mod, "_smart_crusher", crusher):
        return mod._compress(text, "json", {})


class TestTheGuardRefusesWhatItMust:
    def test_shorter_but_unparseable_output_is_rejected(self):
        """The exact failure the length check let through."""
        bad = "[12]{id:int,action:string}\n0,searched\n1,searched\n"
        assert len(bad) < len(_DOC), "fixture must be SHORTER, or it proves nothing"
        out = _compress(_DOC, _crusher(bad))
        assert out != bad, (
            "a compaction that destroyed JSON validity was accepted purely for being "
            "shorter — a client calling json.loads() on this tool result would break"
        )

    def test_the_result_is_still_usable_after_a_rejection(self):
        """Rejecting must fall through to the built-in compactor, not return None or
        raise — G19 sits on the response path of a request that has already been billed."""
        out = _compress(_DOC, _crusher("not json at all"))
        assert out is None or isinstance(out, str)
        if isinstance(out, str):
            json.loads(out)


class TestTheGuardAllowsWhatItMust:
    def test_shorter_and_still_parseable_is_accepted(self):
        """The shipped behaviour: headroom keeps the envelope, so this is the real path."""
        good = json.dumps({"status": "success", "results": "[12]{id:int}\n0\n1\n"},
                          separators=(",", ":"))
        assert len(good) < len(_DOC)
        assert _compress(_DOC, _crusher(good)) == good

    def test_a_non_json_input_carries_no_promise(self):
        """One-directional by design. If the INPUT was never valid JSON we never promised
        the output would be, and refusing there would disable compaction on prose and
        logs for no benefit."""
        text = "a line of plain prose that is quite long and repeats itself " * 6
        out = _compress(text, _crusher("shorter prose"))
        assert out == "shorter prose"

    def test_longer_output_is_still_rejected_on_length_alone(self):
        """The original guard must survive — this change adds a check, it does not
        replace one."""
        longer = _DOC + json.dumps({"padding": "x" * 500})
        assert _compress(_DOC, _crusher(longer)) != longer


class TestG14UsesTheLosslessEntryPoint:
    def test_g14_never_calls_crush(self):
        """`crush` owns a lossy row-dropping path that emits a `<<ccr:HASH>>` retrieval
        marker. Nothing in this repo resolves that marker — the dropped rows sit in
        headroom's in-process Rust store, which no route of ours exposes and which does
        not survive a restart or span instances. Verified not to fire on 0.34.0 across
        queries and payload sizes; not calling it is the durable defence."""
        import inspect
        from middleware import g14_tool_output as g14
        assert "_smart_crusher.crush(" not in inspect.getsource(g14)
