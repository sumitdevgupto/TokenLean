"""
G23 · Streaming Output Compression
Stage: After Response
Saving: 10-25% on repetitive output re-used as context in subsequent turns

Identifies high-frequency repeated n-gram patterns in the LLM response text
(e.g. repeated JSON keys, boilerplate disclaimers, duplicate list items) and
collapses subsequent occurrences with a compact back-reference token.

The compressed content is stored under response["x_compressed_content"] so
the original response is unchanged for the client, but G10 memory and any
downstream agent turn can load the shorter version instead.
"""
import logging
import re
from collections import Counter
from typing import Any, Dict, Optional, Tuple

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G23"

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


class G23StreamingCompression:
    """
    Post-LLM response compression.  Operates on the assistant's reply text,
    collapses repeated n-gram blocks, and stores the compressed version as
    ``response["x_compressed_content"]`` for use by downstream memory / agents.
    """

    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = ctx.config.get("groups", {}).get("G23_streaming_compression", {})
        if not cfg.get("enabled", False):
            return response

        min_repeat = cfg.get("min_repeat", _MIN_REPEAT)
        ngram_size = cfg.get("ngram_size", _NGRAM_SIZE)

        # Extract content from first choice
        choices = response.get("choices", [])
        if not choices:
            return response

        message = choices[0].get("message") or {}
        original_content: Optional[str] = message.get("content")
        if not original_content or not isinstance(original_content, str):
            return response

        compressed, chars_saved = _compress_text(
            original_content,
            min_repeat=min_repeat,
            ngram_size=ngram_size,
        )

        if chars_saved == 0:
            return response

        tokens_saved = _estimate_tokens_from_chars(chars_saved)
        original_tokens = _estimate_tokens_from_chars(len(original_content))
        compressed_tokens = original_tokens - tokens_saved

        # Store compressed version as extension field — client always gets original
        response["x_compressed_content"] = compressed
        response["x_compression_ratio"] = round(
            len(compressed) / len(original_content), 3
        )

        ctx.savings.add_step(
            GROUP,
            f"G23: output compressed {chars_saved} chars → ~{tokens_saved} tokens saved",
            original_tokens,
            compressed_tokens,
        )

        logger.debug(
            "[%s] G23 compressed %d → %d chars (%d tokens saved)",
            ctx.request_id,
            len(original_content),
            len(compressed),
            tokens_saved,
        )

        return response
