"""A compaction must shorten a payload, never delete it (backlog #45, then #48).

Three acceptance rules, each added after the previous one proved insufficient:

  1. **Length** — the original, and the only test until 2026-09-05: shorter is better.
  2. **Parseability** (#45) — a tool result's consumer does `json.loads(result)`, so
     shorter-but-broken is a silent failure on a request that has already been billed.
  3. **No unresolvable reference** (#48) — the one that was actually firing in production.

Rule 3 exists because `headroom.SmartCrusher.compact_document_json`, which G19 routed all
JSON through, replaces any JSON string leaf of roughly 300 characters or more with a
`<<ccr:HASH,string,NB>>` marker pointing into an in-process Rust store no route of ours
exposes. The output is valid JSON and it is shorter, so rules 1 and 2 both pass it — and
the model receives a pointer to nothing. Confirmed 2026-09-06 by running the real
middleware inside the deployed container against the shipped config: a runbook tool result
came back as `"summary":"<<ccr:884b9b5deca1,string,423B>>"`.

The permanent fix was to stop calling that compactor (measured first: the built-in JSON
compactor was within 0.8 points of it on 70 real tool payloads). Rule 3 is the belt: it is
INERT against the structural compressors this module now ships, and it is here so that
re-introducing any third-party compactor cannot repeat #48 in silence.

Note the failure that made this necessary: on 2026-09-05 this same sentinel was probed
against headroom 0.34.0 and declared not to fire. That probe used arrays of short scalar
fields — no long string leaf — so it never exercised the path. A guard stated as a
property of the OUTPUT does not depend on getting such a probe right.
"""
import json

import pytest

from middleware import g19_headroom as mod


_DOC = json.dumps({
    "status": "success",
    "results": [{"id": i, "action": "searched", "score": 0.9} for i in range(12)],
}, separators=(",", ":"))

_MARKED = json.dumps({"status": "success", "results": "<<ccr:884b9b5deca1,string,423B>>"},
                     separators=(",", ":"))


class TestTheGuardRefusesWhatItMust:
    def test_an_unresolvable_reference_is_refused(self):
        """#48 in one assertion. Valid JSON and shorter, so nothing but a CONTENT check
        can catch it."""
        assert len(_MARKED) < len(_DOC), "fixture must be shorter, or it proves nothing"
        json.loads(_MARKED)  # and valid, or it proves nothing either
        assert mod._is_faithful_compaction(_DOC, _MARKED) is False

    @pytest.mark.parametrize("marker", [
        "<<ccr:abc123,string,400B>>",
        "<<CCR:abc123,string,400B>>",      # case must not be an escape hatch
        "<< ccr :abc123>>",                # nor incidental whitespace
    ])
    def test_the_marker_is_matched_by_shape_not_by_one_literal(self, marker):
        after = json.dumps({"summary": marker})
        assert mod._is_faithful_compaction(json.dumps({"summary": "x" * 400}), after) is False

    def test_shorter_but_unparseable_output_is_refused(self):
        """Rule 2 must survive rule 3 being added."""
        bad = "[12]{id:int,action:string}\n0,searched\n1,searched\n"
        assert len(bad) < len(_DOC)
        assert mod._is_faithful_compaction(_DOC, bad) is False


class TestTheGuardAllowsWhatItMust:
    def test_a_genuine_structural_compaction_passes(self):
        good = json.dumps({"status": "success",
                           "results": {"_schema_": ["id"], "_rows_": [[1], [2]]}},
                          separators=(",", ":"))
        assert mod._is_faithful_compaction(_DOC, good) is True

    def test_a_non_json_input_carries_no_parseability_promise(self):
        """One-directional by design: if the INPUT was never valid JSON we never promised
        the output would be, and refusing there would disable compaction on prose and logs
        for no benefit."""
        assert mod._is_faithful_compaction("plain prose, at length", "shorter prose") is True

    def test_but_a_reference_marker_is_refused_even_in_non_json(self):
        """The parseability rule is one-directional; the deletion rule is not. Prose whose
        content has been swapped for a pointer is just as gone as JSON's would be."""
        assert mod._is_faithful_compaction(
            "a long runbook " * 40, "<<ccr:deadbeef,string,600B>>") is False

    def test_prose_that_merely_mentions_ccr_is_not_a_false_positive(self):
        """The guard keys on the marker's delimiters, not on the three letters — an answer
        discussing CCR must still be compressible."""
        before = "Our CCR design uses ccr: prefixed keys in Redis. " * 8
        assert mod._is_faithful_compaction(before, "CCR uses ccr: prefixed keys.") is True


class TestTheGuardIsWiredIntoTheLivePath:
    """A guard nothing calls is decoration. These drive `_compress`, not the predicate."""

    def test_compress_returns_none_rather_than_an_unfaithful_result(self, monkeypatch):
        """Returning None means 'not compressible', and the caller sends the payload
        whole — costing tokens and losing nothing. That is the correct trade on a
        response path where the request is already paid for."""
        monkeypatch.setattr(mod, "_compress_json",
                            lambda text, strategy: "<<ccr:abc,string,400B>>")
        assert mod._compress(_DOC, "json", {}) is None

    def test_every_content_type_passes_through_the_guard(self, monkeypatch):
        """json / code / logs / text all route through the same boundary — a future
        compactor added to any one of them inherits the check."""
        for content_type, fn in (("json", "_compress_json"), ("code", "_compress_code"),
                                 ("logs", "_compress_logs"), ("text", "_compress_text")):
            monkeypatch.setattr(mod, fn, lambda text, strategy: "<<ccr:abc,string,9B>>")
            assert mod._compress("some payload here", content_type, {}) is None, content_type

    def test_a_faithful_compaction_still_gets_through(self):
        """Guards to prove: this one must not have turned compaction off."""
        out = mod._compress(_DOC, "json", {"remove_empty": True, "dedupe_keys": True})
        assert isinstance(out, str) and len(out) < len(_DOC)
        json.loads(out)


class TestNoThirdPartyCompactorIsReachable:
    """The permanent half of the fix. Rules 1–3 are the belt; this is removing the cause.

    Pinned by source inspection rather than behaviour because the failure mode is someone
    re-adding the call — which no behavioural test of the current code can detect.
    """

    @pytest.mark.parametrize("module_name", ["g19_headroom", "g14_tool_output"])
    def test_neither_module_calls_a_headroom_compactor(self, module_name):
        import importlib
        import inspect
        src = inspect.getsource(importlib.import_module(f"middleware.{module_name}"))
        code = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        for forbidden in ("SmartCrusher", "compact_document_json", ".crush("):
            assert forbidden not in code, (
                f"{module_name} calls {forbidden} again. It deletes string leaves >= ~300 "
                f"chars behind an unresolvable <<ccr:...>> marker (backlog #48) and buys "
                f"0.8 points over the built-in compactor. Measured, not assumed."
            )
