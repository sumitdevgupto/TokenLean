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
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g19_headroom import G19Headroom
from middleware import RequestContext
from tests.conftest import _make_savings


@pytest.fixture(autouse=True)
def _pin_the_builtin_compactor():
    """Pin these tests to the BUILT-IN compactor, which is the contract they assert.

    Without this they silently depend on whether the optional `headroom` package happens
    to be installed. It is a pinned production dependency (`headroom-ai==0.34.0`) and IS
    present in the container and in CI, but is absent from a typical dev machine — so
    these four tests passed locally and for the OSS gate while failing in CI the moment
    the gate was widened to run them (2026-09-04). `headroom.SmartCrusher` takes a
    different path for arrays of records: it emits TOON-style compaction, so `results`
    becomes a string rather than a list and empty keys are not pruned. Neither behaviour
    is wrong; asserting one while running the other is.

    `test_g19_structured.py` already patches this flag both ways; this applies the same
    discipline here. The headroom path is covered explicitly at the bottom of this file.
    """
    from middleware import g19_headroom as mod
    with patch.object(mod, "_headroom_available", False):
        yield


def _make_ctx(messages, config=None):
    if config is None:
        config = {
            "groups": {
                "G19_headroom": {
                    "enabled": True,
                    "request_side_enabled": True,
                    "response_side_enabled": True,
                    # These tests exercise the ANSWER-content compressors, which are
                    # opt-in since 2026-08-05 (the default returns answers verbatim —
                    # covered by TestAnswerFidelity in test_g19_structured.py).
                    "response_side_compress_answers": True,
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


# ─── The path that actually ships ────────────────────────────────────────────
# `headroom-ai==0.34.0` is a pinned production dependency, so the container and CI take
# the SmartCrusher branch while a bare dev machine takes the built-in one. Until the test
# gate was widened on 2026-09-04, nothing ever executed these files with headroom present,
# and the difference went unnoticed for as long as both paths existed.
#
# The fake below mirrors what real headroom produced in CI (run 33898297256): the outer
# JSON envelope survives and is still parseable; an inner array of records becomes a
# TOON-style string; and empty keys are NOT pruned, unlike the built-in compactor.

def _headroom_like(text):
    """Stand-in for SmartCrusher.compact_document_json, shaped from observed output."""
    doc = json.loads(text)
    if isinstance(doc.get("results"), list):
        doc["results"] = ("[%d]{action:string,id:int}\n" % len(doc["results"])) + "".join(
            "%s,%s\n" % (r.get("action", ""), r.get("id", "")) for r in doc["results"])
    return json.dumps(doc, separators=(",", ":"))


async def _run_with_headroom(tool_json):
    from middleware import g19_headroom as mod
    sc = MagicMock()
    sc.compact_document_json.side_effect = _headroom_like
    ctx = _make_ctx([{"role": "user", "content": "test"}])
    response = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "search", "result": tool_json}}]}}]}
    with patch.object(mod, "_headroom_available", True), \
         patch.object(mod, "_smart_crusher", sc):
        out = await mod.G19Headroom().process_response(ctx, response)
    return out["choices"][0]["message"]["tool_calls"][0]["function"]["result"], sc


@pytest.mark.asyncio
async def test_headroom_path_keeps_the_tool_result_parseable():
    """The client-facing contract: a tool result stays valid JSON.

    This is what makes the TOON compaction safe to ship — an agent that does
    `json.loads(result)` still works. If a future headroom version returns something
    non-JSON, G19's only guard is a LENGTH check (`0 < len(crushed) < len(text)`), so this
    assertion is the thing standing between that and a broken client.
    """
    result, sc = await _run_with_headroom(_agent_tool_output())
    assert sc.compact_document_json.called, "the shipped path must actually be exercised"
    json.loads(result)          # must not raise


@pytest.mark.asyncio
async def test_headroom_path_differs_from_the_builtin_and_that_is_expected():
    """Documents the divergence rather than pretending it does not exist: the built-in
    compactor prunes empty keys and keeps `results` a list; headroom does neither."""
    result, _ = await _run_with_headroom(_agent_tool_output())
    parsed = json.loads(result)
    assert isinstance(parsed["results"], str), "headroom TOON-encodes arrays of records"
    assert parsed["status"] == "success", "scalar fields still survive intact"
