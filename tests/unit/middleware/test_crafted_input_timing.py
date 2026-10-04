"""Patterns that run on client text inside the event loop must take linear time.

A pattern where two quantifiers can split the same run of characters backtracks over every
split once the rest fails to match: `^\\s+` under MULTILINE spans the newlines that `^`
starts on, and `\\s*\\{?\\s*` splits one run of spaces two ways. Both took seconds on the
crafted inputs below and grew with the square of the length, stalling every request on the
worker. The one-second bound is generous so the test measures the shape, not the machine.
"""
import pathlib
import sys
import time

import pytest

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

from middleware.g01_compression import _LOG_ERROR_PATTERNS, _is_log_error_content  # noqa: E402
from middleware.g19_headroom import _fence_language  # noqa: E402


def _quick(fn, *args) -> bool:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start < 1.0


class TestG01LogDetector:
    """G01 runs these on assistant history whenever LLMLingua does not shorten a message."""

    @pytest.mark.parametrize("text", [
        "\n" * 60_000, " \n" * 30_000, "\t\n" * 30_000, "\n\t" * 30_000, " " * 60_000,
        "  at " + "a." * 30_000, "\tat a(" + "b." * 30_000, "00:00:00." + "1" * 60_000,
        "[" * 60_000, "2026-09-28 " * 6_000,
    ], ids=lambda t: repr(t[:10]))
    def test_every_pattern_is_quick_on_crafted_input(self, text):
        for pattern in _LOG_ERROR_PATTERNS:
            assert _quick(pattern.search, text), pattern.pattern

    @pytest.mark.parametrize("text", [
        "java.lang.NullPointerException\n\tat com.acme.Foo.bar(Foo.java:42)",
        "Exception in thread main\n    at com.acme.Foo.bar(Foo.java:42)",
        "2026-09-28 10:00:00 worker started",
        "[ERROR] connection refused",
        'Traceback (most recent call last):\n  File "x.py", line 1',
        "10:00:00.123 [main] INFO started",
    ])
    def test_real_log_output_is_still_detected(self, text):
        assert _is_log_error_content(text) is True

    def test_a_stack_frame_is_indented_on_its_own_line(self):
        """The old `^\\s+` let the 'indentation' be a blank line above an unindented line."""
        assert _LOG_ERROR_PATTERNS[3].search("text\n\nat com.acme.X.y(Y.java:1)") is None


class TestG19FenceLanguage:
    """G19 reads the language of every fence it compresses, in any role's content."""

    @pytest.mark.parametrize("line, language", [
        ("```python", "python"), ("``` python", "python"), ("```{python}", "python"),
        ("``` { python", "python"), ("  ```js", "js"), ("````TS", "ts"), ("```c++", "c++"),
        ("```", ""), ("```   ", ""), ("text", ""),
    ])
    def test_the_language_is_read(self, line, language):
        assert _fence_language(line) == language

    @pytest.mark.parametrize("line", [
        "```" + " " * 60_000 + "!", "```" + "\t" * 60_000 + "!", "```{" + " " * 60_000 + "!",
        "```" + " " * 30_000 + "{" + " " * 30_000 + "!", " " * 60_000 + "!",
        "`" * 60_000 + "!",
    ], ids=lambda t: repr(t[:6]))
    def test_it_is_quick_on_crafted_input(self, line):
        assert _quick(_fence_language, line)
