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

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "F2"

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
        # Keyless agents are allowed; litellm still wants a non-empty key placeholder.
        api_key = os.environ.get(agent.get("api_key_env", ""), "") or "no-key"
        model = agent.get("model") or ctx.model

        params: Dict[str, Any] = {}
        # Per-agent output budget (governance): cap max_tokens if configured.
        max_tokens = agent.get("max_tokens")
        if max_tokens:
            params["max_tokens"] = int(max_tokens)
        timeout = agent.get("timeout_seconds", 60)

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
        return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
