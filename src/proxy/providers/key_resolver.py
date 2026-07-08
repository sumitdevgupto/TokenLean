"""Pluggable per-tenant LLM provider key resolution — the BYOK seam (CORE, ships in OSS).

The core default resolver is the existing global env/Secret Manager lookup
(``auth.api_key_manager.get_llm_provider_key``), so OSS self-host behaviour is byte-identical
and unchanged. The commercial layer installs a tenant-aware resolver at startup via
``set_provider_key_resolver()`` (see ``api/tenant_keys.py`` + ``commercial_app.py``). Core
never imports commercial code — the seam is a plain function pointer.

Resolution contract:
  * returns a key string           → use it
  * returns ``None``               → "no key available" (today's meaning: callers keep their
                                     existing 503 / degrade-to-heuristic behaviour)
  * raises ``ProviderKeyError``    → strict-BYOK denial (only the commercial resolver raises);
                                     the main chat path maps it to HTTP 402 with an actionable
                                     message; middleware treats it like a missing key (degrade).
"""
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ProviderKeyError(Exception):
    """Strict-BYOK denial: the tenant has no key for this provider and is not exempt."""

    def __init__(self, provider: str, tenant_id: str, message: Optional[str] = None) -> None:
        self.provider = provider
        self.tenant_id = tenant_id
        self.public_message = message or (
            f"No {provider} API key is configured for your account. "
            f"Add one in the portal under Settings → Models & Keys."
        )
        super().__init__(self.public_message)


# fn(provider, tenant_id, ctx_or_None) -> Optional[str]; MAY raise ProviderKeyError
ProviderKeyResolverFn = Callable[[str, str, Optional[Any]], Awaitable[Optional[str]]]


async def _default_resolver(provider: str, tenant_id: str, ctx: Optional[Any]) -> Optional[str]:
    """OSS default: global platform key by provider name (env LLM_KEY_<P> / Secret Manager).

    Tenant-agnostic — identical to the pre-BYOK behaviour. Lazy import avoids any import
    cycle with ``auth.api_key_manager`` and keeps ``providers`` importable in isolation.
    """
    from auth.api_key_manager import get_llm_provider_key
    return get_llm_provider_key(provider)


_resolver: ProviderKeyResolverFn = _default_resolver


def set_provider_key_resolver(fn: ProviderKeyResolverFn) -> None:
    """Install a resolver (commercial layer, at startup). Idempotent; last one wins."""
    global _resolver
    _resolver = fn
    logger.info("provider key resolver installed: %s", getattr(fn, "__qualname__", repr(fn)))


def reset_provider_key_resolver() -> None:
    """Restore the OSS default resolver (used by tests)."""
    global _resolver
    _resolver = _default_resolver


async def resolve_provider_key(
    provider: str, tenant_id: str = "default", ctx: Optional[Any] = None
) -> Optional[str]:
    """Resolve the LLM key for (provider, tenant). See module docstring for the contract."""
    return await _resolver(provider, tenant_id, ctx)
