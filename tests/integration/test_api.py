"""
API endpoint integration tests via FastAPI TestClient.
All external calls (litellm, Redis, Secret Manager, Langfuse) are mocked.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


# ─── Test credentials ────────────────────────────────────────────────────────

_PROXY_KEY = "test-proxy-key-integration"
_PROXY_KEY_HASH = hashlib.sha256(_PROXY_KEY.encode()).hexdigest()
_VALID_KEYS_JSON = json.dumps({_PROXY_KEY_HASH: "test-user"})

_LLM_RESPONSE = MagicMock()
_LLM_RESPONSE.model_dump.return_value = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
}


def _make_client() -> TestClient:
    """Build a TestClient with all external dependencies mocked."""
    with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
         patch("config_loader.load_config"), \
         patch("config_loader.start_hot_reload"), \
         patch("config_loader.get_config", return_value=_test_config()):
        import main
        # Reset pipeline state
        import importlib
        importlib.reload(main)
        return TestClient(main.app)


def _test_config():
    return {
        "proxy": {"port": 4000},
        "providers": [{"name": "openai", "models": ["gpt-4o", "gpt-4o-mini"]}],
        "groups": {
            "G1_compression": {"enabled": False},
            "G2_template_registry": {"enabled": False},
            "G4_bypass": {"enabled": False},
            "G5_cache": {"enabled": False},
            "G6_routing": {"enabled": False},
            "G7_retrieval": {"enabled": False},
            "G8_tools": {"enabled": False},
            "G9_context_schema": {"enabled": False},
            "G10_memory": {"enabled": False},
            "G11_output": {"enabled": False},
            "G12_reasoning": {"enabled": False},
            "G13_batch": {"enabled": False},
            "G14_tool_output": {"enabled": False},
            "G15_server_compute": {"enabled": False},
            "G16_agent_arch": {"enabled": False},
            "G17_loop": {"enabled": False},
            "G18_observability": {"enabled": False},
        },
    }


def _auth_headers():
    return {"Authorization": f"Bearer {_PROXY_KEY}"}


def _chat_body(content="What is the capital of France?"):
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}]}


# ─── /health ──────────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_200(self):
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
             patch("config_loader.load_config"), \
             patch("config_loader.start_hot_reload"), \
             patch("main.get_config", return_value=_test_config()):
            import main
            client = TestClient(main.app)
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


# ─── /v1/models ───────────────────────────────────────────────────────────────

class TestModelsEndpoint:
    def test_models_list_non_empty(self):
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
             patch("config_loader.load_config"), \
             patch("config_loader.start_hot_reload"), \
             patch("main.get_config", return_value=_test_config()):
            import main
            client = TestClient(main.app)
            response = client.get("/v1/models", headers=_auth_headers())
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert len(data["data"]) > 0

    def test_models_requires_auth(self):
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
             patch("config_loader.load_config"), \
             patch("config_loader.start_hot_reload"), \
             patch("main.get_config", return_value=_test_config()):
            import main
            client = TestClient(main.app)
            response = client.get("/v1/models")
        assert response.status_code == 401


# ─── /v1/chat/completions ─────────────────────────────────────────────────────

class TestChatCompletionsEndpoint:
    def _client_and_patches(self):
        patches = {
            "auth": patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON),
            "load_config": patch("config_loader.load_config"),
            "hot_reload": patch("config_loader.start_hot_reload"),
            "get_config": patch("main.get_config", return_value=_test_config()),
            "litellm": patch("litellm.acompletion", new_callable=AsyncMock, return_value=_LLM_RESPONSE),
            "provider_key": patch("main.get_llm_provider_key", return_value="sk-test-key"),
            "g05_store": patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
            "g18_emit": patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
        }
        active = {k: v.__enter__() for k, v in patches.items()}
        import main
        client = TestClient(main.app)
        return client, patches, active

    def _teardown(self, patches, active):
        for k, p in patches.items():
            p.__exit__(None, None, None)

    def test_valid_request_returns_200(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            self._teardown(patches, active)
        assert response.status_code == 200

    def test_missing_auth_returns_401(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post("/v1/chat/completions", json=_chat_body())
        finally:
            self._teardown(patches, active)
        assert response.status_code == 401

    def test_invalid_key_returns_401(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bad-key"},
                json=_chat_body(),
            )
        finally:
            self._teardown(patches, active)
        assert response.status_code == 401

    def test_response_has_choices(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            self._teardown(patches, active)
        body = response.json()
        assert "choices" in body
        assert len(body["choices"]) > 0

    def test_token_opt_field_present(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            self._teardown(patches, active)
        body = response.json()
        assert "_token_opt" in body, "_token_opt missing from response"

    def test_savings_required_fields_present(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            self._teardown(patches, active)
        opt = response.json().get("_token_opt", {})
        for key in ("baseline_tokens", "total_abs_saving", "total_pct_saving",
                    "cost_saving_usd", "step_savings"):
            assert key in opt, f"Missing _token_opt.{key}"

    def test_savings_numerical_sanity(self):
        client, patches, active = self._client_and_patches()
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            self._teardown(patches, active)
        opt = response.json()["_token_opt"]
        assert opt["total_abs_saving"] >= 0
        assert 0.0 <= opt["total_pct_saving"] <= 100.0
        assert opt["baseline_tokens"] > 0

    def test_bypass_skips_litellm(self):
        bypass_config = _test_config()
        bypass_config["groups"]["G4_bypass"] = {
            "enabled": True,
            "rules": [{"name": "greet", "keywords": ["hello"], "static_response": "Hi!"}],
        }
        patches = {
            "auth": patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON),
            "load_config": patch("config_loader.load_config"),
            "hot_reload": patch("config_loader.start_hot_reload"),
            "get_config": patch("main.get_config", return_value=bypass_config),
            "litellm": patch("litellm.acompletion", new_callable=AsyncMock, return_value=_LLM_RESPONSE),
            "provider_key": patch("main.get_llm_provider_key", return_value="sk-test-key"),
        }
        active = {k: v.__enter__() for k, v in patches.items()}
        try:
            import main
            client = TestClient(main.app)
            response = client.post(
                "/v1/chat/completions",
                headers=_auth_headers(),
                json=_chat_body("hello there"),
            )
        finally:
            self._teardown(patches, active)

        assert response.status_code == 200
        # litellm should NOT have been called
        assert active["litellm"].call_count == 0

    def test_cache_hit_skips_litellm(self):
        cached = json.dumps({
            "id": "cached-1",
            "choices": [{"message": {"role": "assistant", "content": "Cached Paris"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 5},
        })
        cache_config = _test_config()
        cache_config["groups"]["G5_cache"] = {"enabled": True}

        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=cached)
        mock_redis.aclose = AsyncMock()

        patches = {
            "auth": patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON),
            "load_config": patch("config_loader.load_config"),
            "hot_reload": patch("config_loader.start_hot_reload"),
            "get_config": patch("main.get_config", return_value=cache_config),
            "litellm": patch("litellm.acompletion", new_callable=AsyncMock, return_value=_LLM_RESPONSE),
            "provider_key": patch("main.get_llm_provider_key", return_value="sk-test-key"),
            "redis": patch("middleware.g05_cache._get_redis", return_value=mock_redis),
        }
        active = {k: v.__enter__() for k, v in patches.items()}
        try:
            import main
            client = TestClient(main.app)
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            self._teardown(patches, active)

        assert response.status_code == 200
        assert active["litellm"].call_count == 0


# ─── /ingest-doc ──────────────────────────────────────────────────────────────

class TestIngestDocEndpoint:
    def test_valid_payload_returns_202_or_200(self):
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
             patch("config_loader.load_config"), \
             patch("config_loader.start_hot_reload"), \
             patch("main.get_config", return_value=_test_config()), \
             patch("main.trigger_doc_ingestion", new_callable=AsyncMock, return_value=True):
            import main
            client = TestClient(main.app)
            response = client.post("/ingest-doc", json={"bucket": "my-bucket", "name": "docs/file.pdf"})
        assert response.status_code in (200, 202)
        assert response.json().get("triggered") is True

    def test_missing_payload_returns_400(self):
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
             patch("config_loader.load_config"), \
             patch("config_loader.start_hot_reload"), \
             patch("main.get_config", return_value=_test_config()):
            import main
            client = TestClient(main.app)
            response = client.post("/ingest-doc", json={"bucket": "my-bucket"})
        assert response.status_code == 400
