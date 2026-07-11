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

# Default set scanned when config doesn't narrow it. Ordered by descending span
# length preference is handled at overlap-resolution time, not here.
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
