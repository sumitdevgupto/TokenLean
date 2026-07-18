"""Unit tests for the G29 PII detection + masking engine (guardrails/pii.py).

Precision-biased: every true-positive case has a matching false-positive guard so
the regexes can't be loosened without a test noticing.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from guardrails.pii import (
    PiiDetector, PiiMatch, RedactionResult, mask_matches, unmask_text, remask_with_vault,
    _resolve_overlaps, _luhn_ok,
    EMAIL, US_SSN, CREDIT_CARD, PHONE, IP_ADDRESS,
)


def _types(text, **kw):
    return sorted({m.entity_type for m in PiiDetector(**kw).detect(text)})


# ── Email ──────────────────────────────────────────────────────────────────────
def test_email_true_positive():
    assert EMAIL in _types("reach me at john.doe+billing@sub.example.co.uk today")


def test_email_no_false_positive_on_at_word():
    assert EMAIL not in _types("meet me @ the office at noon")


# ── US SSN — separated forms only, valid ranges ──────────────────────────────────
@pytest.mark.parametrize("s", ["123-45-6789", "123 45 6789", "SSN: 078-05-1120"])
def test_ssn_true_positive(s):
    assert US_SSN in _types(s)


@pytest.mark.parametrize("s", [
    "order 123456789 shipped",        # bare 9-digit run is NOT an SSN
    "999-45-6789",                    # invalid area (900-999)
    "000-45-6789",                    # invalid area (000)
    "666-45-6789",                    # invalid area (666)
    "123-00-6789",                    # invalid group (00)
    "123-45-0000",                    # invalid serial (0000)
    "123-45 6789",                    # mixed separators
])
def test_ssn_false_positive_guards(s):
    assert US_SSN not in _types(s)


# ── Credit card — Luhn-validated ─────────────────────────────────────────────────
@pytest.mark.parametrize("s", ["4111111111111111", "4111 1111 1111 1111", "5500-0000-0000-0004"])
def test_credit_card_true_positive(s):
    assert CREDIT_CARD in _types(s)


@pytest.mark.parametrize("s", [
    "4111111111111112",               # fails Luhn (last digit wrong)
    "1234567812345678",               # random 16-digit, fails Luhn
    "12345678",                       # too short to be a card
])
def test_credit_card_false_positive_guards(s):
    assert CREDIT_CARD not in _types(s)


def test_luhn_helper():
    assert _luhn_ok("4111111111111111")
    assert not _luhn_ok("4111111111111112")


# ── Phone (North American) ───────────────────────────────────────────────────────
@pytest.mark.parametrize("s", ["(415) 555-2671", "+1 415-555-2671", "415.555.2671"])
def test_phone_true_positive(s):
    assert PHONE in _types(s)


def test_phone_no_false_positive_on_plain_digits():
    # A 10-digit run with no separators is not treated as a phone number.
    assert PHONE not in _types("the id is 4155552671 exactly")


# ── IPv4 with octet range validation ─────────────────────────────────────────────
def test_ip_true_positive():
    assert IP_ADDRESS in _types("connect to 192.168.1.254 now")


def test_ip_false_positive_on_out_of_range_octets():
    assert IP_ADDRESS not in _types("version 999.999.1.1 released")


# ── Entity narrowing ─────────────────────────────────────────────────────────────
def test_entities_narrowing_scans_only_requested_types():
    text = "email a@b.com or call 415-555-2671"
    assert _types(text, entities=[EMAIL]) == [EMAIL]


# ── Masking + reversible round-trip ──────────────────────────────────────────────
def test_mask_reversible_round_trip():
    text = "Email john@x.com or card 4111 1111 1111 1111 please"
    det = PiiDetector()
    matches = det.detect(text)
    res = mask_matches(text, matches, reversible=True)
    assert "john@x.com" not in res.text
    assert "4111 1111 1111 1111" not in res.text
    assert "[PII:EMAIL:1]" in res.text
    # The vault restores the exact original.
    assert unmask_text(res.text, res.vault) == text


def test_remask_with_vault_inverts_unmask():
    # F3: after un-masking for the client, the trace path re-masks with the same vault.
    text = "Email alice@x.com now"
    res = mask_matches(text, PiiDetector().detect(text), reversible=True)
    restored = unmask_text(res.text, res.vault)
    assert "alice@x.com" in restored
    remasked = remask_with_vault(restored, res.vault)
    assert remasked == res.text
    assert "alice@x.com" not in remasked


def test_remask_longest_original_first_avoids_substring_collision():
    vault = {"[PII:EMAIL:1]": "a@b.com", "[PII:EMAIL:2]": "a@b.com.uk"}
    # "a@b.com" is a prefix of "a@b.com.uk"; longest-first must mask the long one whole.
    assert remask_with_vault("contact a@b.com.uk please", vault) == "contact [PII:EMAIL:2] please"


def test_remask_empty_vault_is_noop():
    assert remask_with_vault("nothing here", {}) == "nothing here"


def test_mask_irreversible_has_empty_vault_and_typed_placeholder():
    text = "ssn 123-45-6789 here"
    res = mask_matches(text, PiiDetector().detect(text), reversible=False)
    assert res.text == "ssn [US_SSN] here"
    assert res.vault == {}


def test_mask_noop_when_no_matches():
    text = "nothing sensitive here"
    res = mask_matches(text, PiiDetector().detect(text))
    assert res.text == text
    assert res.count == 0
    assert res.entity_types == []


def test_redaction_result_reports_types_without_raw_text():
    text = "a@b.com and 192.168.0.1"
    res = mask_matches(text, PiiDetector().detect(text))
    # entity_types is the audit-safe surface — types only, no raw values.
    assert res.entity_types == sorted([EMAIL, IP_ADDRESS])
    assert "a@b.com" not in "".join(res.entity_types)


# ── Overlap resolution ───────────────────────────────────────────────────────────
def test_resolve_overlaps_prefers_longer_span():
    matches = [
        PiiMatch(PHONE, 0, 8, "aaaaaaaa"),
        PiiMatch(CREDIT_CARD, 0, 16, "bbbbbbbbbbbbbbbb"),  # same start, longer → wins
        PiiMatch(EMAIL, 20, 30, "cccccccccc"),
    ]
    kept = _resolve_overlaps(matches)
    assert [m.entity_type for m in kept] == [CREDIT_CARD, EMAIL]


def test_detect_returns_sorted_non_overlapping():
    text = "call 415-555-2671 or email x@y.com"
    matches = PiiDetector().detect(text)
    starts = [m.start for m in matches]
    assert starts == sorted(starts)
    for a, b in zip(matches, matches[1:]):
        assert a.end <= b.start


# ══════════════════════════════════════════════════════════════════════════════
# PHI entities (opt-in): DEA / NPI / MRN / ICD-10 — Task 9
# ══════════════════════════════════════════════════════════════════════════════
from pathlib import Path

from guardrails.pii import (
    DEA, NPI, MRN, ICD10, PHI_ENTITIES, DEFAULT_ENTITIES,
    _dea_ok, _npi_ok,
)

_PHI_DETECTOR = PiiDetector(entities=list(PHI_ENTITIES))

# (text, expected entity) — each MUST trip. Values are format-valid:
# DEA AB1234563 (checksum 3), NPI 1234567893 (80840-Luhn valid).
_PHI_TRUE_POSITIVES = [
    ("prescriber DEA AB1234563 on the script", DEA),
    ("registration BF1234563 verified", DEA),
    ("provider NPI: 1234567893 billed", NPI),
    ("Patient MRN: 00456789 admitted", MRN),
    ("the medical record number 123456 was updated", MRN),
    ("ICD-10 code E11.9 recorded", ICD10),
    ("diagnosed with E11.9 last week", ICD10),
    ("diagnosis: I10", ICD10),
    ("patient dx J45.909 noted", ICD10),
]


def _load_false_positive_corpus():
    p = Path(__file__).resolve().parents[2] / "data" / "phi_false_positives.txt"
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


class TestPhiChecksums:
    def test_dea_checksum(self):
        assert _dea_ok("1234563") is True     # (1+3+5)+2*(2+4+6)=33 → units 3 == d7
        assert _dea_ok("1234567") is False
        assert _dea_ok("123") is False         # wrong length

    def test_npi_checksum(self):
        assert _npi_ok("1234567893") is True
        assert _npi_ok("1234567890") is False
        assert _npi_ok("123") is False


class TestPhiTruePositives:
    @pytest.mark.parametrize("text,entity", _PHI_TRUE_POSITIVES)
    def test_each_phi_form_is_detected(self, text, entity):
        found = {m.entity_type for m in _PHI_DETECTOR.detect(text)}
        assert entity in found, f"{entity} not detected in {text!r} (found {found})"


class TestPhiFalsePositiveCorpus:
    @pytest.mark.parametrize("line", _load_false_positive_corpus())
    def test_corpus_line_is_clean(self, line):
        # A detector with ALL PHI entities on must find NOTHING in a benign line.
        matches = _PHI_DETECTOR.detect(line)
        assert matches == [], f"false positive on {line!r}: {[(m.entity_type, m.text) for m in matches]}"

    def test_corpus_is_nonempty(self):
        # Guard against a silently-emptied fixture giving a vacuous pass.
        assert len(_load_false_positive_corpus()) >= 15


class TestPhiIsOptIn:
    def test_phi_excluded_from_default_entities(self):
        for e in PHI_ENTITIES:
            assert e not in DEFAULT_ENTITIES

    def test_default_detector_ignores_phi(self):
        # The default detector (existing tenants) must not start flagging PHI.
        d = PiiDetector()
        assert d.detect("DEA AB1234563 NPI: 1234567893 MRN: 00456789 dx E11.9") == []

    def test_pii_still_works_alongside_phi(self):
        # Enabling PHI must not break the existing PII entities.
        d = PiiDetector(entities=[EMAIL, US_SSN, DEA, NPI, MRN, ICD10])
        found = {m.entity_type for m in d.detect("email x@y.com ssn 123-45-6789 DEA AB1234563")}
        assert {EMAIL, US_SSN, DEA} <= found
