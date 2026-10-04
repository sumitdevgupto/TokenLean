"""G01 must not hand the model back a corrupted copy of its own code.

G01 sends assistant history to LLMLingua-2, which keeps about half the word tokens and drops
the rest; only punctuation and digits are forced to stay. Kompress rewrites log and error
text with a seq2seq model. Neither can tell `fetch_user` or `/v2/users/` from filler, so in a
coding chat the model was editing code it never wrote. Now a message holding a fenced code
block never reaches those compressors, and any compression that changes code, a URL, a
path, an identifier or a version number is refused, and the original is sent. The
compressors are stubbed: what matters is what G01 accepts from them.
"""
import logging
import pathlib
import sys

import pytest

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

import middleware.g01_compression as g01  # noqa: E402
from middleware.g01_compression import G01Compression, _is_faithful_compression  # noqa: E402

CODE_TURN = (
    'Here is the helper: def fetch_user(user_id): return requests.get('
    'f"https://api.acme.io/v2/users/{user_id}") and it returns the user record for you.')


class TestTheGuard:

    @pytest.mark.parametrize("before, after", [
        (CODE_TURN, CODE_TURN.replace("fetch_user", "fetch user")),
        (CODE_TURN, CODE_TURN.replace("/v2/users/", "/v2/")),
        ("Set the user_id field on the record.", "Set user id field on record."),
        ("Call fetchUser with the account number.", "Call fetch User with account number."),
        ("Docs live at https://docs.acme.io/v2/users/list for reference.",
         "Docs at https://docs.acme.io/v2/list reference."),
        ("Edit /etc/proxy/config.yaml to change it.", "Edit /etc/config.yaml change it."),
        ("Run `make deploy-prod` to ship it.", "Run `make deploy` ship it."),
        ("Upgrade the package to 2.14.1 this week.", "Upgrade package 2.14 week."),
        ("Read config.database.host from the settings file.", "Read config host settings file."),
        ("Call load_config(path) on startup.", "Call load_config on startup."),
    ], ids=["identifier", "url-in-code", "snake_case", "camelCase", "url", "path",
            "inline-code", "version", "dotted", "call"])
    def test_changing_code_or_an_identifier_is_refused(self, before, after):
        assert _is_faithful_compression(before, after) is False
        assert g01._unfaithful_reason(before, after) == (
            "code, a URL, a path or an identifier was changed")

    def test_moving_a_protected_part_is_refused(self):
        """The compressors only delete, so a part that moved was rewritten."""
        assert _is_faithful_compression("Use alpha_one and then beta_two.",
                                        "Use beta_two then alpha_one.") is False

    def test_dropping_prose_around_intact_code_is_allowed(self):
        assert _is_faithful_compression(
            "You can simply call fetch_user(user_id) and then look at "
            "https://api.acme.io/v2/users for the full result.",
            "call fetch_user(user_id) look https://api.acme.io/v2/users full result.") is True

    @pytest.mark.parametrize("before, after", [
        # A sentence-ending period is not part of the path or the URL.
        ("The file is /etc/app/x.yaml.", "file /etc/app/x.yaml"),
        ("See https://acme.io/docs.", "See https://acme.io/docs"),
        # The regex fallback tightens the space before punctuation.
        ("The file is /etc/app/x.yaml .", "file is /etc/app/x.yaml."),
        # Prose in parentheses is not code.
        ("The report (see the appendix) covers every region this year.",
         "report (see appendix) covers every region year."),
    ], ids=["path-period", "url-period", "tightened-space", "parenthetical"])
    def test_punctuation_and_prose_are_not_protected(self, before, after):
        assert _is_faithful_compression(before, after) is True

    def test_the_negation_check_still_comes_first(self):
        assert g01._unfaithful_reason(
            "Never call fetch_user(user_id) twice.", "call fetch_user(user_id) twice.") == (
            "a negation or scope qualifier was dropped")

    def test_a_dropped_message_is_refused(self):
        assert g01._unfaithful_reason(CODE_TURN, "") == "the whole message was dropped"


def _ctx(make_ctx, minimal_config, content, **cfg):
    minimal_config["groups"]["G1_compression"] = {
        "enabled": True, "min_tokens_to_compress": 10, "min_chars_to_compress": 50,
        "kompress_enabled": False, "layered_composition_enabled": False,
        "selective_context_enabled": False, "deterministic_fallback": False, **cfg}
    return make_ctx([{"role": "user", "content": "Add error handling to that function."},
                     {"role": "assistant", "content": content},
                     {"role": "user", "content": "Go on."}], config=minimal_config)


class _Sidecar:
    """Stands in for LLMLingua-2: applies `edit` to what it is sent and records the calls."""

    def __init__(self, edit):
        self.edit, self.sent = edit, []

    async def __call__(self, url, text, ratio, force_reserve_digit=True):
        self.sent.append(text)
        return self.edit(text)


FENCED = ("Here is the helper you asked for, written so that it is really quite easy to "
          "follow:\n```python\ndef fetch_user(user_id):\n    return the_client.get(user_id)\n```\n"
          "It basically just returns the user.")


@pytest.mark.asyncio
class TestTheRequestPath:

    async def test_a_message_with_a_code_block_never_reaches_the_sidecar(
            self, make_ctx, minimal_config, monkeypatch):
        sidecar = _Sidecar(lambda text: text[: len(text) // 2])
        monkeypatch.setattr(g01, "_call_llmlingua", sidecar)
        ctx = _ctx(make_ctx, minimal_config, FENCED)
        out = await G01Compression().process_request(ctx)
        assert sidecar.sent == []
        assert out.messages[1]["content"] == FENCED

    async def test_nor_kompress_nor_the_selective_pruner(
            self, make_ctx, minimal_config, monkeypatch):
        calls = []

        class _Pruner:
            def prune_context(self, text):
                calls.append("pruner")
                return text[: len(text) // 2], 0.5

        log_with_code = "2026-09-27 10:15:02 ERROR a traceback follows\n" + FENCED
        monkeypatch.setattr(g01, "_call_llmlingua", _Sidecar(lambda text: text))
        monkeypatch.setattr(g01, "_kompress_compress",
                            lambda text, model, n: calls.append("kompress") or text[:40])
        monkeypatch.setattr(G01Compression, "_get_selective_pruner", lambda self, cfg: _Pruner())
        out = await G01Compression().process_request(
            _ctx(make_ctx, minimal_config, log_with_code, kompress_enabled=True))
        assert calls == []
        assert out.messages[1]["content"] == log_with_code

    @pytest.mark.parametrize("fence", ["```", "~~~", "  ```"])
    async def test_every_fence_form_counts(self, make_ctx, minimal_config, monkeypatch, fence):
        sidecar = _Sidecar(lambda text: text[: len(text) // 2])
        monkeypatch.setattr(g01, "_call_llmlingua", sidecar)
        content = FENCED.replace("\n```python", f"\n{fence}python").replace("\n```\n", f"\n{fence}\n")
        await G01Compression().process_request(_ctx(make_ctx, minimal_config, content))
        assert sidecar.sent == []

    async def test_a_sidecar_output_that_breaks_an_identifier_is_refused(
            self, make_ctx, minimal_config, monkeypatch, caplog):
        sidecar = _Sidecar(lambda text: text.replace("fetch_user", "fetch user")
                           .replace(" and it returns the user record for you", ""))
        monkeypatch.setattr(g01, "_call_llmlingua", sidecar)
        ctx = _ctx(make_ctx, minimal_config, CODE_TURN)
        with caplog.at_level(logging.WARNING, logger=g01.logger.name):
            out = await G01Compression().process_request(ctx)
        assert sidecar.sent == [CODE_TURN]
        assert out.messages[1]["content"] == CODE_TURN
        assert "code, a URL, a path or an identifier was changed" in caplog.text
        assert "fetch_user" not in caplog.text, "the log names the reason, not the content"

    async def test_a_prose_only_compression_still_applies(
            self, make_ctx, minimal_config, monkeypatch):
        shorter = CODE_TURN.replace(" and it returns the user record for you", "")
        monkeypatch.setattr(g01, "_call_llmlingua", _Sidecar(lambda text: shorter))
        out = await G01Compression().process_request(_ctx(make_ctx, minimal_config, CODE_TURN))
        assert out.messages[1]["content"] == shorter

    async def test_a_kompress_rewrite_of_a_log_path_is_refused(
            self, make_ctx, minimal_config, monkeypatch, caplog):
        log = ("2026-09-27 10:15:02 ERROR worker failed reading /var/lib/proxy/queue/batch-7.json "
               "because the retry budget was spent, skipping this batch for now")
        monkeypatch.setattr(g01, "_call_llmlingua", _Sidecar(lambda text: text))  # no reduction
        monkeypatch.setattr(g01, "_kompress_compress",
                            lambda text, model, n: "2026-09-27 10:15:02 ERROR worker failed "
                                                   "reading /var/lib/queue/batch.json")
        with caplog.at_level(logging.WARNING, logger=g01.logger.name):
            out = await G01Compression().process_request(
                _ctx(make_ctx, minimal_config, log, kompress_enabled=True))
        assert out.messages[1]["content"] == log
        assert "code, a URL, a path or an identifier was changed" in caplog.text

    async def test_the_regex_fallback_still_compresses_prose_around_code(
            self, make_ctx, minimal_config, monkeypatch):
        """The deterministic fallback keeps code byte-for-byte, so it still runs on a
        message that the model-based compressors skip."""
        sidecar = _Sidecar(lambda text: text[: len(text) // 2])
        monkeypatch.setattr(g01, "_call_llmlingua", sidecar)
        out = await G01Compression().process_request(
            _ctx(make_ctx, minimal_config, FENCED, deterministic_fallback=True))
        assert sidecar.sent == []
        result = out.messages[1]["content"]
        assert len(result) < len(FENCED)
        assert "```python\ndef fetch_user(user_id):\n    return the_client.get(user_id)\n```" in result
