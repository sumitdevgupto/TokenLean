"""
Shared pytest fixtures for the TokenLean — Token Optimisation Framework test suite.
Adds src/proxy to sys.path so all proxy modules are importable without installation.
"""
import copy
import sys
import os
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Path setup ──────────────────────────────────────────────────────────────
_PROXY_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "proxy")
if _PROXY_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_PROXY_DIR))

from savings.models import SavingsRecord
from savings.calculator import count_messages_tokens
from middleware import RequestContext


# ─── Minimal config fixture ───────────────────────────────────────────────────

def _minimal_config() -> Dict[str, Any]:
    """All G-groups enabled with test-friendly settings."""
    return {
        "proxy": {"port": 4000, "log_level": "INFO"},
        "providers": [{"name": "openai", "models": ["gpt-4o", "gpt-4o-mini"]}],
        "rate_limit": {
            "enabled": False,  # Disabled for integration tests to avoid Redis dependency
            "default": {
                "requests_per_minute": 60,
                "requests_per_hour": 1000,
            },
            "per_user": {},
            "per_team": {},
        },
        "groups": {
            "G1_compression": {
                "enabled": True,
                "min_tokens_to_compress": 10,
                "compression_ratio_target": 0.5,
                "sidecar_url": "http://mock-llmlingua",
            },
            "G2_template_registry": {
                "enabled": True,
                "budgets": {
                    "test-template": {
                        "system_prompt_max": 50,
                        "total_input_max": 100,
                        "output_max": 50,
                    }
                },
            },
            "G4_bypass": {
                "enabled": True,
                "database_first": False,  # Use config rules for tests
                "rules": [
                    {
                        "name": "greetings",
                        "keywords": ["hello", "hi"],
                        "static_response": "Hello! How can I help?",
                        "confidence_threshold": 0.30,  # Lower threshold for tests
                    }
                ],
            },
            "G5_cache": {
                "enabled": True,
                "l1_ttl_seconds": 3600,
                "l2_similarity_threshold": 0.90,
                "l2_ttl_seconds": 86400,
                "l2_embedding_model": "BAAI/bge-small-en-v1.5",
            },
            "G6_routing": {
                "enabled": True,
                "tiers": {
                    "simple": ["gpt-4o-mini"],
                    "medium": ["gpt-4o"],
                    "complex": ["gpt-4o"],
                },
            },
            "G7_retrieval": {
                "enabled": True,
                "top_k": 3,
                "top_k_after_rerank": 1,
                "similarity_threshold": 0.80,
            },
            "G8_tools": {
                "enabled": True,
                "max_tools_per_agent": 5,
            },
            "G9_context_schema": {
                "enabled": True,
                "use_instructor": False,
                "instructor_model": "gpt-4o-mini",
                "instructor_timeout_ms": 3000,
                "instructor_fallback_to_heuristic": True,
                "prose_min_length_chars": 80,
                "prose_indicators": [
                    "customer", "user", "he", "she", "they", "called",
                    "about", "regarding", "mentioned", "requested",
                    "explained", "told", "said", "asked",
                ],
                "schema_fields": {},
            },
            "G10_memory": {
                "enabled": True,
                "sliding_window_turns": 2,
                "summary_model": "gpt-4o-mini",
            },
            "G11_output": {
                "enabled": True,
                "enforce_max_tokens": True,
                "default_max_tokens_multiplier": 2.0,
                "force_json_for_all": False,
                "provider_structured_output": True,
                "max_tokens_feedback_loop": True,
                "max_tokens_auto_tighten": True,
                "tighten_quantile": 0.95,
                "tighten_multiplier": 1.2,
                "max_tokens_history_ttl_days": 7,
            },
            "G12_reasoning": {
                "enabled": True,
                "default_effort": "medium",
                "effort_map": {
                    "low": {"openai": "low", "anthropic_tokens": 2000, "gemini_mode": "on-demand"},
                    "medium": {"openai": "medium", "anthropic_tokens": 5000, "gemini_mode": "auto"},
                    "high": {"openai": "high", "anthropic_tokens": 20000, "gemini_mode": "on"},
                },
                "provider_params": [
                    {
                        "model_fragment": "o1",
                        "param_key": "reasoning_effort",
                        "is_effort_key": True,
                    },
                    {
                        "model_fragment": "o3",
                        "param_key": "reasoning_effort",
                        "is_effort_key": True,
                    },
                    {
                        "model_fragment": "claude",
                        "param_key": "thinking",
                        "budget_key": "budget_tokens",
                    },
                    {
                        "model_fragment": "gemini",
                        "param_key": "generation_config",
                        "mode_key": "thinking",
                    },
                ],
                "reasoning_suppression_prompts": {
                    "low": "[BUDGET] Provide the final answer only.",
                    "medium": "[BUDGET] Keep reasoning minimal.",
                    "high": None,
                },
            },
            "G13_batch": {"enabled": True, "max_batch_size": 50, "flush_interval_ms": 500},
            "G14_tool_output": {"enabled": True, "field_whitelist": {}},
            "G15_server_compute": {"enabled": True, "hooks": []},
            "G16_agent_arch": {
                "enabled": True,
                "max_system_prompt_tokens": 50,
                "max_tools_per_agent": 3,
            },
            "G17_loop": {
                "enabled": True,
                "max_iterations": 5,
                "starting_budget_tokens": 1000,
                "compact_output_below_tokens": 100,
            },
            "G18_observability": {
                "enabled": True,
                "langfuse_enabled": True,
                "langfuse_host": "http://mock-langfuse",
                "et_weights": {"input": 1.0, "cache_read": 0.1, "output": 4.0},
            },
            "G19_headroom": {
                "enabled": True,
                "request_side_enabled": True,
                "response_side_enabled": True,
                "min_length_to_compress": 50,
                "compression_strategies": {
                    "json": {"remove_empty": True, "dedupe_keys": True},
                    "code": {"strip_comments": True, "strip_whitespace": True, "compress_imports": True},
                    "logs": {"dedupe_lines": True, "truncate_long_lines": 200},
                },
            },
            "G20_prompt_optimization": {
                "enabled": True,
                "optimizer": "builtin",
                "model": "gpt-4o-mini",
                "quality_threshold": 0.95,
                "max_prompt_tokens": 4000,
                "schedule": "weekly",
            },
            "G21_cache_alignment": {
                "enabled": True,
                "providers": {
                    "openai": {"auto": True},
                    "anthropic": {"marker": True, "cache_type": "ephemeral"},
                },
            },
        },
    }


@pytest.fixture
def minimal_config() -> Dict[str, Any]:
    """All G-groups enabled with test-friendly settings."""
    return _minimal_config()


def _integration_config() -> Dict[str, Any]:
    """Config for integration tests - disables G4/G5 to ensure full pipeline runs."""
    cfg = _minimal_config()
    cfg["rate_limit"]["enabled"] = False
    cfg["groups"]["G4_bypass"]["enabled"] = False
    cfg["groups"]["G4_bypass"]["rules"] = []
    cfg["groups"]["G5_cache"]["enabled"] = False
    return cfg


# ─── RequestContext factory ───────────────────────────────────────────────────

def _make_savings(messages, model, request_id="req-test", user_id="user-test"):
    baseline = count_messages_tokens(messages, model)
    return SavingsRecord(
        request_id=request_id,
        user_id=user_id,
        timestamp=datetime.now(timezone.utc),
        model_requested=model,
        routed_model=model,
        baseline_tokens=baseline,
    )


@pytest.fixture
def make_ctx(minimal_config):
    """Factory fixture: make_ctx(messages, model='gpt-4o', params={}) → RequestContext."""
    def _factory(
        messages: List[Dict[str, Any]] = None,
        model: str = "gpt-4o",
        params: Dict[str, Any] = None,
        config: Dict[str, Any] = None,
    ) -> RequestContext:
        if messages is None:
            messages = [{"role": "user", "content": "What is the capital of France?"}]
        if params is None:
            params = {}
        cfg = config if config is not None else minimal_config
        savings = _make_savings(messages, model)
        return RequestContext(
            request_id="req-test-001",
            user_id="test_user",
            original_messages=copy.deepcopy(messages),
            messages=copy.deepcopy(messages),
            model=model,
            routed_model=model,
            params=dict(params),
            config=cfg,
            savings=savings,
        )
    return _factory


@pytest.fixture
def simple_messages():
    return [{"role": "user", "content": "What is the capital of France?"}]


@pytest.fixture
def long_messages():
    """Messages long enough to trigger compression / sliding window."""
    system = "You are a helpful assistant. " * 20
    history = [{"role": "system", "content": system}]
    for i in range(5):
        history.append({"role": "user", "content": f"Question {i}"})
        history.append({"role": "assistant", "content": f"Answer {i} with some detail."})
    history.append({"role": "user", "content": "Now summarise everything."})
    return history


@pytest.fixture
def mock_litellm_response():
    """Synthetic litellm.acompletion response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Paris"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
        },
    }


# ─── Async test client for integration tests ──────────────────────────────────

import hashlib
import json as _json
from httpx import AsyncClient, ASGITransport

_TEST_KEY = "test-key"
_ADMIN_KEY = "admin-key"
_INTEGRATION_KEY = "test-proxy-key-integration"

_VALID_KEYS_JSON = _json.dumps({
    hashlib.sha256(_TEST_KEY.encode()).hexdigest(): "test-user",
    # New-format admin key: is_admin_key() requires a dict with admin=True, so the
    # /admin/* endpoints (guarded by _require_admin → 403) accept this key.
    hashlib.sha256(_ADMIN_KEY.encode()).hexdigest(): {"tenant_id": "admin-user", "tier": "enterprise", "admin": True},
    hashlib.sha256(_INTEGRATION_KEY.encode()).hexdigest(): "integration-user",
})

_LLM_RESPONSE_MOCK = MagicMock()
_LLM_RESPONSE_MOCK.model_dump.return_value = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
}


def _make_mock_redis():
    """Create a mock Redis that returns actual values instead of AsyncMock objects."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock(return_value=True)
    mock.setex = AsyncMock(return_value=True)
    mock.expire = AsyncMock(return_value=True)
    mock.incr = AsyncMock(return_value=1)
    mock.zrangebyscore = AsyncMock(return_value=[])
    mock.zrange = AsyncMock(return_value=[])
    mock.zadd = AsyncMock(return_value=1)
    mock.keys = AsyncMock(return_value=[])
    mock.hgetall = AsyncMock(return_value={})
    mock.hget = AsyncMock(return_value=None)
    mock.hset = AsyncMock(return_value=True)
    return mock


def _setup_auth_keys():
    """Setup valid API keys for testing."""
    import json as _json
    import hashlib
    return {
        hashlib.sha256("test-key".encode()).hexdigest(): "test-user",
        # admin scope required by is_admin_key() for the /admin/* endpoints
        hashlib.sha256("admin-key".encode()).hexdigest(): {"tenant_id": "admin-user", "tier": "enterprise", "admin": True},
        hashlib.sha256("test-proxy-key-integration".encode()).hexdigest(): "integration-user",
    }


@pytest.fixture
async def client():
    """Async test client with all external dependencies mocked."""
    with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON), \
         patch("auth.api_key_manager._KEY_CACHE", _setup_auth_keys()), \
         patch("auth.api_key_manager._CACHE_LOADED_AT", 9999999999.0), \
         patch("config_loader.load_config"), \
         patch("config_loader.start_hot_reload"), \
         patch("config_loader.get_config", return_value=_integration_config()), \
         patch("litellm.acompletion", new_callable=AsyncMock, return_value=_LLM_RESPONSE_MOCK), \
         patch("main.get_llm_provider_key", return_value="sk-test-key"), \
         patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock), \
         patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock), \
         patch("cache.redis_pool.init_pool"), \
         patch("cache.redis_pool.close_pool", new_callable=AsyncMock), \
         patch("cache.redis_pool.get_redis", return_value=_make_mock_redis()), \
         patch("middleware.g00_rate_limit._get_redis", return_value=_make_mock_redis()), \
         patch("middleware.g17_loop_control._get_redis", return_value=_make_mock_redis()):
        import main
        import importlib
        importlib.reload(main)
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
