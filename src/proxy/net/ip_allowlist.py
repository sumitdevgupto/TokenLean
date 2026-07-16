"""Source-IP allowlist enforcement (OSS core).

App-level CIDR allowlisting for the proxy request path. Enforced in
``main.py`` ``_authenticate`` (the single choke-point every authenticated route
funnels through), NOT as a Starlette middleware — because the per-tenant CIDRs are
only known *after* the proxy key is validated, and a middleware would have to
re-validate the key to learn the tenant.

Two allowlist sources, union'd:
  * ``global_cidrs`` — from ``config.yaml`` ``ip_allowlist.global_cidrs``; applies to
    ALL tenants (e.g. an office / VPN egress that must always work).
  * ``tenant_cidrs`` — per-tenant, stamped into the key metadata by the commercial
    admin lifecycle (``api_key_manager.set_ip_allowlist``); ``companies.ip_allowlist``
    is the source-of-record.

Semantics (see ``ip_allowed``): a request is allowed iff its source IP falls in
``global_cidrs ∪ tenant_cidrs``. If BOTH lists are empty the tenant is
unrestricted (allow). A tenant with its own non-empty list is bound to
``its_own ∪ global``. IPv4 and IPv6 safe.

This module imports only the stdlib — no commercial module, keeping the open-core
barricade green (``verify-oss-gates.sh`` Gate 7).
"""

from __future__ import annotations

import ipaddress
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def client_ip_from_request(request, trust_xff: bool = True) -> str:
    """Best-effort client IP for a Starlette/FastAPI ``request``.

    When ``trust_xff`` is True the first ``X-Forwarded-For`` hop wins (Cloud Run and
    most proxies front the app and set this to the real client). Otherwise the direct
    socket peer is used. Mirrors ``portal_auth._client_ip`` (that helper lives in a
    commercial module core cannot import, so the logic is duplicated here on purpose).
    Returns ``"unknown"`` when no IP can be determined.
    """
    if trust_xff:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if getattr(request, "client", None) else "unknown"


def _parse_networks(cidrs: List[str]) -> List[ipaddress._BaseNetwork]:
    nets: List[ipaddress._BaseNetwork] = []
    for c in cidrs or []:
        try:
            nets.append(ipaddress.ip_network(str(c).strip(), strict=False))
        except ValueError:
            # A malformed CIDR should never silently widen access — skip it and log.
            logger.warning("ip_allowlist: ignoring invalid CIDR %r", c)
    return nets


def ip_allowed(ip: str, global_cidrs: Optional[List[str]],
               tenant_cidrs: Optional[List[str]]) -> bool:
    """Return True when ``ip`` is permitted by the union of the two CIDR lists.

    Both lists empty ⇒ allow (feature off / unrestricted tenant). Otherwise allow
    iff ``ip`` is contained in any network of ``global_cidrs ∪ tenant_cidrs``. An
    unparseable ``ip`` is denied when any restriction is in force (fail-closed).
    """
    global_cidrs = global_cidrs or []
    tenant_cidrs = tenant_cidrs or []
    if not global_cidrs and not tenant_cidrs:
        return True  # unrestricted
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        logger.warning("ip_allowlist: undeterminable/invalid client ip %r → denied", ip)
        return False
    for net in _parse_networks(list(global_cidrs) + list(tenant_cidrs)):
        # Only compare same-family (ip_address in ip_network raises across families).
        if addr.version == net.version and addr in net:
            return True
    return False
