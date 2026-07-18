"""Unit tests for the application-quality metrics surface (middleware/quality_metrics.py):
the pure grounding_coverage heuristic + the PII-free emit helpers."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from middleware import quality_metrics as Q


# ── grounding_coverage heuristic ──────────────────────────────────────────────

class TestGroundingCoverage:
    def test_full_overlap_is_one(self):
        assert Q.grounding_coverage(
            "Paris is the capital of France.",
            ["Paris is the capital of France and a major European city."],
        ) == 1.0

    def test_no_overlap_is_zero(self):
        assert Q.grounding_coverage(
            "The moon is made of cheese.",
            ["Paris is the capital of France."],
        ) == 0.0

    def test_partial_overlap_is_fraction(self):
        # one grounded sentence, one ungrounded → 0.5
        cov = Q.grounding_coverage(
            "Paris is the capital of France. The moon is made of cheese.",
            ["Paris is the capital of France."],
        )
        assert cov == 0.5

    def test_empty_answer_is_zero(self):
        assert Q.grounding_coverage("", ["Paris"]) == 0.0

    def test_empty_chunks_is_zero(self):
        assert Q.grounding_coverage("Paris is nice.", []) == 0.0

    def test_answer_with_only_stopwords_does_not_crash(self):
        # No content tokens in the sentence → ignored, no division-by-zero.
        assert Q.grounding_coverage("the and or but.", ["Paris is the capital."]) == 0.0

    def test_min_overlap_is_tunable(self):
        # A sentence half-covered passes at 0.5 but fails at 0.9.
        answer = "Paris rainbow capital."   # 3 content tokens; 2 in context
        chunks = ["Paris is the capital of France."]
        assert Q.grounding_coverage(answer, chunks, min_overlap=0.5) == 1.0
        assert Q.grounding_coverage(answer, chunks, min_overlap=0.9) == 0.0


# ── emit helpers (increment the right metric; never raise) ────────────────────

def _val(counter, **labels):
    return counter.labels(**labels)._value.get()


class TestEmitHelpers:
    def test_record_retrieval_hit_and_miss(self):
        before_hit = _val(Q.RETRIEVAL_REQUESTS_TOTAL, tenant_id="t1", result="hit")
        before_miss = _val(Q.RETRIEVAL_REQUESTS_TOTAL, tenant_id="t1", result="miss")
        Q.record_retrieval("t1", n_chunks=3, max_age_seconds=100.0)
        Q.record_retrieval("t1", n_chunks=0)
        assert _val(Q.RETRIEVAL_REQUESTS_TOTAL, tenant_id="t1", result="hit") == before_hit + 1
        assert _val(Q.RETRIEVAL_REQUESTS_TOTAL, tenant_id="t1", result="miss") == before_miss + 1
        # gauge set only when age known
        assert Q.CONTEXT_MAX_AGE_SECONDS.labels(tenant_id="t1")._value.get() == 100.0

    def test_record_grounding_and_verify(self):
        Q.record_grounding("t2", 0.75)   # must not raise
        Q.record_verify_score("t2", 4)

    def test_record_schema_failure_and_tool_denied(self):
        before = _val(Q.TOOL_ELIGIBILITY_DENIED_TOTAL, tenant_id="t3")
        Q.record_tool_denied("t3")
        assert _val(Q.TOOL_ELIGIBILITY_DENIED_TOTAL, tenant_id="t3") == before + 1
        Q.record_schema_failure("t3", "block")   # must not raise

    def test_helpers_never_raise_on_bad_input(self):
        # None tenant, weird values — helpers swallow errors (metrics must not break requests).
        Q.record_retrieval(None, n_chunks=1)
        Q.record_grounding(None, float("nan"))
        Q.record_tool_denied(None, 0)


# ── emit_grounding (response-path wiring) ─────────────────────────────────────
class _Ctx:
    def __init__(self, chunks=None, tenant_id="tg"):
        if chunks is not None:
            self.rag_chunk_texts = chunks
        self.tenant_id = tenant_id


def _answer_resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class TestEmitGrounding:
    def test_emits_when_chunks_and_answer_present(self):
        from unittest.mock import patch
        ctx = _Ctx(chunks=["Paris is the capital of France."])
        with patch.object(Q, "record_grounding") as m:
            Q.emit_grounding(ctx, _answer_resp("Paris is the capital of France."))
        m.assert_called_once()
        assert m.call_args.args[0] == "tg"
        assert m.call_args.args[1] == 1.0        # fully grounded

    def test_noop_when_no_rag_chunks(self):
        from unittest.mock import patch
        ctx = _Ctx(chunks=None)   # non-RAG request → attribute absent
        with patch.object(Q, "record_grounding") as m:
            Q.emit_grounding(ctx, _answer_resp("anything"))
        m.assert_not_called()

    def test_noop_on_tool_call_answer(self):
        from unittest.mock import patch
        ctx = _Ctx(chunks=["some context"])
        tool_resp = {"choices": [{"message": {"role": "assistant", "content": None,
                     "tool_calls": [{"id": "c1"}]}}]}
        with patch.object(Q, "record_grounding") as m:
            Q.emit_grounding(ctx, tool_resp)
        m.assert_not_called()

    def test_never_raises_on_malformed_response(self):
        ctx = _Ctx(chunks=["ctx"])
        Q.emit_grounding(ctx, {})              # no choices
        Q.emit_grounding(ctx, {"choices": []})  # empty choices


class TestPiiFreeLabels:
    def test_no_content_labels(self):
        # Every metric's labels must be tenant_id (+ a small enum), never content.
        allowed = {"tenant_id", "result", "mode"}
        for metric in (Q.RETRIEVAL_REQUESTS_TOTAL, Q.RETRIEVAL_CHUNKS_RETURNED,
                       Q.CONTEXT_MAX_AGE_SECONDS, Q.GROUNDING_COVERAGE,
                       Q.OUTPUT_SCHEMA_FAILURES_TOTAL, Q.TOOL_ELIGIBILITY_DENIED_TOTAL,
                       Q.OUTPUT_VERIFY_SCORE):
            assert set(metric._labelnames) <= allowed, f"{metric._name} has non-allowed labels"
