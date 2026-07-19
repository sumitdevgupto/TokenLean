"""Unit tests for the deterministic prose compressor (prose_compress.py).

Ported from caveman-shrink (MIT). The load-bearing invariant is PROTECTION:
code, URLs, paths, identifiers, function calls and version numbers must survive
byte-for-byte. Everything else is best-effort filler removal.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from middleware.prose_compress import (
    compress,
    compress_text,
    compress_descriptions_in_place,
)


class TestProtectionInvariants:
    def test_fenced_code_block_preserved(self):
        text = "Here is the code:\n```python\ndef foo():\n    return the a value\n```\nDone."
        out = compress_text(text)
        assert "```python\ndef foo():\n    return the a value\n```" in out

    def test_inline_code_preserved(self):
        out = compress_text("Just call `the_function(a, an, the)` really now.")
        assert "`the_function(a, an, the)`" in out

    def test_url_preserved(self):
        out = compress_text("Please fetch https://api.example.com/v1/the/thing?x=1 now.")
        assert "https://api.example.com/v1/the/thing?x=1" in out

    def test_path_preserved(self):
        out = compress_text("The config is at /etc/app/the-config.yaml basically.")
        assert "/etc/app/the-config.yaml" in out

    def test_windows_path_preserved(self):
        out = compress_text("Open C:\\Users\\the_user\\config.json simply.")
        assert "C:\\Users\\the_user\\config.json" in out

    def test_const_case_identifier_preserved(self):
        out = compress_text("Set the MAX_RETRY_COUNT constant really high.")
        assert "MAX_RETRY_COUNT" in out

    def test_function_call_preserved(self):
        out = compress_text("You should just call getUser(id, name) now.")
        assert "getUser(id, name)" in out

    def test_dotted_path_preserved(self):
        out = compress_text("Use the config.database.host value basically.")
        assert "config.database.host" in out

    def test_version_number_preserved(self):
        out = compress_text("Upgrade to version 1.2.3 really soon please.")
        assert "1.2.3" in out

    def test_no_sentinel_leaks(self):
        text = "Call `foo()` at https://x.io/y and set THE_CONST to 1.2.3 basically."
        out = compress_text(text)
        assert "\x00" not in out  # NUL sentinel fully restored


class TestFillerRemoval:
    def test_removes_fillers(self):
        out = compress_text("This is really just a very simple basically test.")
        for w in ("really", "just", "very", "basically"):
            assert w not in out.lower().split()

    def test_removes_pleasantries(self):
        out = compress_text("Sure, thanks. Please run the tests.")
        assert "please" not in out.lower()
        assert "thanks" not in out.lower()

    def test_removes_leader_phrase(self):
        # "I'll " at line start is a leader phrase → stripped
        out = compress_text("I'll fix the bug now.")
        assert not out.lower().startswith("i'll")

    def test_article_before_lowercase_removed(self):
        out = compress_text("Fix the bug in the handler.")
        assert " the " not in f" {out.lower()} "

    def test_article_before_uppercase_kept(self):
        # "the" before an UPPERCASE identifier is kept (ARTICLES lookahead is lowercase-only)
        out = compress_text("Set the MAX_RETRIES value.")
        assert "the MAX_RETRIES" in out


class TestBehaviourContract:
    def test_empty_and_non_string_passthrough(self):
        assert compress("")["compressed"] == ""
        assert compress(None)["compressed"] is None
        assert compress(123)["compressed"] == 123

    def test_reports_char_counts(self):
        r = compress("This is really just a basically verbose sentence.")
        assert r["before"] == len("This is really just a basically verbose sentence.")
        assert r["after"] <= r["before"]

    def test_compression_reduces_prose(self):
        r = compress("I'll basically just really simply explain the whole thing to you.")
        assert r["after"] < r["before"]

    def test_idempotent(self):
        text = "You should really just run the `tests` before you push to the main branch."
        once = compress_text(text)
        twice = compress_text(once)
        assert once == twice  # second pass changes nothing (no fillers left)

    def test_deterministic(self):
        text = "Please kindly review the config at /etc/x.yaml and call setup()."
        assert compress_text(text) == compress_text(text)

    def test_pure_code_prose_free_unchanged(self):
        # A message that is ONLY protected content must come back byte-identical.
        text = "`getUser()` https://x.io/a /etc/y.yaml MAX_N 1.2.3"
        assert compress_text(text) == text


class TestDescriptionCompression:
    def test_compresses_nested_descriptions(self):
        tools = [
            {"type": "function", "function": {
                "name": "get_weather",
                "description": "This function will really just fetch the current weather.",
                "parameters": {"type": "object"},
            }},
        ]
        saved = compress_descriptions_in_place(tools)
        assert saved > 0
        desc = tools[0]["function"]["description"]
        assert "really" not in desc.lower()
        assert "get_weather" == tools[0]["function"]["name"]  # name untouched

    def test_custom_fields(self):
        obj = {"instructions": "Please just do the thing.", "note": "keep me"}
        saved = compress_descriptions_in_place(obj, ("instructions",))
        assert saved > 0
        assert obj["note"] == "keep me"  # non-listed field untouched

    def test_no_descriptions_returns_zero(self):
        obj = {"name": "x", "value": 3}
        assert compress_descriptions_in_place(obj) == 0
