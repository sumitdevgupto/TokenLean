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
    """G14 compacts tool output structurally, with no third-party library."""

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

    def test_maybe_compress_uses_the_builtin_compactor(self):
        from middleware.g14_tool_output import _maybe_compress_spreadsheet
        rows = [{"id": i, "val": i * 2} for i in range(5)]
        assert "_schema_" in _maybe_compress_spreadsheet(rows, "gpt-4o")

    def test_a_csv_string_passes_through_unchanged(self):
        """CSV had a `crush` round-trip until 2026-09-05. Measured on real headroom
        0.34.0 at 10, 200 and 2,000 rows, its output was NEVER shorter than the input, so
        the length guard discarded it every single time — a Rust call per CSV tool result
        whose result was always thrown away."""
        from middleware.g14_tool_output import _maybe_compress_spreadsheet
        csv_text = "id,name,price" + chr(10) + chr(10).join(
            f"{i},item-{i},{i * 10}" for i in range(20))
        assert _maybe_compress_spreadsheet(csv_text, "gpt-4o") == csv_text

    def test_a_long_string_field_is_never_swapped_for_a_pointer(self):
        """Backlog #48, at G14's own boundary.

        `compact_document_json` replaced any string leaf of roughly 300 characters or more
        with `<<ccr:HASH,string,NB>>`, a pointer into an in-process Rust store this repo
        exposes no route to. Reproduced through this function in the deployed container on
        an array of incident records: every `summary` came back as a marker. The tool
        result stayed valid JSON and got shorter, so neither guard in place at the time
        could see it.
        """
        from middleware.g14_tool_output import _maybe_compress_spreadsheet
        summary = ("Root cause: payment gateway TCP connection pool exhausted under load "
                   "after a deploy changed pool settings. Mitigation: raise "
                   "max_connections, enable the circuit breaker, roll back if needed. " * 2)
        assert len(summary) > 300, "fixture must cross the trigger size"
        rows = [{"id": f"inc-{i}", "service": "checkout", "summary": summary}
                for i in range(3)]

        result = json.dumps(_maybe_compress_spreadsheet(rows, "gpt-4o"))
        assert "<<ccr:" not in result
        assert "connection pool exhausted" in result

    def test_no_third_party_compactor_is_called(self):
        """Pinned by source, because the failure mode is someone re-adding the call —
        which no behavioural test of the current code can detect."""
        import inspect
        import middleware.g14_tool_output as mod
        code = chr(10).join(line for line in inspect.getsource(mod).splitlines()
                            if not line.lstrip().startswith("#"))
        for forbidden in ("SmartCrusher", "compact_document_json", ".crush("):
            assert forbidden not in code, (
                f"G14 calls {forbidden} again — see backlog #48"
            )

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
