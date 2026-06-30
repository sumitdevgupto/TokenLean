"""Regression tests for G11 max_tokens capping — Phase 5.

Verifies that max_tokens never exceeds model limits, even for large prompts.
This prevents 502 Bad Gateway errors from providers rejecting requests with
max_tokens above their model limits.
"""
import pytest
from unittest.mock import AsyncMock, patch

from middleware import RequestContext
from middleware.g11_output_format import (
    G11OutputFormat,
    _get_model_max_tokens,
    _DEFAULT_MODEL_MAX_TOKENS,
)

# Canonical model limits — mirrors what config.yaml.template ships.
# Tests use this dict instead of hardcoded fallback (T37: limits live in config, not code).
_MODEL_LIMITS_CONFIG = {
    "gpt-4o": 16384,
    "gpt-4o-mini": 16384,
    "gpt-4-turbo": 4096,
    "gpt-4": 8192,
    "gpt-3.5-turbo": 4096,
    "claude-3-opus": 4096,
    "claude-3-sonnet": 4096,
    "claude-3-haiku": 4096,
    "claude-3.5-sonnet": 8192,
    "claude-3.5-haiku": 8192,
    "claude-4-sonnet": 16384,
    "gemini-2.5-flash": 8192,
    "gemini-2.5-pro": 8192,
}


@pytest.fixture
def g11_config():
    """Standard G11 config for testing — includes model_max_tokens (T37: no hardcoded fallback)."""
    return {
        "groups": {
            "G11_output": {
                "enabled": True,
                "enforce_max_tokens": True,
                "default_max_tokens_multiplier": 2.0,
                "max_tokens_auto_tighten": False,
                "provider_structured_output": False,
                "model_max_tokens": dict(_MODEL_LIMITS_CONFIG),
            }
        }
    }


@pytest.fixture
def large_prompt_context(g11_config):
    """Context with a very large prompt (simulating 16k+ tokens)."""
    # Create a large message that would produce ~20,000 tokens baseline
    large_content = "This is a test sentence with multiple words. " * 4000
    ctx = RequestContext.create(
        request_id="regression-16k",
        user_id="test-user",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": large_content},
        ],
        model="gpt-4o-mini",
        params={},
        config=g11_config,
    )
    return ctx


class TestModelMaxTokensFunction:
    """Test _get_model_max_tokens — all limits come from config (T37)."""

    def test_exact_model_match_from_config(self):
        cfg = {"model_max_tokens": _MODEL_LIMITS_CONFIG}
        assert _get_model_max_tokens("gpt-4o-mini", cfg) == 16384

    def test_prefix_match_from_config(self):
        cfg = {"model_max_tokens": {"gpt-4o-mini": 16384}}
        assert _get_model_max_tokens("gpt-4o-mini-2024-07-18", cfg) == 16384

    def test_unknown_model_returns_default(self):
        assert _get_model_max_tokens("unknown-model-xyz") == _DEFAULT_MODEL_MAX_TOKENS

    def test_none_model_returns_default(self):
        assert _get_model_max_tokens(None) == _DEFAULT_MODEL_MAX_TOKENS

    def test_config_override_takes_priority(self):
        cfg = {"model_max_tokens": {"gpt-4o-mini": 8192}}
        assert _get_model_max_tokens("gpt-4o-mini", cfg) == 8192

    def test_config_prefix_override(self):
        cfg = {"model_max_tokens": {"gpt-4o": 32768}}
        assert _get_model_max_tokens("gpt-4o-mini-2024", cfg) == 32768

    def test_config_default_model_max(self):
        cfg = {"default_model_max_tokens": 2048}
        assert _get_model_max_tokens("totally-unknown-model", cfg) == 2048

    def test_no_hardcoded_fallback(self):
        """Without config, unknown models return the module default, not a hardcoded value."""
        assert _get_model_max_tokens("gpt-4o-mini") == _DEFAULT_MODEL_MAX_TOKENS

    def test_config_models_complete(self):
        """All common models return > 0 when config is provided."""
        cfg = {"model_max_tokens": _MODEL_LIMITS_CONFIG}
        for model in _MODEL_LIMITS_CONFIG:
            assert _get_model_max_tokens(model, cfg) > 0, f"No limit for {model}"


class TestG11MaxTokensCapping:
    """Regression: max_tokens must never exceed model limit."""

    @pytest.mark.asyncio
    async def test_large_prompt_capped_at_model_limit(self, large_prompt_context):
        """16k+ token prompt should have max_tokens capped at 16384 for gpt-4o-mini."""
        g11 = G11OutputFormat()
        ctx = await g11.process_request(large_prompt_context)
        assert ctx.params["max_tokens"] <= 16384
        assert ctx.params["max_tokens"] > 0

    @pytest.mark.asyncio
    async def test_small_prompt_uncapped(self, g11_config):
        """Small prompt should have max_tokens = 2x * 30% of input (well below limit)."""
        ctx = RequestContext.create(
            request_id="small-prompt",
            user_id="test-user",
            messages=[
                {"role": "user", "content": "What is 2+2?"},
            ],
            model="gpt-4o-mini",
            params={},
            config=g11_config,
        )
        g11 = G11OutputFormat()
        ctx = await g11.process_request(ctx)
        # Should be 2 * 30% of small input, much less than 16384
        assert ctx.params["max_tokens"] < 16384
        assert ctx.params["max_tokens"] >= 64  # minimum floor

    @pytest.mark.asyncio
    async def test_model_specific_cap_claude(self, g11_config):
        """Claude models respect their specific limits."""
        ctx = RequestContext.create(
            request_id="claude-test",
            user_id="test-user",
            messages=[
                {"role": "user", "content": "Explain quantum physics. " * 2000},
            ],
            model="claude-3-haiku",
            params={},
            config=g11_config,
        )
        g11 = G11OutputFormat()
        ctx = await g11.process_request(ctx)
        assert ctx.params["max_tokens"] <= 4096

    @pytest.mark.asyncio
    async def test_config_override_respected(self, g11_config):
        """Config-defined model limits take precedence over hardcoded."""
        g11_config["groups"]["G11_output"]["model_max_tokens"] = {
            "gpt-4o-mini": 8192  # Override from 16384 to 8192
        }
        ctx = RequestContext.create(
            request_id="config-override",
            user_id="test-user",
            messages=[
                {"role": "user", "content": "Long prompt. " * 3000},
            ],
            model="gpt-4o-mini",
            params={},
            config=g11_config,
        )
        g11 = G11OutputFormat()
        ctx = await g11.process_request(ctx)
        assert ctx.params["max_tokens"] <= 8192

    @pytest.mark.asyncio
    async def test_auto_tighten_respects_model_cap(self, g11_config):
        """Auto-tighten from p95 history must still respect model limits."""
        g11_config["groups"]["G11_output"]["max_tokens_auto_tighten"] = True

        ctx = RequestContext.create(
            request_id="tighten-cap",
            user_id="test-user",
            messages=[
                {"role": "user", "content": "Hello " * 500},
            ],
            model="gpt-3.5-turbo",
            params={"workflow_id": "test-wf", "template_id": "test-tmpl"},
            config=g11_config,
        )

        # Mock Redis returning a historical p95 of 50000 (way over model limit)
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=[
            b'{"max_tokens": 50000, "completion_tokens": 45000}',
            b'{"max_tokens": 48000, "completion_tokens": 44000}',
            b'{"max_tokens": 52000, "completion_tokens": 46000}',
            b'{"max_tokens": 49000, "completion_tokens": 43000}',
            b'{"max_tokens": 51000, "completion_tokens": 47000}',
        ])

        with patch("middleware.g11_output_format._get_redis", return_value=mock_redis):
            g11 = G11OutputFormat()
            ctx = await g11.process_request(ctx)

        # Even with p95 = 50000, max_tokens must be capped at 4096 for gpt-3.5-turbo
        assert ctx.params["max_tokens"] <= 4096

    @pytest.mark.asyncio
    async def test_explicit_max_tokens_not_overwritten(self, g11_config):
        """When developer sets max_tokens explicitly, G11 does not overwrite it."""
        ctx = RequestContext.create(
            request_id="explicit",
            user_id="test-user",
            messages=[
                {"role": "user", "content": "Hello"},
            ],
            model="gpt-4o-mini",
            params={"max_tokens": 500},
            config=g11_config,
        )
        g11 = G11OutputFormat()
        ctx = await g11.process_request(ctx)
        assert ctx.params["max_tokens"] == 500
