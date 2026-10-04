"""Unit tests for the deterministic prose compressor (prose_compress.py).

Ported from caveman-shrink (MIT). The load-bearing invariant is PROTECTION:
code, URLs, paths, identifiers, function calls and version numbers must survive
byte-for-byte. Everything else is best-effort filler removal.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import re
import time

import pytest

from middleware.prose_compress import (
    compress,
    compress_text,
    compress_descriptions_in_place,
    protected_segments,
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

    def test_a_refused_compression_keeps_the_original_and_saves_nothing(self):
        obj = {"description": "Please just do the thing.",
               "parameters": {"properties": {"q": {"description": "Really the query."}}}}
        seen = []

        def refuse(before, after):
            seen.append((before, after))
            return False

        assert compress_descriptions_in_place(obj, accept=refuse) == 0
        assert obj["description"] == "Please just do the thing."
        assert obj["parameters"]["properties"]["q"]["description"] == "Really the query."
        assert [b for b, _ in seen] == ["Please just do the thing.", "Really the query."]

    def test_an_accepted_compression_is_kept(self):
        obj = {"description": "Please just do the thing."}
        assert compress_descriptions_in_place(obj, accept=lambda b, a: True) > 0
        assert "just" not in obj["description"]


class TestMeaningBearingWordsKept:
    """Tool descriptions are instructions to the model (G08 compresses them by default), so the
    compressor drops only words whose loss leaves the instruction as it was."""

    def test_the_reported_description_keeps_its_instructions(self):
        out = compress_text("Make sure the date is ISO-8601. This might return an empty list. "
                            "Uses just-in-time lookup.")
        assert "Make sure" in out and "might return" in out and "just-in-time" in out

    def test_sure_opening_a_reply_is_still_dropped(self):
        out = compress_text("Sure, here is the list. Sure! Done.")
        assert "sure" not in out.lower()

    @pytest.mark.parametrize("text, kept", [
        ("Make sure the date is set.", "Make sure"),
        ("I am not sure, so check the logs.", "not sure,"),
        ("Be sure! It deletes data.", "Be sure!"),
    ])
    def test_sure_inside_a_sentence_is_kept(self, text, kept):
        assert kept in compress_text(text)

    @pytest.mark.parametrize("text, kept", [
        ("This might return an empty list.", "might"),
        ("The field is maybe empty.", "maybe"),
        ("The call perhaps times out.", "perhaps"),
        ("This could potentially delete files.", "could potentially"),
        ("It appears in results only when published.", "It appears"),
    ])
    def test_modal_hedges_are_kept(self, text, kept):
        assert kept in compress_text(text)

    @pytest.mark.parametrize("compound", [
        "just-in-time", "very-high-priority", "really-long", "sure-fire", "please-wait",
        "thank-you", "maybe-null", "the-end", "Class-A", "not-quite", "no-thanks",
        "in my opinion-piece"])
    def test_a_hyphenated_compound_is_kept_whole(self, compound):
        assert compound in compress_text(f"Uses {compound} handling.")

    @pytest.mark.parametrize("text, kept", [
        ("Returns not just the IDs but the full records.", "not just"),
        ("You cannot just delete the file.", "cannot just"),
        ("This isn't really required.", "isn't really"),
        ("This isn’t simply a cache.", "isn’t simply"),
        ("It is not quite sorted.", "not quite"),
        ("Order is never really guaranteed.", "never really"),
    ])
    def test_a_degree_word_after_a_negation_is_kept(self, text, kept):
        assert kept in compress_text(text)

    @pytest.mark.parametrize("text", [
        "Optional. limit caps the rows.",
        "The limit parameter caps the rows.",
        "I'll fix the bug. Please call the cleanup tool.",
        "Basically, query is the search string.",
    ])
    def test_no_surviving_word_changes_case(self, text):
        words = set(re.findall(r"\w+", text))
        assert set(re.findall(r"\w+", compress_text(text))) <= words


class TestTildeFences:
    def test_tilde_fenced_code_block_preserved(self):
        text = "Here is the code:\n~~~python\ndef foo():\n    return the a value\n~~~\nDone."
        assert "~~~python\ndef foo():\n    return the a value\n~~~" in compress_text(text)


class TestProtectedSegments:
    """What G01 requires every compression to keep unchanged, in order."""

    def test_overlapping_matches_merge_into_one_segment_in_order(self):
        text = ('call fetch_user(user_id) then requests.get("https://api.acme.io/v2/users") '
                "and check /etc/app/x.yaml.")
        assert protected_segments(text) == [
            "fetch_user(user_id)", 'requests.get("https://api.acme.io/v2/users")',
            "/etc/app/x.yaml"]

    @pytest.mark.parametrize("text, segment", [
        ("Set the user_id field.", "user_id"),
        ("Set MAX_RETRY_COUNT high.", "MAX_RETRY_COUNT"),
        ("Call fetchUser now.", "fetchUser"),
        ("Use the UserService class.", "UserService"),
        ("Run `make deploy` now.", "`make deploy`"),
        ("See https://acme.io/docs.", "https://acme.io/docs"),
        ("Upgrade to 1.2.3 soon.", "1.2.3"),
        ("Read config.database.host now.", "config.database.host"),
    ])
    def test_each_kind_is_found(self, text, segment):
        assert protected_segments(text) == [segment]

    @pytest.mark.parametrize("text", [
        "The report (see the appendix) covers every region.",
        "Fix the API and the URL today.",
        "Call me now, or later this week.",
    ])
    def test_plain_prose_has_none(self, text):
        assert protected_segments(text) == []


class TestPatternsAreLinear:
    """The patterns run on client-supplied text (tool descriptions via G08, chat history via
    G01), inside the event loop. Unanchored, the path and call patterns took about five
    seconds on these 30k-character inputs (and grow with the square of the length); each
    must now start only at the beginning of its run."""

    @pytest.mark.parametrize("text", [
        "a." * 15_000, "a" * 30_000, "a-" * 15_000, "f(" * 15_000, "a_" * 15_000,
        "aB" * 15_000, "https://" + "." * 30_000, "`" + "a" * 30_000, "```" + "a" * 30_000,
        "~~~" + "a" * 30_000, "1." * 15_000, "a/" * 15_000, "a " * 15_000,
    ], ids=lambda t: repr(t[:8]))
    def test_a_long_crafted_input_is_quick(self, text):
        start = time.perf_counter()
        protected_segments(text)
        compress_text(text)
        assert time.perf_counter() - start < 1.0
