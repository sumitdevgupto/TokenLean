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

Design notes vs the JS original:
  * the sentinel wraps the index in NUL bytes ("\\x00{i}\\x00") rather than the JS
    " {i} " space-digit-space, so a bare number already present in the prose can never
    be mistaken for a sentinel and restored to the wrong segment (NUL never appears in
    real prompt/description text);
  * only words whose loss leaves the text saying what it said are removed. Tool
    descriptions are instructions to the model, so "make sure", modal hedges ("might
    return an empty list" is a different contract from "returns an empty list"), a
    degree word after a negation ("not just X but Y") and any word inside a hyphenated
    compound ("just-in-time") are kept;
  * nothing is recapitalised: a surviving word keeps its case, since a lowercase word
    opening a sentence may be a parameter name.
"""
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

# ─── Removal rules (from caveman-shrink, narrowed: see the design notes) ──────
# `\b` treats '-' as a boundary, so each word is bounded by "neither a word character
# nor a hyphen" instead, which leaves hyphenated compounds whole.
_FILLERS = re.compile(
    r"(?<!not )(?<!n't )(?<!n’t )(?<!never )"   # "not just" / "isn't really" stay
    r"(?<![\w-])(?:just|really|basically|actually|simply|quite|very|essentially|literally)(?![\w-])",
    re.IGNORECASE,
)
_PLEASANTRIES = re.compile(
    r"(?<![\w-])(?:please|kindly|thank you|thanks|certainly|of course|happy to|i'?d be happy)"
    r"(?![\w-])[,.]?\s*",
    re.IGNORECASE,
)
# "Sure," opening a reply is a pleasantry; "make sure" is an instruction.
_REPLY_SURE = re.compile(r"(?:^|(?<=[.!?]\s))sure[,.!]\s*", re.IGNORECASE)
# The speaker's stance only. "It appears" is left alone too: "It appears in results only
# when published" is no hedge.
_HEDGES = re.compile(
    r"(?<![\w-])(?:would like to|i think|in my opinion|it seems)(?![\w-])\s*",
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
# lookahead's [a-z] class, silently defeating that protection. Not after a hyphen either:
# "Class-A shares" is no article.
_ARTICLES = re.compile(r"(?<![\w-])(?i:a|an|the)\s+(?=[a-z])")

# ─── Protection patterns (byte-for-byte preserved) ────────────────────────────
# G01 also refuses any compression that changes a span these match (protected_segments).
# They run on client-supplied text (tool descriptions, chat history), so each pattern can
# only start at the beginning of its run (a lookbehind, a word boundary or a literal), which
# keeps them linear: the unanchored path and call patterns took about a minute on 100k
# characters of "a.a.a…" or "aaa…".
_PROTECTED_PATTERNS: List[re.Pattern] = [
    re.compile(r"(```|~~~)[\s\S]*?\1"),                             # fenced code blocks
    re.compile(r"`[^`\n]+`"),                                       # inline code
    # URLs, without trailing sentence punctuation or a closing parenthesis
    re.compile(r"\bhttps?://[^\s<>\"'`]*[^\s<>\"'`.,;:!?)]", re.IGNORECASE),
    re.compile(r"(?<![\w.-])[\w.-]*[/\\][\w./\\-]*[\w/\\-]"),        # paths (no final '.')
    re.compile(r"\b\w+\.\w+(?:\.\w+)*\(?\)?"),                      # dotted.paths / fn()
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*\([^()]*\)"),  # calls: name(args)
    re.compile(r"\b(?=\w*_)(?=\w*[A-Za-z])\w+\b"),                  # snake_case, CONST_CASE
    re.compile(r"\b(?:[a-z]+|[A-Z][a-z0-9]+)[A-Z]\w*\b"),           # camelCase, PascalCase
    re.compile(r"\b\d+\.\d+\.\d+\b"),                               # version numbers
]

_SENTINEL = "\x00"
_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")
_MAX_RESTORE_PASSES = 8


def protected_segments(text: str) -> List[str]:
    """The parts of ``text`` that must reach the model byte-for-byte (code, URLs, paths,
    identifiers, function calls, version numbers), in order: overlapping or touching
    matches are merged into one segment."""
    merged: List[List[int]] = []
    for start, end in sorted((m.start(), m.end())
                             for pat in _PROTECTED_PATTERNS for m in pat.finditer(text)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [text[start:end] for start, end in merged]


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
    s = _REPLY_SURE.sub("", s)
    s = _PLEASANTRIES.sub("", s)
    s = _HEDGES.sub("", s)
    s = _FILLERS.sub("", s)
    s = _ARTICLES.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)           # collapse runs of spaces/tabs
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)      # tighten space-before-punctuation
    s = re.sub(r"\n{3,}", "\n\n", s)            # collapse blank-line runs
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
    obj: Any, field_names: Iterable[str] = ("description",),
    accept: Optional[Callable[[str, str], bool]] = None,
) -> int:
    """Recursively compress the named string fields of a nested dict/list
    (e.g. tool/function ``description`` prose) IN PLACE. Returns the number of
    characters saved across all touched fields. ``accept(before, after)``, when given,
    vets each changed field; a refused one is left as it was."""
    fields = set(field_names)
    saved = 0
    if isinstance(obj, list):
        for item in obj:
            saved += compress_descriptions_in_place(item, fields, accept)
        return saved
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in fields and isinstance(val, str) and val:
                res = compress(val)
                if (res["compressed"] != val and accept is not None
                        and not accept(val, res["compressed"])):
                    continue
                obj[key] = res["compressed"]
                saved += max(0, res["before"] - res["after"])
            elif isinstance(val, (dict, list)):
                saved += compress_descriptions_in_place(val, fields, accept)
    return saved
