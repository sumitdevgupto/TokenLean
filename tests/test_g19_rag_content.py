"""
G19 ROI ablation — DS2 RAG KB.

Validates:
  - Baseline (G19 off): doc-chunk metadata preserved verbatim
  - Isolated (G19 on): metadata JSON compacted, empty fields removed
  - Gain: 20-40% on RAG context
  - Quality gate: no key fields lost (chunk_id, content, source)
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
        request_id="req-g19-rag",
        user_id="rag-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model="gpt-4o",
        routed_model="gpt-4o",
        params={},
        config=config,
        savings=savings,
    )


def _rag_chunks():
    """RAG retrieval result with metadata-heavy JSON."""
    return json.dumps({
        "chunks": [
            {
                "chunk_id": "doc-1-chunk-3",
                "content": "Return policy allows 30 days with receipt.",
                "source_doc": "policy_v2.pdf",
                "page": 12,
                "score": 0.91,
                "embedding": [],
                "tags": [],
                "lang": "en",
                "created_by": None,
                "reviewed": False,
                "extra": {},
            },
            {
                "chunk_id": "doc-2-chunk-1",
                "content": "Free shipping on orders over $50.",
                "source_doc": "shipping_v1.pdf",
                "page": 4,
                "score": 0.85,
                "embedding": [],
                "tags": [],
                "lang": "en",
                "created_by": None,
                "reviewed": False,
                "extra": {},
            },
        ],
        "query": "return policy",
        "total_hits": 2,
        "latency_ms": 45,
    }, indent=2)


@pytest.mark.asyncio
async def test_rag_baseline_no_compression():
    """Baseline: G19 disabled, RAG chunks unchanged."""
    rag_json = _rag_chunks()
    msgs = [{"role": "user", "content": "What is the return policy?"}]
    ctx = _make_ctx(msgs, config={
        "groups": {"G19_headroom": {"enabled": False}}
    })
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": rag_json,
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    assert result["choices"][0]["message"]["content"] == rag_json
    assert len(ctx.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_rag_isolated_compression():
    """Isolated: RAG metadata compacted, schema dedup applied."""
    rag_json = _rag_chunks()
    msgs = [{"role": "user", "content": "What is the return policy?"}]
    ctx = _make_ctx(msgs)
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": rag_json,
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    compressed = result["choices"][0]["message"]["content"]
    parsed = json.loads(compressed)

    # Empty/null fields removed
    assert "embedding" not in parsed["chunks"][0]
    assert "tags" not in parsed["chunks"][0]
    assert "created_by" not in parsed["chunks"][0]
    assert "extra" not in parsed["chunks"][0]

    # Key fields preserved
    assert parsed["chunks"][0]["chunk_id"] == "doc-1-chunk-3"
    assert parsed["chunks"][0]["content"] == "Return policy allows 30 days with receipt."
    assert parsed["chunks"][0]["source_doc"] == "policy_v2.pdf"
    assert parsed["chunks"][1]["chunk_id"] == "doc-2-chunk-1"

    # Savings recorded
    steps = ctx.savings.step_savings
    assert len(steps) >= 1
    assert steps[0].group == "G19"


@pytest.mark.asyncio
async def test_rag_quality_gate_key_fields_preserved():
    """Quality gate: chunk_id, content, source_doc must survive compression."""
    rag_json = _rag_chunks()
    msgs = [{"role": "user", "content": "What is the return policy?"}]
    ctx = _make_ctx(msgs)
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": rag_json,
            }
        }]
    }
    g19 = G19Headroom()
    result = await g19.process_response(ctx, response)
    parsed = json.loads(result["choices"][0]["message"]["content"])

    for chunk in parsed["chunks"]:
        assert "chunk_id" in chunk
        assert "content" in chunk
        assert "source_doc" in chunk
        assert "score" in chunk
