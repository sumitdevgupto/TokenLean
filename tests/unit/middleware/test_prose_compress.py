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

    def test_preexisting_nul_sentinel_lookalike_stripped_not_confused(self):
        # A pre-existing "\x00<digits>\x00" sequence in the INPUT (reachable via an
        # ordinary JSON unicode escape for codepoint zero) must never be treated as a
        # real sentinel and substituted with unrelated protected content from
        # elsewhere in the string.
        text = "Please fetch \x000\x00 from https://secret-internal.example.com/leak really now."
        out = compress_text(text)
        assert "\x00" not in out
        # The real protected URL is preserved exactly once — not duplicated into
        # the position where the fake sentinel sat.
        assert out.count("https://secret-internal.example.com/leak") == 1

    def test_bare_nul_bytes_stripped(self):
        out = compress_text("abc\x00123\x00def")
        assert "\x00" not in out


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

    def test_article_before_protected_identifier_kept(self):
        # "the" survives because MAX_RETRIES is stashed as a protected CONST_CASE
        # segment BEFORE _ARTICLES ever runs — NOT because the lookahead is
        # case-sensitive (see test_article_before_unprotected_uppercase_kept below
        # for a direct test of the lookahead itself).
        out = compress_text("Set the MAX_RETRIES value.")
        assert "the MAX_RETRIES" in out

    def test_article_before_unprotected_uppercase_kept(self):
        # A bare capitalized word with no underscore (not CONST_CASE-protected) must
        # still keep its article — the _ARTICLES lookahead's [a-z] must stay
        # case-sensitive even though the alternation itself is scoped-IGNORECASE.
        out = compress_text("Fix the API now.")
        assert "the API" in out

    def test_capitalized_article_at_sentence_start_still_stripped(self):
        # The scoped (?i:...) must still catch "The"/"An" (capitalized alternation)
        # when the FOLLOWING word is lowercase — only the lookahead is case-sensitive.
        out = compress_text("The bug is bad.")
        assert not out.lower().startswith("the bug")


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
