"""
TenantContext resolver — extracts tenant identity from an incoming HTTP request.

The **authenticated proxy key is the single source of truth** for tenant identity
(it carries ``tenant_id``/``tier``/``admin`` in Secret Manager / local-keys.json).
The client-supplied ``X-Tenant-ID`` header is honoured **only for keys flagged
``admin``** — it lets an operator/benchmark key act on behalf of another tenant.
A non-admin key can never assume another tenant by sending a header.

Resolution order:
  1. Admin impersonation   (admin key + X-Tenant-ID → that tenant)
  2. Key-authoritative      (key's own tenant_id/tier; header ignored if present)
  3. Legacy registry        (back-compat: api_key_hash → TenantContext, tests only)
  4. Fallback               (tenant_id="default", no namespace, free tier)

The resolver is synchronous so it can be called from both async FastAPI request
handlers and sync test helpers.
"""
import logging
from typing import Dict, Optional

from tenancy.context import TenantContext

logger = logging.getLogger(__name__)

# Legacy in-process cache of api_key_hash → TenantContext. No longer the source
# of truth (the key's metadata is passed per-request); retained only so existing
# callers/tests that inject a registry keep working.
_KEY_TO_TENANT: Dict[str, TenantContext] = {}

_VALID_TIERS = {"free", "enterprise"}


def _normalise_tier(tier: str) -> str:
    """Return a valid pricing tier, defaulting unknown/blank values to ``free``.

    The tier is bound to the API key at issuance (``issue-key.sh``) and arrives via
    the key's metadata. A typo or legacy value must not silently bill at an arbitrary
    tier, so normalise to the known set (case/whitespace-insensitive) and fall back to
    ``free`` (the $0 self-host floor) with a warning that surfaces in logs.
    """
    t = (tier or "").strip().lower()
    if t in _VALID_TIERS:
        return t
    logger.warning("resolve_tenant: unknown pricing tier %r — defaulting to 'free'", tier)
    return "free"


def resolve_tenant(
    headers: Dict[str, str],
    key_tenant_id: Optional[str] = None,
    key_tier: str = "free",
    key_is_admin: bool = False,
    api_key_hash: Optional[str] = None,
    tenant_registry: Optional[Dict[str, TenantContext]] = None,
) -> TenantContext:
    """Resolve the tenant for a request.

    Args:
        headers:        HTTP request headers (lower-cased keys expected).
        key_tenant_id:  Tenant bound to the authenticated proxy key (authoritative).
        key_tier:       Pricing tier bound to that key.
        key_is_admin:   True if the key carries the admin/impersonation scope.
        api_key_hash:   Legacy: SHA-256 of the key, for the registry fallback.
        tenant_registry: Legacy: explicit key→tenant map (tests).

    Returns:
        A fully-populated ``TenantContext``.
    """
    key_tier = _normalise_tier(key_tier)
    registry = tenant_registry if tenant_registry is not None else _KEY_TO_TENANT
    header_tenant = headers.get("x-tenant-id", "").strip()

    # 1. Admin impersonation — only an admin-scoped key may assume another tenant.
    if header_tenant and key_is_admin:
        return TenantContext.for_tenant(header_tenant, pricing_tier=key_tier)

    # 2. Key-authoritative — the key's own tenant wins; a header is ignored.
    if key_tenant_id:
        if header_tenant and header_tenant != key_tenant_id:
            logger.warning(
                "resolve_tenant: ignoring X-Tenant-ID=%r from non-admin key bound to "
                "tenant %r (cross-tenant header denied)",
                header_tenant, key_tenant_id,
            )
        return TenantContext.for_tenant(key_tenant_id, pricing_tier=key_tier)

    # 3. Legacy registry fallback (no per-request key metadata supplied).
    if api_key_hash and api_key_hash in registry:
        return registry[api_key_hash]

    # 4. Fallback — single-tenant / anonymous. A header alone is NOT trusted.
    if header_tenant:
        logger.warning(
            "resolve_tenant: ignoring X-Tenant-ID=%r with no authenticated tenant "
            "binding (use an admin key to impersonate)",
            header_tenant,
        )
    return TenantContext.default()


def register_tenant(api_key_hash: str, ctx: TenantContext) -> None:
    """Register an API key → TenantContext mapping (called by auth layer on key load)."""
    _KEY_TO_TENANT[api_key_hash] = ctx


def clear_registry() -> None:
    """Clear the in-process registry (used in tests)."""
    _KEY_TO_TENANT.clear()
