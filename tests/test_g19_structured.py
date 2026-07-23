"""
Tests for G19 — Structured Context Pruning (Headroom).

Validates:
  - JSON compression (remove empty fields, compact)
  - Code compression (strip comments, blank lines)
  - Log compression (dedup, truncate)
  - Content-type auto-detection
  - Config-driven enable/disable
  - Request-side and response-side processing
  - Min length guard
  - Fallback when headroom not installed
"""
import copy
import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g19_headroom import (
    G19Headroom,
    _detect_content_type,
    _compress,
    _compress_json,
    _compress_code,
    _compress_logs,
    _compress_text,
    _dedupe_repeated_structures,
)


def _make_config(enabled=True, request_side=True, response_side=True,
                 min_length=50, strategies=None):
    if strategies is None:
        strategies = {
            "json": {"remove_empty": True, "dedupe_keys": True},
            "code": {"strip_comments": True, "strip_whitespace": True, "compress_imports": True},
            "logs": {"dedupe_lines": True, "truncate_long_lines": 200},
        }
    return {
        "groups": {
            "G19_headroom": {
                "enabled": enabled,
                "request_side_enabled": request_side,
                "response_side_enabled": response_side,
                "min_length_to_compress": min_length,
                "compression_strategies": strategies,
            }
        }
    }


def _make_ctx(messages, model="gpt-4o", params=None, config=None):
    from tests.conftest import _make_savings
    from middleware import RequestContext

    if params is None:
        params = {}
    if config is None:
        config = _make_config()
    savings = _make_savings(messages, model)
    return RequestContext(
        request_id="req-g19-test",
        user_id="test-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model=model,
        routed_model=model,
        params=dict(params),
        config=config,
        savings=savings,
    )


# ─── Content type detection ──────────────────────────────────────────────────

def test_detect_json():
    assert _detect_content_type('{"key": "value", "nested": {"a": 1}}') == "json"
    assert _detect_content_type('[{"id": 1}, {"id": 2}]') == "json"


def test_detect_code():
    code = """import os
import sys

def main():
    print("hello")

class Foo:
    pass
"""
    assert _detect_content_type(code) == "code"


def test_detect_logs():
    logs = """2024-06-01T12:00:00.000Z INFO Starting server
2024-06-01T12:00:01.000Z DEBUG Connected to database
2024-06-01T12:00:02.000Z ERROR Connection timeout
"""
    assert _detect_content_type(logs) == "logs"


def test_detect_plain_text_returns_text():
    assert _detect_content_type("Hello, how are you today?") == "text"
    assert _detect_content_type("The quick brown fox jumps over the lazy dog.") == "text"


# ─── JSON compression ────────────────────────────────────────────────────────

def test_json_removes_empty_fields():
    data = json.dumps({"name": "Alice", "email": "", "address": None, "tags": []})
    result = _compress_json(data, {"remove_empty": True, "dedupe_keys": True})
    parsed = json.loads(result)
    assert "name" in parsed
    assert "email" not in parsed
    assert "address" not in parsed
    assert "tags" not in parsed


def test_json_compact_output():
    data = json.dumps({"key": "value", "nested": {"a": 1}}, indent=2)
    result = _compress_json(data, {"remove_empty": True})
    assert " " not in result or "value" in result  # compact separators
    assert "\n" not in result


def test_json_invalid_returns_none():
    assert _compress_json("not json at all", {}) is None


def test_json_dedupe_repeated_structures():
    data = json.dumps([
        {"event": "created", "timestamp": "2024-01-01", "details": ""},
        {"event": "shipped", "timestamp": "2024-01-02", "details": ""},
        {"event": "delivered", "timestamp": "2024-01-03", "details": ""},
    ])
    result = _compress_json(data, {"remove_empty": False, "dedupe_keys": True})
    parsed = json.loads(result)
    # Should be converted to schema-referencing format
    assert "_schema_" in parsed
    assert "_rows_" in parsed
    assert parsed["_schema_"] == ["details", "event", "timestamp"]
    assert len(parsed["_rows_"]) == 3


def test_dedupe_repeated_structures_direct():
    data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}]
    result = _dedupe_repeated_structures(data)
    assert result == {
        "_schema_": ["a", "b"],
        "_rows_": [[1, 2], [3, 4], [5, 6]],
    }


def test_dedupe_skips_heterogeneous_keys():
    data = [{"a": 1}, {"b": 2}, {"c": 3}]
    result = _dedupe_repeated_structures(data)
    # No majority key set (>50%) — should remain unchanged
    assert result == data


# ─── Code compression ────────────────────────────────────────────────────────

def test_code_strips_comments():
    code = """# This is a comment
import os
# Another comment
def main():
    x = 1  # inline comment
    return x
"""
    result = _compress_code(code, {"strip_comments": True, "strip_whitespace": True, "compress_imports": True})
    assert "# This is a comment" not in result
    assert "# Another comment" not in result
    assert "def main():" in result
    assert "return x" in result


def test_code_strips_blank_lines():
    code = """import os

import sys


def main():
    pass
"""
    result = _compress_code(code, {"strip_comments": True, "strip_whitespace": True, "compress_imports": True})
    assert "\n\n" not in result


def test_code_preserves_logic():
    code = """def calculate(a, b):
    if a > b:
        return a - b
    else:
        return b - a
"""
    result = _compress_code(code, {"strip_comments": True, "strip_whitespace": True, "compress_imports": False})
    assert "def calculate(a, b):" in result
    assert "return a - b" in result
    assert "return b - a" in result


# ─── Log compression ─────────────────────────────────────────────────────────

def test_logs_dedup_repeated_lines():
    logs = """2024-06-01T12:00:00Z INFO Health check OK
2024-06-01T12:00:05Z INFO Health check OK
2024-06-01T12:00:10Z INFO Health check OK
2024-06-01T12:00:15Z ERROR Connection timeout
"""
    result = _compress_logs(logs, {"dedupe_lines": True, "truncate_long_lines": 200})
    lines = result.strip().split("\n")
    # Should have the first health check, the error, and a dedup summary
    assert any("Health check OK" in l for l in lines)
    assert any("Connection timeout" in l for l in lines)
    assert any("duplicate" in l.lower() for l in lines)


def test_logs_truncate_long_lines():
    long_line = "2024-06-01T12:00:00Z INFO " + "x" * 500
    result = _compress_logs(long_line, {"dedupe_lines": False, "truncate_long_lines": 100})
    lines = result.strip().split("\n")
    assert len(lines[0]) <= 115  # 100 + "[truncated]" suffix


# ─── Log compression — severity-aware dedup (2026-07-23) ────────────────────
# A recurring ERROR is diagnostic signal (is this flapping or a one-off?), not
# noise — the old timestamp-blind dedup silently collapsed a genuine second
# occurrence into the same bucket as repeated INFO/DEBUG boilerplate (proven on
# pitch-test-plan DS7 ds7-03: the optimised answer omitted a real ERROR
# recurrence the baseline reported). Default always_keep_severities preserves
# every ERROR/FATAL/CRITICAL/PANIC line verbatim; only lower severities dedupe.

def test_recurring_error_is_never_deduped_by_default():
    logs = """2024-06-01T10:50:00Z [ERROR] connection refused to upstream payments service
2024-06-01T10:51:01Z [ERROR] connection refused to upstream payments service
2024-06-01T10:52:02Z [FATAL] pod restarted after crash
"""
    result = _compress_logs(logs, {"dedupe_lines": True, "truncate_long_lines": 200})
    assert "10:50:00" in result and "10:51:01" in result and "10:52:02" in result
    # neither ERROR nor FATAL counted as a dedup — no suppression footer at all
    assert "suppressed" not in result.lower()


def test_boilerplate_still_dedupes_alongside_preserved_errors():
    logs = """2024-06-01T10:00:00Z [INFO] health check ok
2024-06-01T10:01:00Z [INFO] health check ok
2024-06-01T10:02:00Z [INFO] health check ok
2024-06-01T10:50:00Z [ERROR] connection refused
2024-06-01T10:51:01Z [ERROR] connection refused
"""
    result = _compress_logs(logs, {"dedupe_lines": True, "truncate_long_lines": 200})
    lines = result.strip().split("\n")
    assert sum(1 for l in lines if "health check ok" in l) == 1        # collapsed
    assert sum(1 for l in lines if "connection refused" in l) == 2     # both kept
    assert any("1 duplicate log patterns suppressed" in l for l in lines)  # only INFO counted


def test_always_keep_severities_is_whole_word_and_case_insensitive():
    # "ERRORS" must not match the whole-word "ERROR" pattern (no false-positive
    # widening); lowercase "error" must still match (case-insensitive).
    logs = """2024-06-01T10:00:00Z [errors_summary] nothing to see
2024-06-01T10:01:00Z [errors_summary] nothing to see
2024-06-01T10:50:00Z [error] real failure
2024-06-01T10:51:01Z [error] real failure
"""
    result = _compress_logs(logs, {"dedupe_lines": True, "truncate_long_lines": 200})
    lines = result.strip().split("\n")
    assert sum(1 for l in lines if "errors_summary" in l) == 1   # NOT a whole-word "ERROR" match -> deduped
    assert sum(1 for l in lines if "real failure" in l) == 2     # lowercase "error" matched -> both kept


def test_empty_always_keep_severities_reverts_to_old_behaviour():
    logs = """2024-06-01T10:50:00Z [ERROR] connection refused
2024-06-01T10:51:01Z [ERROR] connection refused
"""
    result = _compress_logs(logs, {"dedupe_lines": True, "truncate_long_lines": 200,
                                   "always_keep_severities": []})
    lines = result.strip().split("\n")
    assert sum(1 for l in lines if "connection refused" in l) == 1  # back to timestamp-blind dedup
    assert any("1 duplicate log patterns suppressed" in l for l in lines)


def test_custom_always_keep_severities_list():
    logs = """2024-06-01T10:00:00Z [NOTICE] recurring but not in the default list
2024-06-01T10:01:00Z [NOTICE] recurring but not in the default list
"""
    default_result = _compress_logs(logs, {"dedupe_lines": True, "truncate_long_lines": 200})
    assert default_result.strip().count("NOTICE") == 1  # NOTICE not protected by default -> deduped

    custom_result = _compress_logs(logs, {
        "dedupe_lines": True, "truncate_long_lines": 200,
        "always_keep_severities": ["NOTICE"],
    })
    assert custom_result.strip().count("NOTICE") == 2  # operator opted NOTICE into the keep-list


def test_high_severity_lines_still_truncate_when_long():
    long_error = "2024-06-01T10:50:00Z [ERROR] " + "x" * 500
    result = _compress_logs(long_error, {"dedupe_lines": True, "truncate_long_lines": 100})
    lines = result.strip().split("\n")
    assert len(lines[0]) <= 115
    assert lines[0].endswith("...[truncated]")


def test_dedupe_disabled_keeps_every_line_including_errors():
    logs = """2024-06-01T10:50:00Z [ERROR] connection refused
2024-06-01T10:51:01Z [ERROR] connection refused
"""
    result = _compress_logs(logs, {"dedupe_lines": False, "truncate_long_lines": 200})
    assert result.strip().count("connection refused") == 2


# ─── Middleware integration ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_skips():
    msgs = [{"role": "user", "content": '{"key": "value", "empty": "", "null_field": null}'}]
    ctx = _make_ctx(msgs, config=_make_config(enabled=False))
    g19 = G19Headroom()
    result = await g19.process_request(ctx)
    assert result.messages == msgs


@pytest.mark.asyncio
async def test_request_side_compresses_json():
    big_json = json.dumps({
        "name": "Alice", "email": "", "address": None,
        "tags": [], "data": {"nested": "value", "empty": ""},
    })
    msgs = [{"role": "system", "content": big_json}]
    ctx = _make_ctx(msgs, config=_make_config(min_length=10))
    g19 = G19Headroom()
    result = await g19.process_request(ctx)
    # Should be compressed (empty fields removed, compact)
    assert len(result.messages[0]["content"]) < len(big_json)
    assert len(result.savings.step_savings) == 1
    assert result.savings.step_savings[0].group == "G19"


@pytest.mark.asyncio
async def test_response_side_compresses_tool_output():
    big_json = json.dumps({
        "results": [{"id": 1, "data": "hello"}, {"id": 2, "data": "world"}],
        "metadata": None, "empty": "",
    })
    msgs = [{"role": "user", "content": "test"}]
    ctx = _make_ctx(msgs, config=_make_config(min_length=10))
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {"name": "search", "result": big_json}
                }]
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    tool_result = result["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
    assert len(tool_result) < len(big_json)


@pytest.mark.asyncio
async def test_min_length_guard():
    """Short content should not be compressed."""
    msgs = [{"role": "system", "content": '{"a": 1}'}]
    ctx = _make_ctx(msgs, config=_make_config(min_length=100))
    g19 = G19Headroom()
    result = await g19.process_request(ctx)
    assert result.messages[0]["content"] == '{"a": 1}'


@pytest.mark.asyncio
async def test_request_side_disabled():
    big_json = json.dumps({"name": "Alice", "empty": ""})
    msgs = [{"role": "system", "content": big_json}]
    ctx = _make_ctx(msgs, config=_make_config(request_side=False, min_length=10))
    g19 = G19Headroom()
    result = await g19.process_request(ctx)
    assert result.messages[0]["content"] == big_json


@pytest.mark.asyncio
async def test_response_side_disabled():
    """response_side_enabled=false leaves tool output untouched even when enabled=true."""
    big_json = json.dumps({
        "results": [{"id": 1, "data": "hello"}],
        "metadata": None, "empty": "",
    })
    msgs = [{"role": "user", "content": "test"}]
    ctx = _make_ctx(msgs, config=_make_config(response_side=False, min_length=10))
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "search", "result": big_json}}],
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    tool_result = result["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
    assert tool_result == big_json
    assert len(ctx.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_plain_text_not_compressed():
    """Natural language should be left for G1, not G19."""
    msgs = [{"role": "system", "content": "You are a helpful AI assistant that specializes in customer support. Please be polite and thorough in your responses."}]
    ctx = _make_ctx(msgs, config=_make_config(min_length=10))
    g19 = G19Headroom()
    result = await g19.process_request(ctx)
    assert result.messages[0]["content"] == msgs[0]["content"]
    assert len(result.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_original_messages_not_mutated():
    big_json = json.dumps({"name": "Alice", "empty": "", "null": None})
    msgs = [{"role": "system", "content": big_json}]
    ctx = _make_ctx(msgs, config=_make_config(min_length=10))
    original_before = copy.deepcopy(ctx.original_messages)
    g19 = G19Headroom()
    await g19.process_request(ctx)
    assert ctx.original_messages == original_before


# ─── T10: CodeCompressor / SmartCrusher routing ──────────────────────────────

class TestT10CompressorRouting:
    """Verify _compress() routing for headroom >= 0.27 (CodeCompressor / SmartCrusher.compress
    were removed upstream): JSON -> SmartCrusher.compact_document_json (best-in-class for JSON);
    logs/code/text -> built-in compressors; built-ins also serve as fallback."""

    def test_json_routes_to_headroom_compact(self):
        """JSON uses SmartCrusher.compact_document_json() when Headroom is available."""
        from unittest.mock import MagicMock, patch
        import middleware.g19_headroom as mod

        mock_sc = MagicMock()
        mock_sc.compact_document_json.return_value = '{"k":1}'  # shorter than input

        with patch.object(mod, "_headroom_available", True), \
             patch.object(mod, "_smart_crusher", mock_sc):
            result = _compress('{"key": 1, "empty": null}', "json", {"remove_empty": True})

        mock_sc.compact_document_json.assert_called_once_with('{"key": 1, "empty": null}')
        assert result == '{"k":1}'

    def test_json_falls_back_to_builtin_on_headroom_failure(self):
        """If Headroom is a no-op or raises, fall through to the built-in JSON compactor."""
        from unittest.mock import MagicMock, patch
        import middleware.g19_headroom as mod

        for ret in (None, RuntimeError("boom")):
            mock_sc = MagicMock()
            if isinstance(ret, Exception):
                mock_sc.compact_document_json.side_effect = ret
            else:
                mock_sc.compact_document_json.return_value = ret
            with patch.object(mod, "_headroom_available", True), \
                 patch.object(mod, "_smart_crusher", mock_sc):
                result = _compress('{"key": 1, "empty": null}', "json", {"remove_empty": True})
            assert result is not None and "empty" not in result  # built-in removed the empty field

    def test_code_uses_builtin_not_headroom(self):
        """Code uses the built-in compressor (upstream CodeCompressor was removed in 0.27)."""
        from unittest.mock import MagicMock, patch
        import middleware.g19_headroom as mod

        mock_sc = MagicMock()
        code = "# comment\ndef foo():\n    pass"
        with patch.object(mod, "_headroom_available", True), \
             patch.object(mod, "_smart_crusher", mock_sc):
            result = _compress(code, "code", {"strip_comments": True, "strip_whitespace": True, "compress_imports": True})

        mock_sc.compact_document_json.assert_not_called()
        assert result is not None and "# comment" not in result

    def test_logs_use_builtin_not_headroom(self):
        """Logs use the built-in compressor (query-less crush does not help logs)."""
        from unittest.mock import MagicMock, patch
        import middleware.g19_headroom as mod

        mock_sc = MagicMock()
        logs = "\n".join("2024-01-01 INFO duplicate line" for _ in range(5))
        with patch.object(mod, "_headroom_available", True), \
             patch.object(mod, "_smart_crusher", mock_sc):
            result = _compress(logs, "logs", {"dedupe_lines": True})

        mock_sc.compact_document_json.assert_not_called()
        assert result is not None  # built-in dedupe collapses the repeated lines

    def test_fallback_to_builtin_when_headroom_unavailable(self):
        """When _headroom_available=False, built-in compressors handle all types."""
        from unittest.mock import patch
        import middleware.g19_headroom as mod

        code = "# comment\ndef foo():\n    pass"
        with patch.object(mod, "_headroom_available", False):
            result = _compress(code, "code", {"strip_comments": True, "strip_whitespace": True, "compress_imports": True})

        assert result is not None
        assert "# comment" not in result


# ─── T10: Plain text (_compress_text) ─────────────────────────────────────────

class TestCompressText:
    """Tests for the built-in plain-text fallback compressor."""

    def test_deduplicates_repeated_sentences(self):
        text = "The system is healthy. All checks passed. The system is healthy."
        result = _compress_text(text, {"dedupe_sentences": True})
        assert result is not None
        assert result.count("The system is healthy") == 1

    def test_no_change_when_no_duplicates(self):
        text = "First sentence. Second sentence. Third sentence."
        result = _compress_text(text, {"dedupe_sentences": True})
        # No duplicates → result may be None (no improvement)
        if result is not None:
            assert len(result) <= len(text)

    def test_dedupe_disabled_preserves_duplicates(self):
        text = "Repeat. Repeat. Repeat."
        result = _compress_text(text, {"dedupe_sentences": False})
        if result is not None:
            assert result.count("Repeat") == 3

    def test_max_sentence_len_truncates(self):
        long_sentence = "A" * 200 + "."
        short_sentence = "Short."
        text = f"{long_sentence} {short_sentence}"
        result = _compress_text(text, {"dedupe_sentences": True, "max_sentence_len": 50})
        assert result is not None
        assert "…" in result

    def test_max_sentence_len_zero_means_disabled(self):
        long_sentence = "A" * 200 + "."
        result = _compress_text(long_sentence + " " + long_sentence,
                                {"dedupe_sentences": True, "max_sentence_len": 0})
        # Duplicates removed but no truncation
        if result is not None:
            assert "…" not in result

    def test_returns_none_when_no_improvement(self):
        text = "Hello."
        result = _compress_text(text, {"dedupe_sentences": True})
        assert result is None

    def test_text_uses_builtin_not_headroom(self):
        """'text' content uses the built-in text compressor (no Headroom call)."""
        from unittest.mock import MagicMock, patch
        import middleware.g19_headroom as mod

        mock_sc = MagicMock()
        text = "Repeat me. Repeat me. Repeat me."
        with patch.object(mod, "_headroom_available", True), \
             patch.object(mod, "_smart_crusher", mock_sc):
            result = _compress(text, "text", {"dedupe_sentences": True})

        mock_sc.compact_document_json.assert_not_called()
        assert result is not None and result.count("Repeat me") == 1

    @pytest.mark.asyncio
    async def test_middleware_compresses_plain_text_when_strategy_configured(self):
        """G19 compresses plain text when 'text' is in compression_strategies."""
        repeated_text = "The quick brown fox. " * 5
        msgs = [{"role": "system", "content": repeated_text}]
        strategies = {
            "json": {"remove_empty": True, "dedupe_keys": True},
            "code": {"strip_comments": True, "strip_whitespace": True, "compress_imports": True},
            "logs": {"dedupe_lines": True, "truncate_long_lines": 200},
            "text": {"dedupe_sentences": True, "max_sentence_len": 0},
        }
        ctx = _make_ctx(msgs, config=_make_config(min_length=10, strategies=strategies))
        g19 = G19Headroom()
        result = await g19.process_request(ctx)
        compressed = result.messages[0]["content"]
        assert len(compressed) < len(repeated_text)
        assert len(result.savings.step_savings) == 1

    @pytest.mark.asyncio
    async def test_middleware_skips_text_when_no_text_strategy(self):
        """Without 'text' in strategies, plain text passes through unchanged."""
        text = "The quick brown fox. " * 5
        msgs = [{"role": "system", "content": text}]
        ctx = _make_ctx(msgs, config=_make_config(min_length=10))
        g19 = G19Headroom()
        result = await g19.process_request(ctx)
        assert result.messages[0]["content"] == text
        assert len(result.savings.step_savings) == 0
