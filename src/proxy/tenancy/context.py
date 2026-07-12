"""
TenantContext — resolved tenant identity for a single proxy request.

Carries all per-tenant identifiers needed by the middleware pipeline so that
cache keys, vector store collections, pricing tier gates, and config overrides
are all scoped to the originating tenant without any middleware needing to
re-derive the tenant identity.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

# I5: tenant_id flows into Redis key prefixes, Qdrant collection names, and
# (for admin impersonation) comes from a client header — so it must be strictly
# sanitised. Anything outside this charset is replaced with "_", the id is capped
# at 64 chars, and it is forced to start alphanumeric. Empty → "default".
_TENANT_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
_MAX_TENANT_ID_LEN = 64

# Per-tenant GCS doc bucket naming. GCS bucket names are stricter than tenant ids:
# lowercase letters, digits and hyphens only (no underscores/uppercase), 3–63 chars,
# start/end alphanumeric. So the bucket name is a *lossy* lowercase transform of the
# sanitised tenant id — which means it is NOT reversible by inverse transform. Reverse
# only via registry lookup (build the forward map, look the bucket up in it).
_DEFAULT_BUCKET_PREFIX = os.getenv("DOC_BUCKET_PREFIX", "token-opt-docs-")
_BUCKET_NAME_DISALLOWED = re.compile(r"[^a-z0-9-]")
_MAX_BUCKET_NAME_LEN = 63
_MIN_BUCKET_NAME_LEN = 3


def sanitise_tenant_id(tenant_id: str) -> str:
    """Normalise an arbitrary tenant_id into a safe key/collection fragment."""
    tid = (tenant_id or "").strip()
    tid = _TENANT_ID_DISALLOWED.sub("_", tid)[:_MAX_TENANT_ID_LEN]
    if not tid:
        return "default"
    if not tid[0].isalnum():
        tid = ("t" + tid)[:_MAX_TENANT_ID_LEN]
    return tid


def tenant_to_bucket(tenant_id: str, prefix: str = _DEFAULT_BUCKET_PREFIX) -> str:
    """Derive this tenant's GCS doc-bucket name deterministically.

    Lossy lowercase transform of ``sanitise_tenant_id`` (GCS forbids uppercase and
    underscores). Reverse ONLY via ``bucket_to_tenant`` (registry lookup), never by
    inverting this function. Keep the transform in lock-step with the Terraform
    ``local`` that derives the same name (infra/main.tf) — both must agree.
    """
    safe = sanitise_tenant_id(tenant_id)  # reuse the canonical sanitiser
    slug = _BUCKET_NAME_DISALLOWED.sub("-", safe.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        slug = "default"
    name = f"{prefix}{slug}"
    if len(name) > _MAX_BUCKET_NAME_LEN:
        # Truncate the slug portion only — never the prefix — then re-strip.
        keep = _MAX_BUCKET_NAME_LEN - len(prefix)
        slug = slug[:max(1, keep)].strip("-") or "default"
        name = f"{prefix}{slug}"
    # A pathological short prefix could still underflow; pad defensively.
    if len(name) < _MIN_BUCKET_NAME_LEN:
        name = f"{name}-x"
    return name


def bucket_to_tenant(
    bucket: str,
    registry: Iterable[str],
    prefix: str = _DEFAULT_BUCKET_PREFIX,
) -> Optional[str]:
    """Reverse-map a bucket name to its raw registry tenant id, or None if unknown.

    Registry-authoritative: builds ``{tenant_to_bucket(t): t}`` over the known tenants
    and returns the raw ``t`` (not the sanitised slug) so callers can feed it straight
    to ``TenantContext.for_tenant``. Any bucket not produced by a registered tenant
    returns None — the caller MUST reject it (fail-safe, no cross-tenant write).
    """
    if not bucket:
        return None
    forward = {tenant_to_bucket(t, prefix): t for t in registry if t}
    return forward.get(bucket)


def validate_registry_unique(
    registry: Iterable[str],
    prefix: str = _DEFAULT_BUCKET_PREFIX,
) -> None:
    """Assert no two tenants collapse to the same bucket name (raise if they do).

    Real ``{CODE4}-{ENV}-{NN}`` ids cannot collide (CODE4 is a unique PK), but the
    lossy lowercase transform means e.g. ``NOVA_STG`` and ``NOVA-STG`` would. Call
    this at provisioning time to make the invariant explicit rather than assumed.
    """
    tenants = [t for t in registry if t]
    buckets = {}
    for t in tenants:
        b = tenant_to_bucket(t, prefix)
        if b in buckets and buckets[b] != t:
            raise ValueError(
                f"Bucket-name collision: tenants {buckets[b]!r} and {t!r} both map to {b!r}"
            )
        buckets[b] = t


@dataclass(frozen=True)
class TenantContext:
    """Immutable per-request tenant identity resolved before G0 runs."""

    tenant_id: str
    # Pricing tier — free (self-host / $0 floor) or enterprise (managed SaaS). Billing/
    # console only; optimisations are never gated by tier.
    pricing_tier: str = "free"
    # Redis key namespace: all cache / session writes are prefixed with this
    # string.  Empty string disables namespacing (single-tenant / test mode).
    redis_prefix: str = ""
    # Qdrant collection name scoped to this tenant.
    qdrant_collection: str = "rag_docs"
    # Arbitrary per-tenant config overrides merged into ctx.config at pipeline
    # entry.  Allows tenants to tune G-group thresholds without a redeploy.
    config_overrides: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_tenant(cls, tenant_id: str, pricing_tier: str = "free") -> "TenantContext":
        """Build a TenantContext with standard namespacing for the given tenant."""
        safe_id = sanitise_tenant_id(tenant_id)
        return cls(
            tenant_id=safe_id,
            pricing_tier=pricing_tier,
            redis_prefix=f"t:{safe_id}:" if safe_id != "default" else "",
            qdrant_collection=f"rag_{safe_id}" if safe_id != "default" else "rag_docs",
        )

    @classmethod
    def default(cls) -> "TenantContext":
        """Single-tenant / backward-compatible context with no namespace."""
        return cls(tenant_id="default", redis_prefix="", qdrant_collection="rag_docs")
