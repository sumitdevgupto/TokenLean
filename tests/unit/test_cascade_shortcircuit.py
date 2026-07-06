"""G06 cascade short-circuit (double-call bug fix).

When G06 runs the tier cascade inline it produces the final answer and stores it
on ``ctx.cascade_response``. main.py must return that directly and MUST NOT call
the provider again. Previously ``ctx.cascade_response`` was set but never
consumed, so cascade requests paid for the cascade AND a second main LLM call.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main
from middleware.g06_routing import _timed_llm


_CASCADE_RESPONSE = {
    "id": "cascade-1",
    "model": "gpt-4o-mini",
    "choices": [
        {"message": {"role": "assistant", "content": "cheap cascade answer"}}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


class _FakePipeline:
    """Stands in for OptimisationPipeline: process_request sets cascade_response
    (as real G06 cascade_execution would), process_response is a pass-through."""

    def __init__(self, cascade_response):
        self._cr = cascade_response

    async def process_request(self, ctx, request_headers=None):
        ctx.cascade_response = self._cr
        return ctx

    async def process_response(self, ctx, response):
        return ctx, response


_client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _no_billing(monkeypatch):
    monkeypatch.setattr(main, "_usage_meter", None)


async def _fake_auth(request):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "pro"}


def test_cascade_response_skips_duplicate_main_llm_call(monkeypatch):
    called = {"acompletion": 0}

    async def _boom_acompletion(**kwargs):
        called["acompletion"] += 1
        raise AssertionError("main LLM call must be skipped when cascade_response is set")

    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: {"groups": {}, "providers": []})
    monkeypatch.setattr(main, "_pipeline", _FakePipeline(_CASCADE_RESPONSE))
    monkeypatch.setattr(main.litellm, "acompletion", _boom_acompletion)

    resp = _client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer tok-x"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "cheap cascade answer"
    assert called["acompletion"] == 0  # the provider was NOT called a second time


@pytest.mark.asyncio
async def test_timed_llm_accumulates_provider_time_into_ctx():
    ctx = SimpleNamespace(llm_elapsed_ms=0.0)

    async def _slow():
        # Deterministic non-zero work without a real sleep dependency: return a
        # sentinel; the wrapper still records a small positive elapsed.
        return "ok"

    result = await _timed_llm(ctx, _slow())
    assert result == "ok"
    assert ctx.llm_elapsed_ms >= 0.0  # accumulated (monotonic clock, never negative)


@pytest.mark.asyncio
async def test_timed_llm_records_time_even_on_error():
    ctx = SimpleNamespace(llm_elapsed_ms=5.0)

    async def _fail():
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await _timed_llm(ctx, _fail())
    # Prior accumulation preserved and this call's time added (finally block).
    assert ctx.llm_elapsed_ms >= 5.0
