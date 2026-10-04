"""
G23 · Streaming Output Compression
Stage: After Response
Saving: none (a measurement)

Identifies high-frequency repeated n-gram patterns in the LLM response text
(e.g. repeated JSON keys, boilerplate disclaimers, duplicate list items) and
counts how many output tokens collapsing the repeats would remove.

It measures and changes nothing: the client receives the whole answer, and nothing
reuses a shorter copy on later turns. It used to put that copy in the response
(``x_compressed_content``, cached with it by G05) and book it as a saving.
"""
import logging
import re
from collections import Counter
from typing import Any, Dict, Optional, Tuple

from prometheus_client import Counter as PromCounter

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G23"

COMPRESSIBLE_OUTPUT_TOKENS = PromCounter(
    "token_opt_g23_compressible_output_tokens_total",
    "Output tokens that collapsing repeated phrases would remove (measured by G23; nothing "
    "is removed, the client receives the whole answer)",
    ["tenant_id"],
)

_NGRAM_SIZE = 5  # words per n-gram
_MIN_REPEAT = 3  # minimum repetitions to trigger compression
_MIN_WORD_LEN = 20  # skip patterns shorter than this many characters


def _tokenise(text: str):
    return re.findall(r"\b\w[\w']*\b", text.lower())


def _build_ngrams(words, n: int):
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


def _compress_text(text: str, min_repeat: int = _MIN_REPEAT, ngram_size: int = _NGRAM_SIZE) -> Tuple[str, int]:
    """Replace high-frequency repeated n-grams with a `[×N]` marker.

    Returns the compressed text and the number of characters saved.
    """
    words = _tokenise(text)
    if len(words) < ngram_size * min_repeat:
        return text, 0

    ngrams = _build_ngrams(words, ngram_size)
    freq = Counter(ngrams)
    repeated = {ng: cnt for ng, cnt in freq.items() if cnt >= min_repeat}

    if not repeated:
        return text, 0

    # Sort longest patterns first so sub-patterns don't prevent longer matches
    patterns_by_len = sorted(
        repeated.items(), key=lambda kv: len(" ".join(kv[0])), reverse=True
    )

    compressed = text
    for ng, cnt in patterns_by_len:
        phrase = " ".join(ng)
        if len(phrase) < _MIN_WORD_LEN:
            continue
        # Escape for regex
        escaped = re.escape(phrase)
        regex = re.compile(escaped, re.IGNORECASE)
        matches = list(regex.finditer(compressed))
        if len(matches) < min_repeat:
            continue
        # Keep first occurrence; replace all subsequent with marker
        first_end = matches[0].end()
        suffix = compressed[first_end:]
        suffix_compressed = regex.sub(f"[×{cnt - 1}]", suffix, count=cnt - 1)
        compressed = compressed[:first_end] + suffix_compressed

    chars_saved = len(text) - len(compressed)
    return compressed, max(0, chars_saved)


def _estimate_tokens_from_chars(char_count: int) -> int:
    """Rough 4-chars-per-token estimate (no tokeniser dependency here)."""
    return max(0, char_count // 4)


def measure_output(ctx: Any, text: Optional[str]) -> int:
    """Count, for this tenant, the output tokens collapsing ``text``'s repeated phrases
    would remove, and return that count. A measurement only: no savings step (no token was
    saved) and nothing changed. Used for both served and streamed answers."""
    cfg = ctx.config.get("groups", {}).get("G23_streaming_compression", {})
    if not cfg.get("enabled", False) or not text or not isinstance(text, str):
        return 0
    _, chars = _compress_text(text, min_repeat=cfg.get("min_repeat", _MIN_REPEAT),
                              ngram_size=cfg.get("ngram_size", _NGRAM_SIZE))
    tokens = _estimate_tokens_from_chars(chars)
    if tokens:
        COMPRESSIBLE_OUTPUT_TOKENS.labels(
            tenant_id=getattr(ctx, "tenant_id", "default") or "default").inc(tokens)
        logger.debug("[%s] G23: ~%d output tokens repeat earlier phrases",
                     getattr(ctx, "request_id", "?"), tokens)
    return tokens


class G23StreamingCompression:
    """
    Post-LLM measurement: how much of the assistant's reply repeats itself
    (``measure_output``). The response is returned exactly as it came.
    """

    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        choices = response.get("choices") or []
        if choices:
            measure_output(ctx, (choices[0].get("message") or {}).get("content"))
        return response
