"""Unit tests for scripts/compress_prompts.py (offline prompt/memory compressor)."""
import importlib.util
import os
import sys

import pytest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "compress_prompts.py"
)


def _load_script():
    """Import compress_prompts.py as a module (it's a standalone script, not a
    package member) so its functions can be exercised directly."""
    spec = importlib.util.spec_from_file_location("compress_prompts", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cp():
    return _load_script()


_FILLER_MD = (
    "# Rules\n\nYou should really just run the tests before you push to `main`. "
    "Please make sure the `RATE_LIMIT` constant is set at /etc/app/config.yaml.\n"
)


class TestCompressTextFile:
    def test_preview_does_not_write(self, cp, tmp_path):
        f = tmp_path / "mem.md"
        f.write_text(_FILLER_MD, encoding="utf-8")
        before, after = cp._compress_text_file(str(f), write=False, backup=True, min_chars=10)
        assert after < before
        assert f.read_text(encoding="utf-8") == _FILLER_MD  # untouched

    def test_write_compresses_and_backs_up(self, cp, tmp_path):
        f = tmp_path / "mem.md"
        f.write_text(_FILLER_MD, encoding="utf-8")
        cp._compress_text_file(str(f), write=True, backup=True, min_chars=10)
        assert f.read_text(encoding="utf-8") != _FILLER_MD
        backup = tmp_path / "mem.original.md"
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == _FILLER_MD

    def test_preserves_code_and_identifiers(self, cp, tmp_path):
        f = tmp_path / "mem.md"
        f.write_text(_FILLER_MD, encoding="utf-8")
        cp._compress_text_file(str(f), write=True, backup=False, min_chars=10)
        out = f.read_text(encoding="utf-8")
        assert "`main`" in out
        assert "`RATE_LIMIT`" in out
        assert "/etc/app/config.yaml" in out

    def test_skips_below_min_chars(self, cp, tmp_path):
        f = tmp_path / "tiny.md"
        f.write_text("short text", encoding="utf-8")
        before, after = cp._compress_text_file(str(f), write=True, backup=True, min_chars=1000)
        assert before == after == len("short text")
        assert f.read_text(encoding="utf-8") == "short text"  # untouched


class TestCompressStructuredFile:
    def test_compresses_json_descriptions(self, cp, tmp_path):
        f = tmp_path / "reg.json"
        f.write_text(
            '[{"name": "web_search", "description": "This tool will really just search the web."}]',
            encoding="utf-8",
        )
        cp._compress_structured_file(str(f), ["description"], write=True, backup=True)
        import json
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "really" not in data[0]["description"].lower()
        assert data[0]["name"] == "web_search"  # non-targeted field untouched

    def test_min_chars_applies_to_structured_files(self, cp, tmp_path):
        # Regression test: --min-chars used to be silently ignored for JSON/YAML.
        f = tmp_path / "tiny.json"
        original = '[{"description": "Please just do the thing basically."}]'
        f.write_text(original, encoding="utf-8")
        before, after = cp._compress_structured_file(
            str(f), ["description"], write=True, backup=True, min_chars=10_000
        )
        assert before == after == len(original)
        assert f.read_text(encoding="utf-8") == original  # untouched — skipped by size gate

    def test_min_chars_default_does_not_skip_normal_files(self, cp, tmp_path):
        f = tmp_path / "reg.json"
        f.write_text(
            '[{"description": "This tool will really just search the web for you please."}]',
            encoding="utf-8",
        )
        before, after = cp._compress_structured_file(
            str(f), ["description"], write=True, backup=True, min_chars=0
        )
        assert after < before

    def test_parse_failure_skips_gracefully(self, cp, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        before, after = cp._compress_structured_file(str(f), ["description"], write=True, backup=True)
        assert before == after
        assert f.read_text(encoding="utf-8") == "{not valid json"  # untouched


class TestMainCLI:
    def test_refuses_code_files_by_default(self, cp, tmp_path, monkeypatch, capsys):
        f = tmp_path / "foo.py"
        f.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["compress_prompts.py", "--write", str(f)])
        cp.main()
        assert f.read_text(encoding="utf-8") == "x = 1\n"  # untouched

    def test_min_chars_flag_reaches_structured_path(self, cp, tmp_path, monkeypatch):
        # End-to-end CLI regression for the --min-chars structured-file fix.
        f = tmp_path / "tiny.json"
        original = '[{"description": "Please just do the thing."}]'
        f.write_text(original, encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv",
            ["compress_prompts.py", "--write", "--min-chars", "10000", str(f)],
        )
        cp.main()
        assert f.read_text(encoding="utf-8") == original  # skipped — flag reached this path
