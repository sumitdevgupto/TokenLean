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

The same rule governs the TEAM (``X-Team``): honoured only for keys flagged
``gateway`` (``resolve_team``), and every identity G00/G18 act on is stamped onto the
request context once, by ``apply_caller_identity``.
"""
import hashlib
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


# ── Team identity (X-Team) ────────────────────────────────────────────────────
# Until 2026-09-18 G00 and G18 read the raw X-Team header, which any caller can set: a
# fresh value per request got a fresh rate-limit bucket (the limits never bound), claimed
# any team's `per_team` limit, and minted a new Prometheus series. X-Team is honoured only
# from a key flagged `gateway` — a customer's API gateway that stamps the team on every
# request, overwriting whatever the app sent. Defined once, here beside the X-Tenant-ID
# rule, so every consumer applies the same rule.
DEFAULT_TEAM = "default"
_MAX_TEAM_TAG_CHARS = 64


def resolve_team(header_team: Optional[str], key_is_gateway: bool) -> str:
    """The request's trusted team: a gateway key's ``X-Team`` value, else ``"default"``.

    Pass the HEADER (the gateway's stamp), never a JSON body field — a body passes
    through a gateway untouched, so a body ``x_team`` is still the app's choice.
    """
    if not key_is_gateway:
        return DEFAULT_TEAM
    team = (header_team or "").strip()
    return team or DEFAULT_TEAM


def team_tag(team: str) -> str:
    """Key-safe form of a team name for a Redis key segment.

    A gateway can send anything, so the value is never embedded verbatim: short, plainly
    safe names stay legible in ``redis-cli``; anything longer or odder becomes a 16-char
    digest (the shape ``g11_output_format._bucket_tag`` uses). ``:`` is outside the safe
    set, so a team can never add a segment to a key that the GDPR purge matches as
    ``tok_opt:rate_limit:*:<tenant>:*``.
    """
    text = str(team)
    if 0 < len(text) <= _MAX_TEAM_TAG_CHARS and all(
        (c.isascii() and c.isalnum()) or c in "-_." for c in text
    ):
        return text
    return "h-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def apply_caller_identity(ctx, headers: Dict[str, str]) -> None:
    """Stamp the authenticated caller's identity onto ``ctx`` — once, at pipeline entry.

    ``main._serve_core`` records the key's facts in ``ctx.params`` as ``_auth_*`` for EVERY
    key (overwriting any ``_auth_*`` field a client put in the JSON body). This turns them
    into the three fields G00 and G18 read:

    * ``ctx.is_gateway_key`` — the key's ``gateway`` flag;
    * ``ctx.key_principal`` — the key-bound identity (the tenant id for a dict-metadata key,
      the user for a legacy key), captured BEFORE any X-User-ID override: an allow-listed
      override is still the caller's choice, so it must never select a rate-limit bucket.
      ``ctx.user_id`` stays the attribution id;
    * ``ctx.team`` — :func:`resolve_team`.

    ``headers`` must be lower-cased.
    """
    params = getattr(ctx, "params", None) or {}
    ctx.is_gateway_key = bool(params.get("_auth_gateway", False))
    ctx.key_principal = str(params.get("_auth_principal") or params.get("_auth_tenant_id")
                            or getattr(ctx, "user_id", "") or "")
    ctx.team = resolve_team(headers.get("x-team"), ctx.is_gateway_key)


def register_tenant(api_key_hash: str, ctx: TenantContext) -> None:
    """Register an API key → TenantContext mapping (called by auth layer on key load)."""
    _KEY_TO_TENANT[api_key_hash] = ctx


def clear_registry() -> None:
    """Clear the in-process registry (used in tests)."""
    _KEY_TO_TENANT.clear()
