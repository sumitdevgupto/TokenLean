"""Unit tests for auth/api_key_manager.py."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


_KEY_ALICE = "proxy-key-abc123"
_KEY_BOB = "proxy-key-xyz789"

# Secret stores SHA256(key) → user_id
_VALID_KEYS_JSON = json.dumps({
    hashlib.sha256(_KEY_ALICE.encode()).hexdigest(): "alice",
    hashlib.sha256(_KEY_BOB.encode()).hexdigest(): "bob",
})


def _mock_secret_client(secret_data: str):
    """Build a mock Google SecretManagerServiceClient that returns secret_data."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.payload.data = secret_data.encode()
    mock_client.access_secret_version.return_value = mock_response
    return mock_client


class TestValidateProxyKey:
    def _reset_cache(self):
        import auth.api_key_manager as mgr
        mgr._KEY_CACHE = {}
        mgr._CACHE_LOADED_AT = 0.0

    def test_valid_key_returns_is_valid_and_user_id(self):
        """Legacy key format ({hash: user_id}) returns (True, user_id, None) —
        the third element is reserved for new-format tenant metadata."""
        import auth.api_key_manager as mgr
        self._reset_cache()
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON):
            is_valid, user_id, metadata = mgr.validate_proxy_key(_KEY_ALICE)
        assert is_valid is True
        assert user_id == "alice"
        assert metadata is None

    def test_invalid_key_returns_false(self):
        import auth.api_key_manager as mgr
        self._reset_cache()
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON):
            is_valid, user_id, metadata = mgr.validate_proxy_key("bad-key-000")
        assert is_valid is False
        assert user_id is None
        assert metadata is None

    def test_malformed_json_returns_false(self):
        import auth.api_key_manager as mgr
        self._reset_cache()
        with patch("auth.api_key_manager._fetch_secret", return_value="not-valid-json{{{}"):
            is_valid, user_id, metadata = mgr.validate_proxy_key(_KEY_ALICE)
        assert is_valid is False

    def test_new_format_key_returns_tenant_id_and_metadata(self):
        """New key format ({hash: {tenant_id, tier}}) returns (True, tenant_id, metadata_dict)."""
        import auth.api_key_manager as mgr
        self._reset_cache()
        new_format_keys = json.dumps({
            hashlib.sha256(_KEY_ALICE.encode()).hexdigest(): {"tenant_id": "acme", "tier": "enterprise"},
        })
        with patch("auth.api_key_manager._fetch_secret", return_value=new_format_keys):
            is_valid, tenant_id, metadata = mgr.validate_proxy_key(_KEY_ALICE)
        assert is_valid is True
        assert tenant_id == "acme"
        assert metadata == {"tenant_id": "acme", "tier": "enterprise"}


class TestIsSuspended:
    def test_suspended_true(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended({"tenant_id": "acme", "suspended": True}) is True

    def test_suspended_false(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended({"tenant_id": "acme", "suspended": False}) is False

    def test_suspended_absent_defaults_false(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended({"tenant_id": "acme", "tier": "enterprise"}) is False

    def test_legacy_string_key_never_suspended(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended(None) is False


class TestGetLLMProviderKey:
    def test_returns_env_var_override(self):
        with patch.dict(os.environ, {"LLM_KEY_OPENAI": "sk-local-test-key"}):
            from auth.api_key_manager import get_llm_provider_key
            result = get_llm_provider_key("openai")
        assert result == "sk-local-test-key"

    def test_falls_back_to_secret_manager(self):
        with patch("auth.api_key_manager._fetch_secret", return_value="sk-secret-key"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LLM_KEY_OPENAI", None)
                from auth.api_key_manager import get_llm_provider_key
                result = get_llm_provider_key("openai")
        assert result == "sk-secret-key"
