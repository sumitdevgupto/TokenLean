"""G32 tool-eligibility on the paths that DON'T run the response pipeline.

Three separate concerns, all about coverage gaps rather than the gate's own logic:

1. **Cache hits / bypasses** return from ``main`` without touching
   ``pipeline.process_response`` at all. Without an explicit hoist a cached answer
   carrying a tool call would sail past the gate — and because the gate also runs when
   the entry is *stored*, a policy tightened afterwards would never reach it. G32 is a
   trust & safety group; it must not be bypassable.

2. The hoist must call **only G32**, never the full chain — that chain also re-runs G18
   observability, re-applies G29 response redaction, and ends in ``g05.store_response``,
   so re-running it on a cache hit would double-record and double-store.

3. **Streaming is knowingly NOT gated.** The test below pins that, so a future change to
   ``_stream_response`` cannot silently invalidate what the README/config-reference
   promise. If it starts failing, the docs need updating — that is the point.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest
from fastapi.testclient import TestClient

import main
from middleware.g32_tool_eligibility import G32ToolEligibility

_client = TestClient(main.app)

_POLICY_CFG = {
    "groups": {
        "G32_tool_eligibility": {
            "enabled": True, "mode": "block", "policy": {"deny": ["shell_exec"]},
        }
    },
    "providers": [],
}


def _cached_response_with_tool_call():
    return {
        "id": "chatcmpl-cached",
        "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function",
                                        "function": {"name": "shell_exec", "arguments": "{}"}}]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class _CacheHitPipeline:
    """Stands in for OptimisationPipeline: process_request reports a cache hit carrying
    a tool call, exactly as G05 would after serving a stored entry."""

    def __init__(self, response):
        self._response = response
        self.g32 = G32ToolEligibility()
        self.process_response_calls = 0

    async def process_request(self, ctx, request_headers=None):
        ctx.cache_hit = True
        ctx.cache_response = self._response
        return ctx

    async def process_response(self, ctx, response):
        self.process_response_calls += 1
        return ctx, response


@pytest.fixture(autouse=True)
def _no_billing(monkeypatch):
    monkeypatch.setattr(main, "_usage_meter", None)


async def _fake_auth(request):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "enterprise"}


def _post(monkeypatch, pipeline, config=_POLICY_CFG):
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: config)
    monkeypatch.setattr(main, "_pipeline", pipeline)
    return _client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer tok-x"},
    )


# ── 1. the gate is not bypassable by the cache ────────────────────────────────
def test_cache_hit_with_denied_tool_call_is_still_stripped(monkeypatch):
    pipeline = _CacheHitPipeline(_cached_response_with_tool_call())
    resp = _post(monkeypatch, pipeline)

    assert resp.status_code == 200
    choice = resp.json()["choices"][0]
    assert "tool_calls" not in choice["message"], (
        "a cached response carrying a denied tool call was served ungated — "
        "the cache/bypass short-circuit must run G32"
    )
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"]


def test_cache_hit_hoist_does_not_rerun_the_whole_response_chain(monkeypatch):
    # Re-running process_response here would double-record billing/observability and
    # re-store the cache entry. Only G32 may run.
    pipeline = _CacheHitPipeline(_cached_response_with_tool_call())
    resp = _post(monkeypatch, pipeline)

    assert resp.status_code == 200
    assert pipeline.process_response_calls == 0


def test_cache_hit_with_allowed_tool_call_is_untouched(monkeypatch):
    allowed = _cached_response_with_tool_call()
    allowed["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "db_read"
    pipeline = _CacheHitPipeline(allowed)
    resp = _post(monkeypatch, pipeline)

    calls = resp.json()["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["db_read"]


def test_hoist_failure_still_serves_the_cache_hit(monkeypatch):
    # Best-effort: a bug in the gate must not turn a served cache hit into a 500.
    class _BoomG32:
        async def process_response(self, ctx, response):
            raise RuntimeError("gate exploded")

    pipeline = _CacheHitPipeline(_cached_response_with_tool_call())
    pipeline.g32 = _BoomG32()
    resp = _post(monkeypatch, pipeline)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["tool_calls"]   # served as-is


# ── 3. streaming is knowingly NOT gated (pins the documented limitation) ──────
def test_streaming_path_does_not_run_the_response_pipeline():
    """`_stream_response` relays provider chunks unchanged, so G32 never sees a
    streamed tool call. This is a DOCUMENTED limitation (README + config-reference +
    the marketing one-liner all say 'non-streaming'). If this assertion ever fails,
    streaming gating has been added and those docs must be updated to match."""
    import inspect
    src = inspect.getsource(main._stream_response)
    assert "process_response" not in src, (
        "_stream_response now calls the response pipeline — G32's documented "
        "'non-streaming only' limitation is stale and the docs must be updated"
    )
    # And the contract is stated where a reader will find it.
    assert "response-side pipeline" in inspect.getdoc(main._stream_response)
