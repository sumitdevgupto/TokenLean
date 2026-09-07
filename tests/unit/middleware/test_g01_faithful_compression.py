"""G01 must not return a compression that says something the source did not.

E26, 2026-09-06. DS9 shipped a wrong answer to a customer through this module. The assistant
message in the conversation said:

    "employees may carry over up to 5 unused PTO days into the following calendar year.
     Any PTO EXCEEDING THIS LIMIT is forfeited on January 1st."

LLMLingua-2 at the shipped ratio returned:

    "Section 4. 2 HR Policy Manual : 5 PTO days. PTO FORFEITED January 1st."

Deleting "exceeding this limit" does not lose a detail — it inverts the rule. The model then
reported the inversion faithfully: "unused PTO days are forfeited... you are not permitted to
carry over any." `force_reserve_digit` had protected the number "5"; nothing protected the
words that bound it.

What makes this a permanent guard rather than a threshold tweak: G01's output was
BYTE-IDENTICAL in the run where DS9 passed (2026-08-07) and the one where it failed
(2026-09-06) — 207t → 181t, saving 26, both times. The compressor did not regress. The model
stopped reconstructing the fact we had destroyed. Eleven consecutive PASSes measured the
model's willingness to repair our damage, not the safety of the compression.
"""
import pathlib
import sys

import pytest

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

from middleware.g01_compression import _is_faithful_compression  # noqa: E402

_PTO_SOURCE = (
    "Per Section 4.2 of the HR Policy Manual: employees may carry over up to 5 unused PTO "
    "days into the following calendar year. Any PTO exceeding this limit is forfeited on "
    "January 1st. Please submit your carry-over request to HR by December 15th."
)
# Verbatim output of the live LLMLingua sidecar for the above, ratio 0.5.
_PTO_COMPRESSED = (
    "Section 4. 2 HR Policy Manual : 5 PTO days. PTO forfeited January 1st. "
    "carry - over December 15th."
)


class TestTheDefectThatCausedThis:
    def test_the_real_pto_inversion_is_refused(self):
        assert _is_faithful_compression(_PTO_SOURCE, _PTO_COMPRESSED) is False, (
            "this exact pair reached a customer as 'you are not permitted to carry over any'"
        )

    def test_the_qualifier_is_what_makes_it_unfaithful(self):
        """Keeping 'exceeding' and 'limit' makes the same shortening acceptable — the guard
        is about meaning-bearing words, not about length."""
        kept = ("Section 4.2 HR Policy Manual: carry over up to 5 unused PTO days. "
                "PTO exceeding limit forfeited January 1st.")
        assert _is_faithful_compression(_PTO_SOURCE, kept) is True


class TestNegationsMustSurvive:
    @pytest.mark.parametrize("before,after", [
        ("You must not restart the database during business hours.",
         "You must restart database during business hours."),
        ("Refunds are never issued after 30 days.",
         "Refunds issued after 30 days."),
        ("This account cannot be reactivated once closed.",
         "This account be reactivated once closed."),
        ("Access is granted without exception to auditors.",
         "Access granted exception auditors."),
    ])
    def test_dropping_a_negation_is_refused(self, before, after):
        assert _is_faithful_compression(before, after) is False

    def test_keeping_the_negation_is_allowed(self):
        assert _is_faithful_compression(
            "You must not restart the database during business hours.",
            "must not restart database business hours.") is True


class TestScopeLimitersMustSurvive:
    @pytest.mark.parametrize("before,after", [
        ("Employees may claim up to 3 days of leave.", "Employees may claim 3 days leave."),
        ("Charges apply unless the order is cancelled.", "Charges apply the order cancelled."),
        ("A maximum of 10 retries is permitted.", "10 retries permitted."),
        ("Only managers can approve this request.", "managers can approve this request."),
    ])
    def test_dropping_a_scope_limiter_is_refused(self, before, after):
        """Each of these turns a bounded rule into an unbounded one — the DS9 shape."""
        assert _is_faithful_compression(before, after) is False


class TestItDoesNotOverRefuse:
    def test_ordinary_terseness_is_allowed(self):
        """The guard must not become a blanket refusal — G01 has to keep working on prose
        that carries no negation or bound."""
        assert _is_faithful_compression(
            "The deployment pipeline builds the container image and pushes it to the registry.",
            "deployment pipeline builds container image pushes registry.") is True

    def test_case_and_punctuation_do_not_matter(self):
        assert _is_faithful_compression(
            "Refunds are NOT issued after 30 days.",
            "refunds not issued, after 30 days") is True

    def test_an_empty_compression_of_empty_source_is_fine(self):
        assert _is_faithful_compression("", "") is True

    def test_an_unchanged_message_is_faithful(self):
        assert _is_faithful_compression(_PTO_SOURCE, _PTO_SOURCE) is True


class TestItGuardsEveryCompressorNotJustLLMLingua:
    def test_the_check_sits_at_the_single_acceptance_point(self):
        """LLMLingua, the Kompress fallback and the deterministic prose fallback all write
        into the same `compressed` variable. Guarding there rather than per-compressor is
        what makes a future compressor safe by default (Gate 9.2: generic and permanent)."""
        import inspect

        from middleware.g01_compression import G01Compression
        src = inspect.getsource(G01Compression.process_request)
        assert "_is_faithful_compression(content, compressed)" in src
        assert src.index("_call_llmlingua") < src.index("_is_faithful_compression")
        assert src.index("_prose_compress_text") < src.index("_is_faithful_compression")

    def test_refusing_sends_the_original_not_a_partial(self):
        import inspect

        from middleware.g01_compression import G01Compression
        src = inspect.getsource(G01Compression.process_request)
        guard = src.index("_is_faithful_compression(content, compressed)")
        assert "compressed = content" in src[guard:guard + 900], (
            "on refusal G01 must fall back to the ORIGINAL message — a request that costs "
            "more is recoverable, a wrong answer is not"
        )
