"""PII detection + masking engine (G29).

Pure stdlib regex tier plus an optional Microsoft Presidio backend. The regex tier
is deliberately **precision-biased** — it prefers to miss an ambiguous match over
flagging a false positive, because a false positive on the request path silently
corrupts a legitimate prompt (masking a 9-digit order number that merely *looks*
like an SSN would change the answer). Recall is raised by enabling the Presidio
backend, not by loosening the regexes.

Design notes
------------
* **US SSN** is matched only in its separated forms (``123-45-6789`` /
  ``123 45 6789``), never as a bare 9-digit run — a bare 9-digit number is far more
  often an order id / account number than an SSN, and masking it would be a
  correctness regression. Enable Presidio for context-aware bare-SSN detection.
* **Credit cards** are Luhn-validated after separator stripping, so a random
  16-digit string that fails Luhn is not flagged.
* Masking is **reversible**: :func:`mask_matches` returns a vault mapping each
  placeholder token to the original span so a downstream answer that echoes the
  placeholder can be rehydrated with :func:`unmask_text`. The vault never leaves
  the request context and is never logged or audited.

The matched **text** lives only inside :class:`PiiMatch` / the vault for the
lifetime of the request; audit rows and metrics carry entity *types* and counts
only (see G29 middleware), so no PII is ever persisted by this feature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ── Entity type constants (stable strings — used as metric/audit labels) ───────
EMAIL = "EMAIL"
US_SSN = "US_SSN"
CREDIT_CARD = "CREDIT_CARD"
PHONE = "PHONE"
IP_ADDRESS = "IP_ADDRESS"

# ── PHI (health) entity types — OPT-IN, never in DEFAULT_ENTITIES ──────────────
# These are enabled only when a tenant lists them (or the `phi` shortcut) in the
# G29 `entities` config, so existing tenants' behaviour is unchanged. Each is
# precision-biased the same way the PII regexes are — DEA/NPI are checksum-gated,
# MRN/ICD-10 require an explicit medical context cue (a bare number/dotted code is
# far more often an order id / version string than a health identifier).
DEA = "DEA"                    # US DEA registration number (2 letters + 7 digits, checksummed)
NPI = "NPI"                    # US National Provider Identifier (10 digits, checksummed)
MRN = "MRN"                    # Medical Record Number (context-required)
ICD10 = "ICD10"               # ICD-10 diagnosis code (context-required)

PHI_ENTITIES: Tuple[str, ...] = (DEA, NPI, MRN, ICD10)

# Default set scanned when config doesn't narrow it. PHI is deliberately EXCLUDED
# (opt-in). Ordered by descending span length preference is handled at
# overlap-resolution time, not here.
DEFAULT_ENTITIES: Tuple[str, ...] = (EMAIL, US_SSN, CREDIT_CARD, PHONE, IP_ADDRESS)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# SSN: 3-2-4 with '-' or single-space separators. Reject the obviously-invalid area
# numbers (000, 666, 900-999) and group/serial all-zero blocks so we don't mask an
# arbitrary dashed number. Bare \d{9} intentionally NOT matched (see module docs).
_SSN_RE = re.compile(
    r"\b(?!000|666|9\d\d)\d{3}([ -])(?!00)\d{2}\1(?!0000)\d{4}\b"
)

# Candidate card runs: 13-19 digits allowing space/hyphen groupings. Luhn-validated
# below before a match is emitted, which is what rejects non-card digit strings.
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# North-American phone numbers with common separators and an optional +1/1 prefix.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?!\d)"
)

# IPv4 with each octet 0-255. (IPv6 handled by Presidio when enabled.)
_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)

# ── PHI regexes ───────────────────────────────────────────────────────────────
# DEA: 2 letters + 7 digits, validated by the DEA checksum below (which is what
# gives precision — a random 2-letter+7-digit string is very unlikely to pass).
_DEA_CANDIDATE_RE = re.compile(r"\b([A-Za-z])([A-Za-z])(\d{7})\b")


def _dea_ok(digits: str) -> bool:
    """DEA checksum: (d1+d3+d5) + 2*(d2+d4+d6) has units digit == d7.

    An all-zero serial passes the raw checksum (0 == 0) but is never a real DEA
    number — and would false-positive on benign ids like 'XY0000000'. Reject it."""
    if len(digits) != 7 or digits == "0000000":
        return False
    d = [ord(c) - 48 for c in digits]
    checksum = (d[0] + d[2] + d[4]) + 2 * (d[1] + d[3] + d[5])
    return checksum % 10 == d[6]


# NPI: 10 digits, but ONLY when preceded by an explicit "NPI" cue — a bare 10-digit
# run is far more often a phone/order/account id, and ~10% would pass the checksum by
# chance, so the cue is what keeps precision high. The captured group (the number) is
# the emitted span.
_NPI_RE = re.compile(r"\bNPI\b[\s:#]*(\d{10})\b", re.IGNORECASE)


def _npi_ok(npi: str) -> bool:
    """NPI check: Luhn over the ISO-prefixed '80840' + the 10-digit NPI is valid."""
    return len(npi) == 10 and _luhn_ok("80840" + npi)


# MRN: no universal format, so it is matched ONLY behind an explicit label
# ("MRN" / "medical record number") — a bare 5-12 char id has no distinguishing
# feature and would false-positive on order/account numbers. The captured id is the
# emitted span (not the label). `\b` after the phrase avoids matching "medical records".
_MRN_RE = re.compile(
    r"\b(?:MRN|medical\s+record(?:\s+(?:no\.?|number|#))?)\b[\s:#]*([A-Za-z0-9]{5,12})\b",
    re.IGNORECASE,
)

# ICD-10: a diagnosis-code shape (letter, 2 digits, optional dotted subcode) is far
# too common on its own ("Section B20.1", "Model E11.9", "clause I10"), so a code is
# only treated as PHI when a medical cue ("ICD", "diagnosis/diagnosed", "dx") appears
# within a short window BEFORE it. Precision comes from the cue, not the code shape.
_ICD_CODE_RE = re.compile(r"\b[A-Za-z]\d[0-9AaBb](?:\.[0-9A-Za-z]{1,4})?\b")
_ICD_CUE_RE = re.compile(r"(?:ICD(?:[-\s]?10)?|diagnos(?:is|ed|es|e)?|dx)\b", re.IGNORECASE)
_ICD_CUE_WINDOW = 30  # chars before the code to look back for a cue


@dataclass(frozen=True)
class PiiMatch:
    """One detected PII span. ``text`` is internal-only (never logged/audited)."""

    entity_type: str
    start: int
    end: int
    text: str


@dataclass
class RedactionResult:
    """Outcome of masking a single string."""

    text: str                                   # masked (or original, if nothing matched)
    matches: List[PiiMatch] = field(default_factory=list)
    vault: Dict[str, str] = field(default_factory=dict)  # placeholder → original span

    @property
    def count(self) -> int:
        return len(self.matches)

    @property
    def entity_types(self) -> List[str]:
        """Distinct entity types found, sorted — safe to log/audit (no raw text)."""
        return sorted({m.entity_type for m in self.matches})


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over a pure-digit string."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class PiiDetector:
    """Regex PII detector with an optional Presidio backend.

    Parameters
    ----------
    entities:
        Which entity types to scan for. ``None`` → :data:`DEFAULT_ENTITIES`.
    use_presidio:
        When True *and* ``presidio-analyzer`` is importable, augment the regex
        matches with Presidio's recognisers (context-aware, higher recall). Any
        import/analyze failure silently falls back to the regex tier — the engine
        must never break the request path.
    """

    def __init__(
        self,
        entities: Optional[Sequence[str]] = None,
        *,
        use_presidio: bool = False,
        min_card_len: int = 13,
    ) -> None:
        self.entities = tuple(entities) if entities else DEFAULT_ENTITIES
        self.min_card_len = min_card_len
        self._presidio = _build_presidio_analyzer() if use_presidio else None

    def detect(self, text: str) -> List[PiiMatch]:
        """Return non-overlapping PII spans in ``text``, left to right.

        Overlaps are resolved in favour of the longer span (so a card number is
        preferred over a phone-shaped substring of it)."""
        if not text or not isinstance(text, str):
            return []
        raw: List[PiiMatch] = []
        if EMAIL in self.entities:
            raw += [PiiMatch(EMAIL, m.start(), m.end(), m.group()) for m in _EMAIL_RE.finditer(text)]
        if US_SSN in self.entities:
            raw += [PiiMatch(US_SSN, m.start(), m.end(), m.group()) for m in _SSN_RE.finditer(text)]
        if CREDIT_CARD in self.entities:
            for m in _CARD_CANDIDATE_RE.finditer(text):
                digits = re.sub(r"\D", "", m.group())
                if self.min_card_len <= len(digits) <= 19 and _luhn_ok(digits):
                    raw.append(PiiMatch(CREDIT_CARD, m.start(), m.end(), m.group()))
        if PHONE in self.entities:
            raw += [PiiMatch(PHONE, m.start(), m.end(), m.group()) for m in _PHONE_RE.finditer(text)]
        if IP_ADDRESS in self.entities:
            raw += [PiiMatch(IP_ADDRESS, m.start(), m.end(), m.group()) for m in _IP_RE.finditer(text)]
        if DEA in self.entities:
            for m in _DEA_CANDIDATE_RE.finditer(text):
                if _dea_ok(m.group(3)):
                    raw.append(PiiMatch(DEA, m.start(), m.end(), m.group()))
        if NPI in self.entities:
            for m in _NPI_RE.finditer(text):
                if _npi_ok(m.group(1)):
                    raw.append(PiiMatch(NPI, m.start(1), m.end(1), m.group(1)))
        if MRN in self.entities:
            raw += [PiiMatch(MRN, m.start(1), m.end(1), m.group(1)) for m in _MRN_RE.finditer(text)]
        if ICD10 in self.entities:
            for m in _ICD_CODE_RE.finditer(text):
                window = text[max(0, m.start() - _ICD_CUE_WINDOW):m.start()]
                if _ICD_CUE_RE.search(window):
                    raw.append(PiiMatch(ICD10, m.start(), m.end(), m.group()))

        if self._presidio is not None:
            raw += self._presidio_matches(text)

        return _resolve_overlaps(raw)

    def _presidio_matches(self, text: str) -> List[PiiMatch]:
        try:
            results = self._presidio.analyze(text=text, language="en")
        except Exception:
            return []
        out: List[PiiMatch] = []
        for r in results:
            etype = _PRESIDIO_TYPE_MAP.get(getattr(r, "entity_type", ""), getattr(r, "entity_type", "PII"))
            try:
                out.append(PiiMatch(etype, r.start, r.end, text[r.start:r.end]))
            except Exception:
                continue
        return out


def _resolve_overlaps(matches: List[PiiMatch]) -> List[PiiMatch]:
    """Sort by start, then greedily drop any match overlapping one already kept,
    preferring the longer span when two share a start."""
    if not matches:
        return []
    ordered = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    kept: List[PiiMatch] = []
    last_end = -1
    for m in ordered:
        if m.start >= last_end:
            kept.append(m)
            last_end = m.end
    return kept


def mask_matches(
    text: str,
    matches: Sequence[PiiMatch],
    *,
    reversible: bool = True,
    placeholder_prefix: str = "PII",
) -> RedactionResult:
    """Replace each match in ``text`` with a typed placeholder.

    With ``reversible=True`` each placeholder is unique (``[PII:EMAIL:1]``) and the
    returned vault maps it back to the original span so :func:`unmask_text` can
    restore a downstream answer that quotes the placeholder. With
    ``reversible=False`` the placeholder is un-numbered (``[EMAIL]``) and the vault
    is empty (irreversible masking)."""
    ordered = sorted(matches, key=lambda m: m.start)
    if not ordered:
        return RedactionResult(text=text, matches=[], vault={})
    out: List[str] = []
    vault: Dict[str, str] = {}
    cursor = 0
    counters: Dict[str, int] = {}
    for m in ordered:
        if m.start < cursor:  # defensive: skip overlaps the caller didn't resolve
            continue
        out.append(text[cursor:m.start])
        if reversible:
            counters[m.entity_type] = counters.get(m.entity_type, 0) + 1
            token = f"[{placeholder_prefix}:{m.entity_type}:{counters[m.entity_type]}]"
            vault[token] = m.text
        else:
            token = f"[{m.entity_type}]"
        out.append(token)
        cursor = m.end
    out.append(text[cursor:])
    return RedactionResult(text="".join(out), matches=list(ordered), vault=vault)


def unmask_text(text: str, vault: Dict[str, str]) -> str:
    """Restore placeholders produced by :func:`mask_matches` (reversible mode).

    Longest tokens first so ``[PII:EMAIL:11]`` is restored before ``[PII:EMAIL:1]``.
    """
    if not vault or not text:
        return text
    for token in sorted(vault, key=len, reverse=True):
        text = text.replace(token, vault[token])
    return text


def remask_with_vault(text: str, vault: Dict[str, str]) -> str:
    """Inverse of :func:`unmask_text` — put each placeholder back in place of its
    original span. Used to keep restored PII out of a persisted trace/log after the
    client-facing response has already been un-masked (the client keeps the real
    values; the trace keeps placeholders). Longest originals first so a short value
    isn't masked inside a longer one that contains it."""
    if not vault or not text:
        return text
    for placeholder, original in sorted(vault.items(), key=lambda kv: len(kv[1]), reverse=True):
        if original:
            text = text.replace(original, placeholder)
    return text


# ── Optional Presidio backend ─────────────────────────────────────────────────
_PRESIDIO_TYPE_MAP = {
    "EMAIL_ADDRESS": EMAIL,
    "US_SSN": US_SSN,
    "CREDIT_CARD": CREDIT_CARD,
    "PHONE_NUMBER": PHONE,
    "IP_ADDRESS": IP_ADDRESS,
}


def _build_presidio_analyzer():
    """Return a Presidio AnalyzerEngine, or None if the dep is unavailable.

    Imported lazily + defensively: presidio-analyzer is an optional dependency
    (see requirements.txt optional block). A missing package must degrade to the
    regex tier, never raise on the request path."""
    try:  # pragma: no cover - exercised only when the optional dep is installed
        from presidio_analyzer import AnalyzerEngine  # type: ignore

        return AnalyzerEngine()
    except Exception:
        return None
