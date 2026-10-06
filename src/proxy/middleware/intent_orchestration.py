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

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from middleware import RequestContext
from providers import without_cache_markers

logger = logging.getLogger(__name__)
GROUP = "F2"

# Hard ceiling on a per-agent dispatch timeout regardless of config — a tenant-registered
# agent must never be able to tie up a request coroutine indefinitely (portal-side
# validation also enforces this on save; this is defense-in-depth for statically
# config-authored agents that bypass the portal).
MAX_AGENT_TIMEOUT_SECONDS = 300

_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "localhost"}
# Names that reach only this host's private network: the reserved suffixes below, and a name
# of one label, such as a compose or Kubernetes service ("langfuse", "qdrant").
_INTERNAL_SUFFIXES = (".internal", ".local", ".localdomain", ".home.arpa", ".svc")
# An IPv4 address spelled as a number ("2852039166", "0xa9fea9fe", "0251.0376.0251.0376",
# "127.1"): the resolver accepts these, the ``ipaddress`` module does not.
_NUMERIC_HOST_RE = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+)){0,3}")

# Each refused (tenant, agent, api_key_env) warns once; the cap bounds the set.
_WARNED_KEY_ENV: set = set()
_MAX_WARNED_KEY_ENV = 1024
# A variable name as operators write one (LLM_KEY_OPENAI). The refusal names only such a value:
# a tenant may have pasted the key itself, and some keys are all capitals too.
_ENV_VAR_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")

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


def _operator_agents(tenant_id: str) -> List[Any]:
    """The agents the operator's own config defines for ``tenant_id`` (global
    ``orchestration.agents`` or the static ``tenants.<id>.orchestration.agents``), never
    the tenant overrides ctx.config also carries. None readable → none (fail closed)."""
    try:
        from config_loader import get_config  # the operator's file, never tenant overrides
        return _orchestration_cfg(get_config(), tenant_id).get("agents") or []
    except Exception:
        return []


def _operator_defines(agent: Dict[str, Any], tenant_id: str) -> bool:
    """Whether the operator's own config defines ``agent`` (the same ``id`` and ``url``)."""
    return any(isinstance(op, dict) and op.get("id") == agent.get("id")
               and op.get("url") == agent.get("url") for op in _operator_agents(tenant_id))


def _agent_key_env(agent: Dict[str, Any], tenant_id: str, request_id: str = "") -> str:
    """The environment variable holding ``agent``'s API key, or ``""`` for none.

    ``api_key_env`` names a SERVER environment variable, so only an operator may choose
    one. It is honoured only when the operator's own config — global
    ``orchestration.agents`` or the static ``tenants.<id>.orchestration.agents`` — defines
    an agent with the same ``id``, ``url`` and ``api_key_env``. ctx.config also carries
    tenant overrides written through the portal, and an agent found only there could name
    any server secret and have it sent to its own url; it is dispatched without a key.
    """
    name = agent.get("api_key_env")
    if not name:
        return ""
    for op in _operator_agents(tenant_id):
        if (isinstance(op, dict) and op.get("api_key_env") == name
                and op.get("id") == agent.get("id") and op.get("url") == agent.get("url")):
            return str(name)
    marker = (str(tenant_id), str(agent.get("id")), str(name)[:128])
    if marker not in _WARNED_KEY_ENV and len(_WARNED_KEY_ENV) < _MAX_WARNED_KEY_ENV:
        _WARNED_KEY_ENV.add(marker)
        shown = (marker[2] if _ENV_VAR_NAME.fullmatch(marker[2])
                 else "(withheld: not a variable name)")
        logger.warning(
            "[%s] F2: agent %r of tenant %r names api_key_env %r, but only agents defined "
            "in the operator config may read a server environment variable — dispatching "
            "without a key", request_id, marker[1], marker[0], shown)
    return ""


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


def _internal_address(addr: Any) -> bool:
    """Whether an address is anything but a public unicast one (private, loopback,
    link-local, shared, reserved, unspecified, multicast). An IPv4-mapped IPv6 address is
    never global to ``ipaddress`` (3.13 judges the mapped address instead), so
    ``[::ffff:169.254.169.254]`` is refused too."""
    return not addr.is_global or addr.is_multicast


def validate_outbound_url(url: str, *, internal_names_ok: bool = False) -> None:
    """Raise ValueError if `url` could route the proxy's own server-side request to an
    internal/private/link-local/reserved network or the cloud metadata service (SSRF).

    Agent URLs are tenant-supplied — via the F3 portal console (which also calls this at
    save time) or static config — so this proxy process must never be tricked into calling
    its own cloud metadata endpoint or internal infrastructure on a tenant's behalf.

    Checks the URL as written (no DNS lookup: the portal and webhooks call it on save):
    (a) the scheme, (b) the metadata and loopback names, (c) an IPv4 address spelled as a
    number ("2852039166" is 169.254.169.254), (d) a literal address that is not public,
    IPv4-mapped IPv6 included, and (e) unless ``internal_names_ok``, a name that only an
    internal network resolves: a single label (a compose or Kubernetes service) or an
    internal suffix (.internal, .local, .svc). A trailing dot changes nothing. An agent the
    operator's own config defines may name a host on the operator's network
    (``internal_names_ok``); F2 resolves a tenant's agent host before calling it
    (``check_resolved_host``)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"agent url scheme must be http/https, got {parsed.scheme!r}")
    host = (parsed.hostname or "").rstrip(".")          # .hostname is lowercased already
    if not host:
        raise ValueError("agent url has no host")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise ValueError(f"agent url host {host!r} is not allowed")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if _NUMERIC_HOST_RE.fullmatch(host):
            raise ValueError(
                f"agent url host {host!r} is an IP address in a non-standard form") from None
        if not internal_names_ok and ("." not in host or host.endswith(_INTERNAL_SUFFIXES)):
            raise ValueError(f"agent url host {host!r} names an internal host") from None
        return
    if _internal_address(addr):
        raise ValueError(f"agent url host {host!r} is a disallowed address ({addr})")


async def _host_addresses(host: str) -> List[str]:
    """Every address ``host`` resolves to, off the event loop."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


async def check_resolved_host(url: str) -> None:
    """Raise ValueError if the URL's host resolves to an address that is not public. Run
    before a tenant's agent is called: a public-looking name can point anywhere. It narrows
    DNS rebinding without closing it (the call resolves the name again); deployments with
    a stricter threat model should also enforce an egress allowlist at the network layer."""
    host = (urlparse(url).hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
        return                                  # a literal: validate_outbound_url judged it
    except ValueError:
        pass
    for raw in await _host_addresses(host):
        addr = ipaddress.ip_address(raw)                # a scoped "fe80::1%eth0" parses too
        if _internal_address(addr):
            raise ValueError(f"agent url host {host!r} resolves to a disallowed address ({addr})")


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
                or ctx.cascade_response is not None
                or getattr(ctx, "cascade_plan", None) is not None  # deferred cascade owns it
                or ctx.agent_dispatched):
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
        # An agent the operator's config defines may live on the operator's network (the
        # template's own example is a compose service); a tenant's may not, by name or by
        # what its name resolves to.
        operator_agent = _operator_defines(agent, ctx.tenant_id)
        validate_outbound_url(url, internal_names_ok=operator_agent)
        if not operator_agent:
            await check_resolved_host(url)
        # Keyless agents are allowed; litellm still wants a non-empty key placeholder.
        key_env = _agent_key_env(agent, ctx.tenant_id, ctx.request_id)
        api_key = (os.environ.get(key_env, "") if key_env else "") or "no-key"
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
            # An agent is an OpenAI-compatible URL, not a provider that caches by marker,
            # and litellm passes a custom endpoint the caller's cache markers as they are.
            messages=without_cache_markers(ctx.messages),
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
