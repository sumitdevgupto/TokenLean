"""Unit tests for the tenant-OWNED key resolver seam (fine-tune platform-key-leak fix).

The tenant-owned seam must NEVER return the platform key — that's what distinguishes it from
resolve_provider_key (which falls back to the platform key when strict BYOK is off). The
fine-tune trigger relies on this: a non-None result is safe to train a tenant's model with.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from providers import key_resolver


@pytest.fixture(autouse=True)
def _reset_resolver():
    key_resolver.reset_tenant_owned_key_resolver()
    yield
    key_resolver.reset_tenant_owned_key_resolver()


@pytest.mark.asyncio
async def test_oss_default_returns_none():
    """OSS/self-host has no per-tenant key store → no tenant ever owns a key here."""
    assert await key_resolver.resolve_tenant_owned_key("openai", "NOVA-STG-01") is None


@pytest.mark.asyncio
async def test_installed_resolver_is_used():
    async def _fake(provider, tenant_id):
        return f"key-for-{tenant_id}-{provider}"

    key_resolver.set_tenant_owned_key_resolver(_fake)
    assert await key_resolver.resolve_tenant_owned_key("openai", "NOVA-STG-01") == "key-for-NOVA-STG-01-openai"


@pytest.mark.asyncio
async def test_installed_resolver_none_means_no_tenant_key():
    async def _none(provider, tenant_id):
        return None

    key_resolver.set_tenant_owned_key_resolver(_none)
    assert await key_resolver.resolve_tenant_owned_key("openai", "NOVA-STG-01") is None


@pytest.mark.asyncio
async def test_seam_is_independent_of_chat_resolver():
    """Installing the chat resolver must NOT affect the tenant-owned seam and vice-versa."""
    async def _chat(provider, tenant_id, ctx=None):
        return "PLATFORM-KEY"  # what the chat resolver may return as fallback

    key_resolver.set_provider_key_resolver(_chat)
    try:
        # tenant-owned seam still the default (None) — it never sees the chat fallback.
        assert await key_resolver.resolve_tenant_owned_key("openai", "NOVA-STG-01") is None
        # and the chat seam is unaffected by the tenant-owned default.
        assert await key_resolver.resolve_provider_key("openai", "NOVA-STG-01") == "PLATFORM-KEY"
    finally:
        key_resolver.reset_provider_key_resolver()
