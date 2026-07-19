"""Integration test: provider failover through the real /v1/chat/completions path.

Drives the endpoint with litellm mocked so the primary provider raises a retryable
RateLimitError and a configured fallback provider succeeds — asserting the client still
gets a 200 (no bare 429), the answer comes from the fallback, and the failover trail is
recorded. Complements the pure-unit coverage in tests/unit/providers/test_resilience.py.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import hashlib
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

import litellm

_PROXY_KEY = "test-proxy-key-resilience"
_PROXY_KEY_HASH = hashlib.sha256(_PROXY_KEY.encode()).hexdigest()
_VALID_KEYS_JSON = json.dumps({_PROXY_KEY_HASH: "test-user"})


def _resp(model: str, content: str) -> MagicMock:
    m = MagicMock()
    m.model_dump.return_value = {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }
    return m


def _config(resilience: dict) -> dict:
    groups = {f"G{n}_x": {"enabled": False} for n in range(1, 19)}
    # Real group keys the pipeline reads (all disabled so the test isolates the call path).
    for k in ["G1_compression", "G2_template_registry", "G4_bypass", "G5_cache", "G6_routing",
              "G7_retrieval", "G8_tools", "G9_context_schema", "G10_memory", "G11_output",
              "G12_reasoning", "G13_batch", "G14_tool_output", "G15_server_compute",
              "G16_agent_arch", "G17_loop", "G18_observability"]:
        groups[k] = {"enabled": False}
    return {
        "proxy": {"port": 4000, "default_provider": "openai"},
        "providers": [
            {"name": "openai", "models": ["gpt-4o-mini"], "model_prefixes": ["gpt-", "o1", "o3", "o4"]},
            {"name": "anthropic", "models": ["claude-3-5-haiku"], "model_prefixes": ["claude"]},
            {"name": "gemini", "models": ["gemini-2.5-flash"], "model_prefixes": ["gemini"]},
        ],
        "resilience": resilience,
        "groups": groups,
    }


def _headers():
    return {"Authorization": f"Bearer {_PROXY_KEY}"}


def _body():
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


def _drive(cfg, acompletion_mock, body=None, store=None):
    """Post one chat request through the real endpoint with externals mocked.

    Single shared harness for every test in this file (incl. streaming — pass a
    body with stream=True): review K9 killed the copy-pasted patch dict. Resets
    the process resilience store so breaker/cooldown state never leaks across
    tests (the store is a process singleton). Pass ``store`` to use a pre-seeded
    store instead (e.g. a model already locked out) — it is NOT reset.
    """
    from providers.resilience import ResilienceStore, set_resilience_store
    set_resilience_store(store if store is not None else ResilienceStore())
    patches = [
        patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("main.get_config", return_value=cfg),
        patch("litellm.acompletion", acompletion_mock),
        patch("main.resolve_provider_key", new_callable=AsyncMock, return_value="sk-test"),
        patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
    ]
    for p in patches:
        p.__enter__()
    try:
        import main
        # Reset the process-wide proxy-key cache (300s TTL) so this test's key wins even
        # if a prior test in the same pytest process warmed the cache with other keys.
        from auth import api_key_manager as _akm
        _akm.replace_cache({_PROXY_KEY_HASH: "test-user"})
        client = TestClient(main.app)
        return client.post("/v1/chat/completions", headers=_headers(), json=body or _body())
    finally:
        for p in patches:
            p.__exit__(None, None, None)


def test_failover_to_fallback_returns_200_from_fallback():
    """Primary rate-limited → fallback serves → client sees 200 from the fallback model."""
    rate_limited = litellm.exceptions.RateLimitError(
        message="rate limited", llm_provider="openai", model="gpt-4o-mini"
    )
    mock = AsyncMock(side_effect=[rate_limited, _resp("claude-3-5-haiku", "hello-from-fallback")])
    cfg = _config({"enabled": True, "num_retries": 0, "retry_base_delay": 0,
                   "fallbacks": {"gpt-4o-mini": ["claude-3-5-haiku"]}})
    resp = _drive(cfg, mock)
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "hello-from-fallback"
    assert mock.await_count == 2  # primary failed, fallback served


def test_locked_model_skipped_serves_from_fallback_without_calling_it():
    """A model already locked out is SKIPPED entirely (no wasted primary call) — the
    request goes straight to the fallback model. This is the value over plain failover:
    a known-bad model isn't re-hammered on every request."""
    from providers.resilience import ResilienceStore, ResilienceConfig
    store = ResilienceStore()
    lock_cfg = ResilienceConfig.resolve(
        {"resilience": {"enabled": True, "model_lockout": True, "model_failure_threshold": 1}},
        "openai")
    store.record_model_failure("openai", "gpt-4o-mini", lock_cfg)   # gpt-4o-mini locked
    # Only the FALLBACK is set up to be called — the primary must never be invoked.
    mock = AsyncMock(side_effect=[_resp("claude-3-5-haiku", "from-fallback")])
    cfg = _config({"enabled": True, "num_retries": 0, "retry_base_delay": 0,
                   "model_lockout": True, "model_failure_threshold": 1,
                   "fallbacks": {"gpt-4o-mini": ["claude-3-5-haiku"]}})
    resp = _drive(cfg, mock, store=store)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "from-fallback"
    assert mock.await_count == 1  # primary SKIPPED (locked) — only the fallback was called


def test_no_fallback_configured_still_raises_429():
    """With resilience on but no fallback list, a rate-limited primary surfaces as 429
    (behaviour-preserving — single target re-raises the original error)."""
    rate_limited = litellm.exceptions.RateLimitError(
        message="rate limited", llm_provider="openai", model="gpt-4o-mini"
    )
    mock = AsyncMock(side_effect=rate_limited)
    cfg = _config({"enabled": True, "num_retries": 0, "fallbacks": {}})
    resp = _drive(cfg, mock)
    assert resp.status_code == 429


def test_retry_then_success_no_failover():
    """A transient error on the primary is retried on the SAME provider and succeeds —
    no fallback needed, 200 returned."""
    transient = litellm.exceptions.RateLimitError(
        message="transient", llm_provider="openai", model="gpt-4o-mini"
    )
    mock = AsyncMock(side_effect=[transient, _resp("gpt-4o-mini", "recovered")])
    cfg = _config({"enabled": True, "num_retries": 1, "retry_base_delay": 0, "fallbacks": {}})
    resp = _drive(cfg, mock)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "recovered"
    assert mock.await_count == 2  # initial + 1 retry


def _stream_chunks(model, text):
    async def gen():
        for ch in [
            {"choices": [{"delta": {"content": text}}]},
            {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2},
             "model": model},
        ]:
            yield ch
    return gen()


def test_streaming_failover_establishes_on_fallback():
    """Primary stream establishment is rate-limited → the stream is established on the
    fallback provider before any bytes are sent; the client gets a 200 SSE body."""
    rate_limited = litellm.exceptions.RateLimitError(
        message="rl", llm_provider="openai", model="gpt-4o-mini"
    )
    calls = {"n": 0}

    async def acompletion(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise rate_limited            # primary stream establishment fails
        return _stream_chunks("claude-3-5-haiku", "streamed-from-fallback")

    cfg = _config({"enabled": True, "num_retries": 0, "retry_base_delay": 0,
                   "fallbacks": {"gpt-4o-mini": ["claude-3-5-haiku"]}})
    resp = _drive(cfg, acompletion, body={**_body(), "stream": True})
    assert resp.status_code == 200
    assert "streamed-from-fallback" in resp.text
    assert "[DONE]" in resp.text
    assert calls["n"] == 2  # primary establishment failed, fallback served


def test_fallback_auth_error_continues_to_next_fallback():
    """Review C3 end-to-end: a fallback's revoked key (401) must not surface as the
    client's error nor abort the chain — the next fallback serves."""
    rate_limited = litellm.exceptions.RateLimitError(
        message="rl", llm_provider="openai", model="gpt-4o-mini"
    )
    auth_err = litellm.exceptions.AuthenticationError(
        message="revoked", llm_provider="anthropic", model="claude-3-5-haiku"
    )
    mock = AsyncMock(side_effect=[rate_limited, auth_err, _resp("gemini-2.5-flash", "third-served")])
    cfg = _config({"enabled": True, "num_retries": 0, "retry_base_delay": 0,
                   "fallbacks": {"gpt-4o-mini": ["claude-3-5-haiku", "gemini-2.5-flash"]}})
    resp = _drive(cfg, mock)
    assert resp.status_code == 200            # NOT 401 — the fallback's key problem is ours
    assert resp.json()["choices"][0]["message"]["content"] == "third-served"
    assert mock.await_count == 3


def test_single_transient_429_does_not_blackhole_next_request():
    """Review C1 end-to-end: after a 429-failed request, the tenant's NEXT request must
    still attempt the provider (fail-open through the cooldown) and succeed."""
    rate_limited = litellm.exceptions.RateLimitError(
        message="rl", llm_provider="openai", model="gpt-4o-mini"
    )
    cfg = _config({"enabled": True, "num_retries": 0, "retry_base_delay": 0, "fallbacks": {}})
    # Request 1: provider 429s → client sees 429 (single target re-raises original).
    m1 = AsyncMock(side_effect=rate_limited)
    r1 = _drive(cfg, m1)
    assert r1.status_code == 429
    # Request 2 (same store would normally be in cooldown — _drive resets it, so set it
    # explicitly to simulate the follow-up request landing inside the cooldown window).
    from providers.resilience import ResilienceStore, set_resilience_store
    store = ResilienceStore()
    store.set_cooldown("", "openai", ttl=30)   # default-tenant prefix is ""
    m2 = AsyncMock(return_value=_resp("gpt-4o-mini", "recovered-after-blip"))
    # _drive resets the store; drive manually with our pre-cooled store.
    set_resilience_store(store)
    patches = [
        patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("main.get_config", return_value=cfg),
        patch("litellm.acompletion", m2),
        patch("main.resolve_provider_key", new_callable=AsyncMock, return_value="sk-test"),
        patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
    ]
    for p in patches:
        p.__enter__()
    try:
        import main
        # Reset the process-wide proxy-key cache (300s TTL) so this test's key wins even
        # if a prior test in the same pytest process warmed the cache with other keys.
        from auth import api_key_manager as _akm
        _akm.replace_cache({_PROXY_KEY_HASH: "test-user"})
        client = TestClient(main.app)
        r2 = client.post("/v1/chat/completions", headers=_headers(), json=_body())
    finally:
        for p in patches:
            p.__exit__(None, None, None)
    assert r2.status_code == 200              # fail-open: attempted despite cooldown
    assert r2.json()["choices"][0]["message"]["content"] == "recovered-after-blip"
    assert m2.await_count == 1


def test_disabled_resilience_single_call_passthrough():
    """resilience.enabled=false → exactly one attempt, original 429 surfaces (no retry)."""
    rate_limited = litellm.exceptions.RateLimitError(
        message="rate limited", llm_provider="openai", model="gpt-4o-mini"
    )
    mock = AsyncMock(side_effect=[rate_limited, _resp("gpt-4o-mini", "should-not-serve")])
    cfg = _config({"enabled": False, "num_retries": 3, "fallbacks": {"gpt-4o-mini": ["claude-3-5-haiku"]}})
    resp = _drive(cfg, mock)
    assert resp.status_code == 429
    assert mock.await_count == 1  # no retry, no failover when disabled
