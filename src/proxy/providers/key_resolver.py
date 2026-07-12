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


class ProviderKeyDecryptError(ProviderKeyError):
    """A stored tenant key was FOUND but could not be decrypted (corrupt ciphertext or a
    master key that no longer matches). This is a **fail-closed** condition, deliberately
    distinct from "no key configured":

      * The commercial resolver raises it BEFORE any platform-key fallback, so a decrypt
        failure never silently burns the platform account.
      * It subclasses ``ProviderKeyError`` so every middleware ``except ProviderKeyError``
        degrade path (G06/G09/G10/G13) still treats it as "no usable key" and fails closed
        (no platform call) — the safe default.
      * The main chat path catches it FIRST (before ``ProviderKeyError``) to return a
        distinct message rather than the misleading "add a key" 402.
    """

    def __init__(self, provider: str, tenant_id: str) -> None:
        super().__init__(
            provider,
            tenant_id,
            message=(
                f"Your stored {provider} API key could not be decrypted and cannot be "
                f"used. Please re-enter it in the portal under Settings → Models & Keys, "
                f"or contact support if this persists."
            ),
        )


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


# ── Tenant-OWNED key seam (never the platform fallback) ───────────────────────
# Distinct from resolve_provider_key: that seam intentionally falls back to the platform
# key when BYOK isn't enforced (correct for the chat path). The fine-tune path must NOT
# train a tenant's model on the platform account, so it needs a resolver that returns a key
# ONLY when it is genuinely the tenant's own — never the platform key. The default (OSS, no
# per-tenant key store) returns None; the commercial layer installs a real one that reads the
# encrypted tenant_provider_keys store. A None return means "this tenant has no own key" and
# the caller decides (strict BYOK → refuse; non-strict → platform key is acceptable).


async def _default_tenant_owned_resolver(provider: str, tenant_id: str) -> Optional[str]:
    """OSS default: there is no per-tenant key store, so no tenant ever OWNS a key here."""
    return None


_tenant_owned_resolver: Callable[[str, str], Awaitable[Optional[str]]] = _default_tenant_owned_resolver


def set_tenant_owned_key_resolver(fn: Callable[[str, str], Awaitable[Optional[str]]]) -> None:
    """Install the tenant-owned-key resolver (commercial layer, at startup)."""
    global _tenant_owned_resolver
    _tenant_owned_resolver = fn
    logger.info("tenant-owned key resolver installed: %s", getattr(fn, "__qualname__", repr(fn)))


def reset_tenant_owned_key_resolver() -> None:
    """Restore the OSS default (used by tests)."""
    global _tenant_owned_resolver
    _tenant_owned_resolver = _default_tenant_owned_resolver


async def resolve_tenant_owned_key(provider: str, tenant_id: str) -> Optional[str]:
    """Return the tenant's OWN provider key, or None — NEVER the platform key.

    Used by the fine-tune trigger: a non-None result is safe to train a tenant's model with;
    a None result means the tenant has no key of its own (the caller enforces strict-BYOK
    policy). May raise ProviderKeyDecryptError (fail-closed) if a stored key is undecryptable.
    """
    return await _tenant_owned_resolver(provider, tenant_id)
