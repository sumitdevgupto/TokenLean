"""
Deterministic prose compressor — zero-LLM, zero-latency, regex-only.

Ported from caveman-shrink (`src/mcp-servers/caveman-shrink/compress.js`,
github.com/JuliusBrussee/caveman, MIT — attribution in docs/oss-licenses.md).
Strips articles / fillers / pleasantries / hedges / leader phrases from prose
while protecting code, URLs, paths, identifiers, function calls and version
numbers byte-for-byte via sentinel substitution.

Used by:
  * G08 — compress tool/function `description` prose (manifests ride every
    agentic request; descriptions are otherwise passed verbatim).
  * G01 — deterministic fast-path when the LLMLingua sidecar is unavailable.
  * scripts/compress_prompts.py — offline memory/template compression.

Design note vs the JS original: the sentinel wraps the index in NUL bytes
("\\x00{i}\\x00") rather than the JS " {i} " space-digit-space, so a bare number
already present in the prose can never be mistaken for a sentinel and restored to
the wrong segment (NUL never appears in real prompt/description text). The
algorithm is otherwise faithful to compress.js.
"""
import re
from typing import Any, Dict, Iterable, List, Optional

# ─── Removal rules (verbatim from caveman-shrink) ─────────────────────────────
_FILLERS = re.compile(
    r"\b(?:just|really|basically|actually|simply|quite|very|essentially|literally)\b",
    re.IGNORECASE,
)
_PLEASANTRIES = re.compile(
    r"\b(?:please|kindly|thank you|thanks|sure|certainly|of course|happy to|i'?d be happy)\b[,.]?\s*",
    re.IGNORECASE,
)
_HEDGES = re.compile(
    r"\b(?:perhaps|maybe|might|could potentially|would like to|i think|in my opinion|it seems|it appears)\b\s*",
    re.IGNORECASE,
)
_LEADERS = re.compile(
    r"^(?:i'?ll|i will|i can|i'?d|you can|we will|we can|let me|let'?s)\s+",
    re.IGNORECASE | re.MULTILINE,
)
# Scoped (?i:...) so ONLY the alternation is case-insensitive (matches "A"/"The" at a
# sentence start too) — the trailing lookahead stays case-SENSITIVE lowercase-only, so
# an article before a genuinely-capitalized unprotected word (e.g. "the API") is kept.
# A bare top-level re.IGNORECASE would apply to the whole pattern including the
# lookahead's [a-z] class, silently defeating that protection.
_ARTICLES = re.compile(r"\b(?i:a|an|the)\s+(?=[a-z])")

# ─── Protection patterns (byte-for-byte preserved) ────────────────────────────
_PROTECTED_PATTERNS: List[re.Pattern] = [
    re.compile(r"```[\s\S]*?```"),                               # fenced code blocks
    re.compile(r"`[^`\n]+`"),                                    # inline code
    re.compile(r"\bhttps?://\S+", re.IGNORECASE),               # URLs
    re.compile(r"\b[\w.-]*[/\\][\w./\\-]+"),                     # filesystem paths
    re.compile(r"\b[A-Z][A-Za-z0-9]*(?:_[A-Z][A-Za-z0-9]*)+\b"),  # CONST_CASE / snake mixes
    re.compile(r"\b\w+\.\w+(?:\.\w+)*\(?\)?"),                   # dotted.paths / fn()
    re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)"),           # function calls
    re.compile(r"\b\d+\.\d+\.\d+\b"),                            # version numbers
]

_SENTINEL = "\x00"
_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")
_MAX_RESTORE_PASSES = 8

_CAP_RE = re.compile(r"(^|[.!?]\s+)([a-z])")


def _with_protected_segments(text: str, transform) -> str:
    """Run ``transform`` over ``text`` with protected spans swapped out for
    sentinels and restored afterwards (up to 8 passes for nested protection)."""
    segments: List[str] = []

    def _stash(match: "re.Match") -> str:
        segments.append(match.group(0))
        return f"{_SENTINEL}{len(segments) - 1}{_SENTINEL}"

    working = text
    for pat in _PROTECTED_PATTERNS:
        working = pat.sub(_stash, working)

    out = transform(working)

    def _restore(match: "re.Match") -> str:
        idx = int(match.group(1))
        return segments[idx] if idx < len(segments) else match.group(0)

    for _ in range(_MAX_RESTORE_PASSES):
        if not _SENTINEL_RE.search(out):
            break
        out = _SENTINEL_RE.sub(_restore, out)
    return out


def _compress_prose(text: str) -> str:
    s = text
    s = _LEADERS.sub("", s)
    s = _PLEASANTRIES.sub("", s)
    s = _HEDGES.sub("", s)
    s = _FILLERS.sub("", s)
    s = _ARTICLES.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)           # collapse runs of spaces/tabs
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)      # tighten space-before-punctuation
    s = re.sub(r"\n{3,}", "\n\n", s)            # collapse blank-line runs
    s = _CAP_RE.sub(lambda m: m.group(1) + m.group(2).upper(), s)  # recapitalise
    return s.strip()


def compress(text: Optional[str]) -> Dict[str, Any]:
    """Compress a prose string. Returns ``{"compressed", "before", "after"}``
    (char counts) so callers can measure impact. Non-strings pass through."""
    if not isinstance(text, str) or len(text) == 0:
        return {"compressed": text, "before": 0, "after": 0}
    before = len(text)
    # Strip any pre-existing NUL bytes first: the protection mechanism below uses NUL
    # as its sentinel delimiter, and NUL has no legitimate place in prompt/description
    # text. A NUL surviving from the caller's input (reachable via an ordinary JSON
    # unicode escape for codepoint zero -- nothing upstream sanitizes it) could
    # otherwise collide with a real sentinel and substitute unrelated protected
    # content, or leak a raw sentinel into the LLM-bound text.
    text = text.replace(_SENTINEL, "")
    compressed = _with_protected_segments(text, _compress_prose)
    return {"compressed": compressed, "before": before, "after": len(compressed)}


def compress_text(text: Optional[str]) -> str:
    """Convenience wrapper returning just the compressed string."""
    return compress(text)["compressed"]


def compress_descriptions_in_place(
    obj: Any, field_names: Iterable[str] = ("description",)
) -> int:
    """Recursively compress the named string fields of a nested dict/list
    (e.g. tool/function ``description`` prose) IN PLACE. Returns the number of
    characters saved across all touched fields."""
    fields = set(field_names)
    saved = 0
    if isinstance(obj, list):
        for item in obj:
            saved += compress_descriptions_in_place(item, fields)
        return saved
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in fields and isinstance(val, str) and val:
                res = compress(val)
                obj[key] = res["compressed"]
                saved += max(0, res["before"] - res["after"])
            elif isinstance(val, (dict, list)):
                saved += compress_descriptions_in_place(val, fields)
    return saved
