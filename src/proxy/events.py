"""Outbound event seam (OSS core).

Core signal sites (the G00 spend-cap gate, G30/G31 guardrail blocks, G29/G31 PII
detection) emit typed, **PII-free** events through :func:`emit_event`. By default this
is a **no-op** — an OSS / self-host deployment has no dispatcher installed, so
``schedule_event`` creates no tasks and the request path is byte-identical.

The commercial layer installs a webhook dispatcher via :func:`set_webhook_dispatcher`
(wired in ``commercial_app.py``) that delivers each event to the tenant's registered
HTTPS endpoints (HMAC-signed, retried, dead-lettered). This is barricade-safe: core
imports only THIS module, never the commercial dispatcher — the handoff is a plain
function pointer, exactly like ``providers/key_resolver.py`` and the ``_audit_logger``
seam.

**Payload contract:** payloads carry event metadata ONLY — tenant id, event type,
request id, timestamp, and PII-free counts / categories / entity TYPES. NEVER prompt
content, matched values, or secrets. (The dispatcher signs and delivers whatever it is
given, so the no-content rule is enforced here, at the emit sites.)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── Event type constants (stable strings — part of the public webhook contract) ──
SPEND_CAP_REACHED = "spend_cap.reached"      # monthly USD cap hit → request refused
BUDGET_THRESHOLD = "budget.threshold"         # spend crossed a warn %% of the cap
GUARDRAIL_BLOCK = "guardrail.block"           # G30/G31 injection block (content-filter)
PII_DETECTED = "pii.detected"                 # G29/G31 PII flagged / masked / blocked
TRIAL_THRESHOLD = "trial.threshold"           # free trial crossed a warn %% (days/requests)
TRIAL_EXPIRED = "trial.expired"               # free trial exhausted → requests now 402

# All event types a tenant may subscribe a webhook endpoint to.
ALL_EVENT_TYPES = (
    SPEND_CAP_REACHED,
    BUDGET_THRESHOLD,
    GUARDRAIL_BLOCK,
    PII_DETECTED,
    TRIAL_THRESHOLD,
    TRIAL_EXPIRED,
)


async def _default_dispatch(tenant_id: str, event: str, payload: Dict[str, Any]) -> None:
    """OSS no-op dispatcher — no endpoints, no delivery."""
    return None


_dispatcher: Callable[[str, str, Dict[str, Any]], Awaitable[None]] = _default_dispatch


def set_webhook_dispatcher(
    fn: Optional[Callable[[str, str, Dict[str, Any]], Awaitable[None]]]
) -> None:
    """Install the outbound event dispatcher (commercial). ``None`` restores the no-op."""
    global _dispatcher
    _dispatcher = fn or _default_dispatch


def dispatcher_installed() -> bool:
    """True once a real dispatcher is installed — lets ``schedule_event`` skip all work
    (and create zero asyncio tasks) on an OSS deploy."""
    return _dispatcher is not _default_dispatch


async def emit_event(tenant_id: str, event: str, payload: Dict[str, Any]) -> None:
    """Dispatch one event. Never raises — delivery is best-effort and must never break
    the request path."""
    try:
        await _dispatcher(tenant_id or "default", event, dict(payload or {}))
    except Exception as exc:  # pragma: no cover - defensive; delivery is best-effort
        logger.debug("event dispatch failed (%s): %s", event, exc)


def schedule_event(tenant_id: str, event: str, payload: Dict[str, Any]) -> None:
    """Fire-and-forget :func:`emit_event`. No-op when no dispatcher is installed (OSS) or
    when there is no running loop. Never blocks or raises on the caller's path."""
    if not dispatcher_installed():
        return
    try:
        asyncio.create_task(emit_event(tenant_id, event, payload))
    except RuntimeError:  # no running loop (not the async request path) — skip
        logger.debug("event %s skipped: no running loop", event)
