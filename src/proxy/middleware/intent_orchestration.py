"""Intent-Based Multi-Agent Orchestration (F2) — OSS-core engine.

Classifies an incoming request by semantic intent and, when it matches a **registered
downstream agent**, dispatches the request to that agent's OpenAI-compatible endpoint
INSTEAD of the normal LLM — so one proxy endpoint fans requests to the right agent
(billing / SRE / support …) with no routing code in the caller's app.

Open-core split (mirrors G29/G30): this ENGINE is OSS-core — it must live in the core
pipeline to intercept the request, and the barricade forbids core importing commercial.
The per-tenant agent registry is config-driven here (hand-edited YAML). The **ENTERPRISE
depth** is the managed registry console + routing-decision audit + a managed ML intent
classifier (F3 / commercial), layered on top without gating this engine.

Default OFF / no-op: with no registered agents (or `orchestration.enabled` false) this
stage returns the context untouched — the normal LLM path runs, byte-identical. Because
it is opt-in and only active when a tenant registers agents, it never perturbs the
published savings baseline (kept OUT of the pitch GROUPS registry).

Dispatch is a request-path short-circuit that REPLACES the LLM call, exactly like the G06
`cascade_response` precedent: this stage sets `ctx.agent_dispatched` + `ctx.agent_response`
and the pipeline returns early; `main.py` serves the agent's answer through
`process_response` so billing + response-side groups still fire.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "F2"

# Hard ceiling on a per-agent dispatch timeout regardless of config — a tenant-registered
# agent must never be able to tie up a request coroutine indefinitely (portal-side
# validation also enforces this on save; this is defense-in-depth for statically
# config-authored agents that bypass the portal).
MAX_AGENT_TIMEOUT_SECONDS = 300

_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "localhost"}

try:  # Prometheus is always present in the proxy image; degrade gracefully in bare tests.
    from prometheus_client import Counter as _Counter

    AGENT_DISPATCH_TOTAL = _Counter(
        "token_opt_agent_dispatch_total",
        "Requests dispatched to a registered downstream agent by intent orchestration",
        ["tenant_id", "agent_id", "outcome"],  # outcome: dispatched | error
    )
except Exception:  # pragma: no cover - metrics optional
    AGENT_DISPATCH_TOTAL = None


def _orchestration_cfg(config: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    """Effective orchestration config for a tenant: global `orchestration` with a
    per-tenant override (`tenants.<id>.orchestration.*`) taking precedence (Gate 2)."""
    if not isinstance(config, dict):
        return {}
    base = dict(config.get("orchestration", {}) or {})
    tenant_over = (
        (config.get("tenants", {}) or {})
        .get(tenant_id, {})
        .get("orchestration", {})
    )
    if isinstance(tenant_over, dict):
        merged = dict(base)
        merged.update(tenant_over)
        # `agents` is a per-tenant list — a tenant override REPLACES the global list
        # (never merges) so tenant A's agents never leak into tenant B.
        if "agents" in tenant_over:
            merged["agents"] = tenant_over["agents"]
        return merged
    return base


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    """The most recent user turn's text (what the intent is classified from)."""
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # multimodal: concatenate text parts
                return " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
    return ""


def validate_outbound_url(url: str) -> None:
    """Raise ValueError if `url` could route the proxy's own server-side request to an
    internal/private/link-local/reserved network or the cloud metadata service (SSRF).

    Agent URLs are tenant-supplied — via the F3 portal console (which also calls this at
    save time) or static config — so this proxy process must never be tricked into calling
    its own cloud metadata endpoint or internal infrastructure on a tenant's behalf.

    Deliberately does NOT resolve hostnames via DNS: this runs on every dispatch (the
    request path), and a live DNS lookup there would add network latency to every agent
    call and make tests depend on real DNS resolution. Instead it rejects (a) disallowed
    scheme, (b) a handful of known-dangerous literal hostnames, and (c) a literal IP
    address (e.g. the exact `http://169.254.169.254/...` metadata-service exploit) that
    resolves to a private/loopback/link-local/reserved/multicast network. A hostname that
    DNS-rebinds to an internal address after registration is a known residual gap —
    deployments with a stricter threat model should also enforce an egress allowlist at
    the network layer."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"agent url scheme must be http/https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("agent url has no host")
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"agent url host {host!r} is not allowed")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not a literal IP — DNS-rebind protection is out of scope here
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        raise ValueError(f"agent url host {host!r} is a disallowed address ({addr})")


def classify_intent(
    text: str, agents: List[Dict[str, Any]], threshold: int = 1
) -> Tuple[Optional[Dict[str, Any]], int]:
    """Pure heuristic intent → agent selection. Returns (agent, score).

    Scores each agent by how many of its `match` keywords appear (case-insensitive,
    word-boundary) in `text`; the highest-scoring agent at/above `threshold` wins (first
    on a tie — registry order is the tie-break). No match → (None, 0), i.e. fall back to
    the normal LLM. An agent with no `match` list is not heuristically selectable (its
    `description` is reserved for the managed ML classifier).
    """
    if not text:
        return None, 0
    low = text.lower()
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for agent in agents or []:
        if not isinstance(agent, dict):
            continue
        keywords = agent.get("match") or []
        score = 0
        for kw in keywords:
            kw = str(kw).strip().lower()
            if not kw:
                continue
            if re.search(r"\b" + re.escape(kw) + r"\b", low):
                score += 1
        if score > best_score:
            best, best_score = agent, score
    if best is not None and best_score >= max(1, int(threshold)):
        return best, best_score
    return None, best_score


class IntentOrchestration:
    """Intent-classify the request and dispatch to a registered downstream agent."""

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        # Respect every upstream short-circuit — never dispatch a bypassed/cached/blocked
        # request, and never override a cascade result.
        if (ctx.bypassed or ctx.cache_hit or getattr(ctx, "security_blocked", False)
                or ctx.cascade_response is not None or ctx.agent_dispatched):
            return ctx

        cfg = _orchestration_cfg(ctx.config, ctx.tenant_id)
        if not cfg.get("enabled", False):
            return ctx
        agents = cfg.get("agents") or []
        if not agents:
            return ctx

        text = _last_user_text(ctx.messages)
        agent, score = classify_intent(text, agents, cfg.get("confidence_threshold", 1))
        if agent is None:
            return ctx  # no intent match → normal LLM path (fallback)

        agent_id = str(agent.get("id") or "agent")
        try:
            response = await self._dispatch(ctx, agent)
        except Exception as exc:
            # Dispatch failure must NEVER drop the request — fall back to the normal LLM.
            logger.warning("[%s] F2: agent '%s' dispatch failed (%s) — falling back to LLM",
                           ctx.request_id, agent_id, exc)
            if AGENT_DISPATCH_TOTAL is not None:
                AGENT_DISPATCH_TOTAL.labels(ctx.tenant_id, agent_id, "error").inc()
            return ctx

        ctx.agent_dispatched = True
        ctx.agent_response = response
        ctx.agent_id = agent_id
        # B1-equivalent for the agent-dispatch short-circuit: pipeline.py's own B1 step
        # (recording proxy_optimised_tokens/final_tokens_sent) sits AFTER Stage 3-5, which
        # this short-circuit skips entirely — without this, both fields stay at their
        # dataclass default of 0, so an agent response lacking a `usage` block would make
        # G18 report the request as ~100% savings regardless of what the agent actually
        # consumed. Same estimate-until-overwritten contract as pipeline.py: G18 still
        # overwrites this with the agent's real prompt_tokens when present.
        ctx.savings.proxy_optimised_tokens = ctx.current_request_token_count
        ctx.savings.final_tokens_sent = ctx.savings.proxy_optimised_tokens
        logger.info("[%s] F2: intent matched (score=%d) → dispatched to agent '%s'",
                    ctx.request_id, score, agent_id)
        if AGENT_DISPATCH_TOTAL is not None:
            AGENT_DISPATCH_TOTAL.labels(ctx.tenant_id, agent_id, "dispatched").inc()
        return ctx

    async def _dispatch(self, ctx: RequestContext, agent: Dict[str, Any]) -> Dict[str, Any]:
        """Forward the conversation to the agent's OpenAI-compatible endpoint and return
        its OpenAI-shaped completion dict. Provider-agnostic (no provider name strings):
        an agent is just an OpenAI-compatible URL reached through litellm's compatible
        transport — the same seam `providers.build_call` uses for `openai_compatible`."""
        import litellm

        url = agent.get("url")
        if not url:
            raise ValueError(f"agent '{agent.get('id')}' has no url")
        validate_outbound_url(url)
        # Keyless agents are allowed; litellm still wants a non-empty key placeholder.
        api_key = os.environ.get(agent.get("api_key_env", ""), "") or "no-key"
        model = agent.get("model") or ctx.model

        params: Dict[str, Any] = {}
        # Per-agent output budget (governance): cap max_tokens if configured.
        max_tokens = agent.get("max_tokens")
        if max_tokens:
            params["max_tokens"] = int(max_tokens)
        timeout = min(int(agent.get("timeout_seconds", 60)), MAX_AGENT_TIMEOUT_SECONDS)

        started = time.perf_counter()
        resp = await litellm.acompletion(
            model=model,
            messages=ctx.messages,
            base_url=url,
            custom_llm_provider="openai",  # litellm transport for any OpenAI-compatible host
            api_key=api_key,
            timeout=timeout,
            **params,
        )
        # Accumulate the agent's provider time into the SLA split (mirrors G06 cascade).
        ctx.llm_elapsed_ms += (time.perf_counter() - started) * 1000.0
        # The agent — not whatever G06 picked for the now-skipped main LLM call — served
        # this request; billing/cost pricing (G18) and the x-tokenlean-routed-model header
        # must reflect that, exactly like the G06 cascade path writes back its own pick.
        ctx.routed_model = model
        ctx.savings.routed_model = model
        return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
