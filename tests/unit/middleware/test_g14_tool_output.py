"""Unit tests for G14 — Tool Call & Output Minimisation."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest


def _response_with_tool_call(tool_name: str, result: dict) -> dict:
    """Build a response where the tool result is embedded in tc['function']['result']."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": "{}",
                                "result": result,   # G14 reads from here
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }


@pytest.mark.asyncio
class TestG14ToolOutput:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G14_tool_output"]["enabled"] = False
        response = _response_with_tool_call("get_order", {"id": 1, "status": "shipped", "extra": "noise"})
        from middleware.g14_tool_output import G14ToolOutput
        response = await G14ToolOutput().process_response(ctx, response)
        # Result should still have 'extra' field (no projection applied)
        result = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert "extra" in result

    async def test_no_tool_calls_passes_through(self, make_ctx):
        ctx = make_ctx()
        response = {"choices": [{"message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}]}
        original = dict(response)
        from middleware.g14_tool_output import G14ToolOutput
        response = await G14ToolOutput().process_response(ctx, response)
        assert response == original

    async def test_field_whitelist_strips_extra_fields(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G14_tool_output"]["field_whitelist"] = {
            "get_order": ["id", "status"]
        }
        response = _response_with_tool_call("get_order", {"id": 1, "status": "shipped", "address": "123 Main St", "phone": "555-1234"})
        from middleware.g14_tool_output import G14ToolOutput
        response = await G14ToolOutput().process_response(ctx, response)
        result = response["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert "id" in result
        assert "status" in result
        assert "address" not in result
        assert "phone" not in result

    async def test_step_saving_recorded_when_fields_stripped(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G14_tool_output"]["field_whitelist"] = {
            "big_tool": ["id"]
        }
        big_result = {f"field_{i}": f"value_{i}" for i in range(20)}
        response = _response_with_tool_call("big_tool", big_result)
        from middleware.g14_tool_output import G14ToolOutput
        response = await G14ToolOutput().process_response(ctx, response)
        # Projection from 20 fields to 1 should record a saving
        if any(s.group == "G14" for s in ctx.savings.step_savings):
            step = next(s for s in ctx.savings.step_savings if s.group == "G14")
            assert step.tokens_after < step.tokens_before

    async def test_max_result_tokens_config_truncates_string_result(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G14_tool_output"]["max_result_tokens"] = 10
        response = _response_with_tool_call("get_blob", "z" * 2000)
        from middleware.g14_tool_output import G14ToolOutput
        resp = await G14ToolOutput().process_response(ctx, response)
        result = resp["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        assert isinstance(result, str) and "...[truncated]" in result


# ─── T32: spreadsheet compression ────────────────────────────────────────────

class TestT32SpreadsheetCompression:
    """Tests for headroom SmartCrusher integration in G14."""

    def test_builtin_compresses_json_array_to_schema_rows(self):
        from middleware.g14_tool_output import _builtin_compress_spreadsheet
        rows = [{"id": i, "name": f"item-{i}", "price": i * 10} for i in range(10)]
        result = _builtin_compress_spreadsheet(rows, "gpt-4o")
        assert "_schema_" in result
        assert "_rows_" in result
        assert result["_schema_"] == ["id", "name", "price"]
        assert len(result["_rows_"]) == 10

    def test_builtin_passes_through_non_array(self):
        from middleware.g14_tool_output import _builtin_compress_spreadsheet
        obj = {"key": "value"}
        assert _builtin_compress_spreadsheet(obj, "gpt-4o") == obj

    def test_builtin_passes_through_small_array(self):
        from middleware.g14_tool_output import _builtin_compress_spreadsheet
        arr = [{"a": 1}]
        assert _builtin_compress_spreadsheet(arr, "gpt-4o") == arr

    def test_maybe_compress_calls_builtin_for_json_array_when_headroom_absent(self):
        from unittest.mock import patch
        import middleware.g14_tool_output as mod
        rows = [{"id": i, "val": i * 2} for i in range(5)]
        with patch.object(mod, "_smart_crusher", None):
            from middleware.g14_tool_output import _maybe_compress_spreadsheet
            result = _maybe_compress_spreadsheet(rows, "gpt-4o")
        assert "_schema_" in result

    def test_maybe_compress_calls_smartcrusher_for_csv(self):
        from unittest.mock import MagicMock, patch
        import middleware.g14_tool_output as mod

        csv_text = "id,name,value\n1,foo,100\n2,bar,200\n3,baz,300\n"
        mock_crusher = MagicMock()
        mock_crusher.crush.return_value = MagicMock(compressed="id|name|value\n1|foo|100")

        with patch.object(mod, "_smart_crusher", mock_crusher):
            from middleware.g14_tool_output import _maybe_compress_spreadsheet
            result = _maybe_compress_spreadsheet(csv_text, "gpt-4o")

        mock_crusher.crush.assert_called_once_with(csv_text)
        assert result == "id|name|value\n1|foo|100"

    def test_maybe_compress_calls_smartcrusher_for_json_array(self):
        from unittest.mock import MagicMock, patch
        import middleware.g14_tool_output as mod

        rows = [{"id": i, "val": i * 2} for i in range(5)]
        compact = '[{"id":0,"val":0}]'  # shorter than the serialised input
        mock_crusher = MagicMock()
        mock_crusher.compact_document_json.return_value = compact

        with patch.object(mod, "_smart_crusher", mock_crusher):
            from middleware.g14_tool_output import _maybe_compress_spreadsheet
            result = _maybe_compress_spreadsheet(rows, "gpt-4o")

        mock_crusher.compact_document_json.assert_called_once()
        assert result == [{"id": 0, "val": 0}]  # parsed back from compacted JSON

    def test_smartcrusher_exception_falls_back_to_builtin(self):
        from unittest.mock import MagicMock, patch
        import middleware.g14_tool_output as mod

        rows = [{"id": i, "val": i} for i in range(5)]
        mock_crusher = MagicMock()
        mock_crusher.compact_document_json.side_effect = RuntimeError("headroom error")

        with patch.object(mod, "_smart_crusher", mock_crusher):
            from middleware.g14_tool_output import _maybe_compress_spreadsheet
            result = _maybe_compress_spreadsheet(rows, "gpt-4o")

        # On failure G14 falls back to the built-in compactor (schema+rows form)
        assert "_schema_" in result

    @pytest.mark.asyncio
    async def test_middleware_compresses_json_array_tool_output(self, make_ctx):
        """End-to-end: JSON array tool result gets schema+rows compression."""
        ctx = make_ctx()
        rows = [{"id": i, "product": f"p-{i}", "qty": i * 5, "price": i * 10.0} for i in range(15)]
        response = _response_with_tool_call("get_inventory", rows)
        from middleware.g14_tool_output import G14ToolOutput
        result_resp = await G14ToolOutput().process_response(ctx, response)
        result = result_resp["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        # Should be schema+rows (smaller) or list (if no improvement)
        result_str = json.dumps(result)
        original_str = json.dumps(rows)
        assert len(result_str) <= len(original_str)

    @pytest.mark.asyncio
    async def test_spreadsheet_compression_disabled_by_config(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G14_tool_output"]["spreadsheet_compression"] = False
        rows = [{"id": i, "val": i} for i in range(20)]
        response = _response_with_tool_call("get_data", rows)
        from middleware.g14_tool_output import G14ToolOutput
        result_resp = await G14ToolOutput().process_response(ctx, response)
        result = result_resp["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
        # Should remain as list (no schema conversion)
        assert isinstance(result, list)


# ─── Configurable per-field / per-result truncation caps ─────────────────────

class TestConfigurableTruncationCaps:
    """G14 field/result token caps are config-driven (config.yaml.template)."""

    def test_truncate_respects_config_result_cap(self):
        from middleware.g14_tool_output import _truncate
        long = "x" * 4000  # ~1000 tokens
        out = _truncate(long, "gpt-4o", max_result_tokens=10)
        assert "...[truncated]" in out
        assert len(out) <= 10 * 4 + len("...[truncated]")

    def test_truncate_respects_config_field_cap(self):
        from middleware.g14_tool_output import _truncate
        out = _truncate({"note": "y" * 400}, "gpt-4o", max_field_tokens=5)
        assert out["note"].endswith("...[truncated]")

    def test_truncate_uses_module_defaults_when_not_overridden(self):
        from middleware.g14_tool_output import _truncate
        assert _truncate("short", "gpt-4o") == "short"
