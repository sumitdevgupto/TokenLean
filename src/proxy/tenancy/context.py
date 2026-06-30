"""
TenantContext — resolved tenant identity for a single proxy request.

Carries all per-tenant identifiers needed by the middleware pipeline so that
cache keys, vector store collections, pricing tier gates, and config overrides
are all scoped to the originating tenant without any middleware needing to
re-derive the tenant identity.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict

# I5: tenant_id flows into Redis key prefixes, Qdrant collection names, and
# (for admin impersonation) comes from a client header — so it must be strictly
# sanitised. Anything outside this charset is replaced with "_", the id is capped
# at 64 chars, and it is forced to start alphanumeric. Empty → "default".
_TENANT_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
_MAX_TENANT_ID_LEN = 64


def sanitise_tenant_id(tenant_id: str) -> str:
    """Normalise an arbitrary tenant_id into a safe key/collection fragment."""
    tid = (tenant_id or "").strip()
    tid = _TENANT_ID_DISALLOWED.sub("_", tid)[:_MAX_TENANT_ID_LEN]
    if not tid:
        return "default"
    if not tid[0].isalnum():
        tid = ("t" + tid)[:_MAX_TENANT_ID_LEN]
    return tid


@dataclass(frozen=True)
class TenantContext:
    """Immutable per-request tenant identity resolved before G0 runs."""

    tenant_id: str
    # Pricing tier controls which G-groups are active (basic / pro / enterprise).
    pricing_tier: str = "basic"
    # Redis key namespace: all cache / session writes are prefixed with this
    # string.  Empty string disables namespacing (single-tenant / test mode).
    redis_prefix: str = ""
    # Qdrant collection name scoped to this tenant.
    qdrant_collection: str = "rag_docs"
    # Arbitrary per-tenant config overrides merged into ctx.config at pipeline
    # entry.  Allows tenants to tune G-group thresholds without a redeploy.
    config_overrides: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_tenant(cls, tenant_id: str, pricing_tier: str = "basic") -> "TenantContext":
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
