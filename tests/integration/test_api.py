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
    def _client_and_patches(self, config=None):
        patches = {
            "auth": patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON),
            "load_config": patch("config_loader.load_config"),
            "hot_reload": patch("config_loader.start_hot_reload"),
            "get_config": patch("main.get_config", return_value=config or _test_config()),
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

    def test_body_fields_cannot_become_upstream_call_arguments(self):
        # litellm reads these as arguments of the CALL (where it goes, which headers and
        # body it carries, a canned reply), so a client body must never supply them.
        # Documented parameters must still arrive.
        client, patches, active = self._client_and_patches()
        body = {
            **_chat_body(), "temperature": 0.2, "x_session_id": "s-1",
            "api_base": "https://attacker.example/v1", "base_url": "https://attacker.example/v1",
            "extra_headers": {"x-exfil": "1"}, "extra_body": {"model": "gpt-4o"},
            "mock_response": "canned", "_native_batch": True,
        }
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=body)
        finally:
            self._teardown(patches, active)
        assert response.status_code == 200
        sent = active["litellm"].call_args.kwargs
        for field in ("api_base", "base_url", "extra_headers", "extra_body", "mock_response", "_native_batch"):
            assert field not in sent, f"client body field {field!r} reached litellm.acompletion"
        assert sent.get("temperature") == 0.2

    def test_a_key_the_resolver_refuses_is_a_402_and_no_provider_call(self):
        """The strict-BYOK resolver refuses, among others, a provider whose calls are signed
        with platform-held credentials (Bedrock), whatever key the tenant stored for it."""
        from providers.key_resolver import (
            ProviderKeyError, reset_provider_key_resolver, set_provider_key_resolver)

        async def _refuse(provider, tenant_id, ctx=None):
            raise ProviderKeyError(provider, tenant_id, "not available with your own key")

        client, patches, active = self._client_and_patches()
        set_provider_key_resolver(_refuse)
        try:
            response = client.post("/v1/chat/completions", headers=_auth_headers(), json=_chat_body())
        finally:
            reset_provider_key_resolver()
            self._teardown(patches, active)
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "provider_key_missing"
        assert response.json()["error"]["message"] == "not available with your own key"
        active["litellm"].assert_not_called()

    def _serve_with_failing(self, stage):
        """POST a chat request with G18 on and the response stage ``stage`` raising. Returns
        the response, the usage rows it scheduled as (status, billable, cost), the requests
        sent for security audit, and the patches (for the provider mock)."""
        config = _test_config()
        config["groups"]["G18_observability"] = {
            "enabled": True, "prometheus_enabled": False, "turn_efficiency_enabled": False,
            "tool_governance_enabled": False}
        _, patches, active = self._client_and_patches(config)
        import main
        client = TestClient(main.app, raise_server_exceptions=False)   # an error is a 500 here
        rows, audited = [], []

        def _billing(ctx, response, *, status_code=200, billable=True, **_):
            rows.append((status_code, billable, ctx.savings.cost_actual_usd))

        try:
            with patch(stage, new_callable=AsyncMock, side_effect=RuntimeError("stage bug")), \
                 patch("main._schedule_billing", side_effect=_billing), \
                 patch("main._schedule_security_audit", side_effect=audited.append):
                response = client.post("/v1/chat/completions", headers=_auth_headers(),
                                       json=_chat_body())
        finally:
            self._teardown(patches, active)
        return response, rows, audited, active

    def test_a_failed_safety_stage_withholds_the_answer_and_records_the_paid_call(self):
        from savings.calculator import estimate_cost
        response, rows, audited, active = self._serve_with_failing(
            "middleware.g30_guardrails.G30Guardrails.process_response")
        assert response.status_code == 500
        assert "Paris" not in response.text
        active["litellm"].assert_awaited_once()
        # Not billed to the tenant, but priced at what the provider billed.
        assert rows == [(500, False, pytest.approx(estimate_cost(20, 5, "gpt-4o-mini")))]
        assert rows[0][2] > 0
        assert len(audited) == 1

    def test_a_failed_optimisation_stage_still_serves_and_bills_the_answer(self):
        response, rows, audited, _ = self._serve_with_failing(
            "middleware.g19_headroom.G19Headroom.process_response")
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Paris"
        assert [(status, billable) for status, billable, _ in rows] == [(200, True)]
        assert rows[0][2] > 0 and len(audited) == 1

    def test_every_served_answer_goes_through_the_same_failure_handling(self):
        # The G06 cascade and F2 agent answers are served as the main call's is.
        import inspect
        import main
        assert inspect.getsource(main).count("_pipeline.process_response(") == 1

    def test_an_admin_key_acting_as_a_tenant_is_recorded_but_not_billed_to_it(self):
        admin_key = "test-admin-key-integration"
        keys = json.dumps({_PROXY_KEY_HASH: "test-user",
                           hashlib.sha256(admin_key.encode()).hexdigest():
                               {"tenant_id": "ops", "tier": "enterprise", "admin": True}})
        client, patches, active = self._client_and_patches()
        rows, bumped = [], []

        def _billing(ctx, response, *, status_code=200, billable=True, **_):
            rows.append((ctx.tenant_id, getattr(ctx, "impersonator_tenant_id", None),
                         status_code, billable))

        try:
            with patch("auth.api_key_manager._fetch_secret", return_value=keys), \
                 patch("main._schedule_billing", side_effect=_billing), \
                 patch("main._bump_quota_counter", side_effect=bumped.append), \
                 patch("main._bump_spend_counter", side_effect=bumped.append), \
                 patch("main._bump_trial_counter", side_effect=bumped.append):
                response = client.post("/v1/chat/completions", json=_chat_body(),
                                       headers={"Authorization": f"Bearer {admin_key}",
                                                "X-Tenant-ID": "ACME-PRD-01"})
        finally:
            self._teardown(patches, active)
        assert response.status_code == 200, response.text
        assert rows == [("ACME-PRD-01", "ops", 200, False)]
        assert bumped == []

    @pytest.mark.parametrize("impersonated", [False, True], ids=["the tenant's own", "impersonated"])
    def test_a_batched_request_counts_against_the_tenant_s_limits(self, monkeypatch, impersonated):
        """Queued and answered 202: billed, and counted against the quota and trial, then. It
        is queued with the spend counter its cost goes to when the answer arrives. An admin
        key acting as the tenant does none of this to the tenant."""
        from middleware import g13_batch
        tenant_key, admin_key = "test-tenant-key-integration", "test-admin-key-integration"
        keys = json.dumps({
            hashlib.sha256(tenant_key.encode()).hexdigest(): {"tenant_id": "acme", "tier": "free"},
            hashlib.sha256(admin_key.encode()).hexdigest():
                {"tenant_id": "ops", "tier": "enterprise", "admin": True}})
        config = _test_config()
        config["groups"]["G13_batch"] = {"enabled": True, "batch_topics": ["bulk"]}
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", {"bulk"})
        stream = AsyncMock()
        stream.xlen = AsyncMock(return_value=0)
        client, patches, active = self._client_and_patches(config)
        rows, bumped = [], []

        def _billing(ctx, response, *, status_code=200, billable=True, **_):
            rows.append((ctx.tenant_id, status_code, billable))

        headers = ({"Authorization": f"Bearer {admin_key}", "X-Tenant-ID": "acme"} if impersonated
                   else {"Authorization": f"Bearer {tenant_key}"})
        try:
            with patch("auth.api_key_manager._fetch_secret", return_value=keys), \
                 patch("middleware.g13_batch._get_redis", return_value=stream), \
                 patch("main._schedule_billing", side_effect=_billing), \
                 patch("main._bump_quota_counter", side_effect=lambda ctx: bumped.append("quota")), \
                 patch("main._bump_spend_counter", side_effect=lambda ctx: bumped.append("spend")), \
                 patch("main._bump_trial_counter", side_effect=lambda ctx: bumped.append("trial")):
                response = client.post("/v1/chat/completions", headers=headers,
                                       json={**_chat_body(), "batch_topic": "bulk"})
        finally:
            self._teardown(patches, active)
        assert response.status_code == 202, response.text
        active["litellm"].assert_not_called()
        queued = json.loads(stream.xadd.await_args.args[1]["payload"])
        if impersonated:
            assert (rows, bumped, queued["spend_prefix"]) == ([("acme", 202, False)], [], "")
        else:
            assert (rows, bumped, queued["spend_prefix"]) == (
                [("acme", 202, True)], ["quota", "trial"], "t:acme:")

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
