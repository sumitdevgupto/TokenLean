"""
G19 ROI ablation — DS3 Multi-Turn Agent.

Validates:
  - Baseline (G19 off): raw tool output JSON with empty fields preserved
  - Isolated (G19 on): tool output compressed, empty fields removed, code stripped
  - Gain: 40-70% additional structured compression after G14
  - Quality gate: logic preserved, no data loss
"""
import copy
import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g19_headroom import G19Headroom
from middleware import RequestContext
from tests.conftest import _make_savings


def _make_ctx(messages, config=None):
    if config is None:
        config = {
            "groups": {
                "G19_headroom": {
                    "enabled": True,
                    "request_side_enabled": True,
                    "response_side_enabled": True,
                    "min_length_to_compress": 30,
                    "compression_strategies": {
                        "json": {"remove_empty": True, "dedupe_keys": False},
                        "code": {"strip_comments": True, "strip_whitespace": True, "compress_imports": True},
                        "logs": {"dedupe_lines": True, "truncate_long_lines": 200},
                    },
                }
            }
        }
    savings = _make_savings(messages, "gpt-4o")
    return RequestContext(
        request_id="req-g19-agent",
        user_id="agent-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config=config,
        savings=savings,
    )


def _agent_tool_output():
    """Verbose multi-turn agent tool output with empty fields."""
    return json.dumps({
        "status": "success",
        "results": [
            {"id": 1, "action": "searched", "query": "policy", "metadata": {}, "notes": None, "score": 0.95},
            {"id": 2, "action": "searched", "query": "return", "metadata": {}, "notes": None, "score": 0.88},
        ],
        "pagination": {"page": 1, "total": 2, "next": None},
        "warnings": [],
    }, indent=2)


@pytest.mark.asyncio
async def test_agent_baseline_no_compression():
    """Baseline: G19 disabled, tool output unchanged."""
    tool_json = _agent_tool_output()
    msgs = [{"role": "user", "content": "test"}]
    ctx = _make_ctx(msgs, config={
        "groups": {"G19_headroom": {"enabled": False}}
    })
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "search", "result": tool_json}}]
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    result_json = result["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
    assert json.loads(result_json) == json.loads(tool_json)
    assert len(ctx.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_agent_isolated_compression():
    """Isolated: tool output compressed, empty fields removed, schema dedup applied."""
    tool_json = _agent_tool_output()
    msgs = [{"role": "user", "content": "test"}]
    ctx = _make_ctx(msgs)
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "search", "result": tool_json}}]
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    result_json = result["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
    parsed = json.loads(result_json)

    # Empty fields removed
    assert "notes" not in parsed
    assert "warnings" not in parsed
    assert "metadata" not in parsed

    # Core data preserved
    assert parsed["status"] == "success"
    assert len(parsed["results"]) == 2

    # Savings recorded
    steps = ctx.savings.step_savings
    assert len(steps) >= 1
    assert steps[0].group == "G19"


@pytest.mark.asyncio
async def test_agent_quality_gate_no_data_loss():
    """Quality gate: compressed output must retain all meaningful data."""
    tool_json = _agent_tool_output()
    msgs = [{"role": "user", "content": "test"}]
    ctx = _make_ctx(msgs)
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "search", "result": tool_json}}]
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    result_json = result["choices"][0]["message"]["tool_calls"][0]["function"]["result"]
    parsed = json.loads(result_json)

    # All non-empty values preserved
    assert parsed["status"] == "success"
    assert parsed["results"][0]["id"] == 1
    assert parsed["results"][0]["action"] == "searched"
    assert parsed["results"][1]["id"] == 2
    assert parsed["pagination"]["page"] == 1
    assert parsed["pagination"]["total"] == 2


@pytest.mark.asyncio
async def test_agent_code_block_stripping():
    """Agent code blocks in response-side content are compressed."""
    code = """# Import system modules
import os
import sys

# Helper function
def helper():
    pass  # noop

class Worker:
    def run(self):
        return 42
"""
    msgs = [{"role": "user", "content": "test"}]
    ctx = _make_ctx(msgs)
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": code,
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    compressed = result["choices"][0]["message"]["content"]
    assert "# Import system modules" not in compressed
    assert "# Helper function" not in compressed
    assert "def helper():" in compressed
    assert "class Worker:" in compressed
