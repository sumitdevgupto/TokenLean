"""
G32 — Tool-Call Eligibility Gate

Checks every ``tool_calls`` entry the model requests against a per-tenant allow/deny
policy, on the RESPONSE path, **before any group that auto-executes a tool sees it**
(G14 tool-output → G28 CCR → G15 server-compute). That ordering *is* the guarantee: G15
dispatches server-side handlers by name match with no authorization of its own, so a
prompt-injected model could otherwise make the proxy *act*, not merely *answer*.

Where the other trust & safety groups sit:
  * G30 guards the untrusted **user prompt** (what comes in),
  * G31 guards **retrieved context** injected by RAG/memory (indirect injection),
  * G29 guards **PII** in both directions,
  * G32 guards what the model is about to **do**.

Policy modes (per-tenant via ``groups.G32_tool_eligibility.mode``):
  * ``off``   — passthrough; the policy is not evaluated (zero overhead).
  * ``flag``  — evaluate; record ineligible calls (metric + PII-free audit row +
                ``_token_opt`` annotation) but leave the response untouched. **Default.**
  * ``block`` — evaluate; **strip** ineligible calls from the message, keeping it
                well-formed, before any auto-executing group runs.

``off`` rather than G30/G31's ``allow`` is deliberate: this group's own config already
uses "allow" twice (``policy.allow`` and ``policy.default: allow``), so a third
unrelated ``mode: allow`` would be genuinely ambiguous to read. G29 uses ``off`` for
the same reason.

**Non-streaming only.** A streamed response bypasses the whole response pipeline
(``main._stream_response`` relays provider chunks unchanged), so a streaming client's
tool calls are NOT gated. This mirrors G29's and G30's response-side limitation and is
documented, not incidental — see the README/config-reference notes on ``mode``.

The policy engine lives in ``guardrails/tool_policy.py`` (OSS core). This middleware
only applies mode + records observability, and carries tool NAMES (function
identifiers, never prompt content) into metrics/audit.

Reference: G32 in internal-docs/demand-driven-features.md #1.
"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext, coerce_mode, resolve_group_config
from guardrails.tool_policy import (
    ToolPolicy,
    ToolPolicyError,
    evaluate_tool,
    normalize_policy,
)

logger = logging.getLogger(__name__)
GROUP = "G32"

_VALID_MODES = ("off", "flag", "block")
_NOOP_POLICY = ToolPolicy()
# Upper bound on distinct tenants cached at once (see _get_policy).
_POLICY_CACHE_MAX = 512
_DEFAULT_BLOCK_MESSAGE = (
    "A tool the model requested is not permitted by this workspace's tool policy, "
    "so it was not carried out."
)


class G32ToolEligibility:
    """Apply the per-tenant tool-eligibility policy to each response."""

    def __init__(self) -> None:
        # tenant_id -> (signature, ToolPolicy). Keyed by TENANT, not by signature alone:
        # one G32 instance serves every tenant, so a single slot both thrashed (each
        # tenant evicting the last) and — on the malformed-policy path below — could hand
        # tenant B the last-good policy compiled for tenant A. Each value is still a
        # single tuple, so a read is one atomic dict lookup returning a consistent pair
        # (GIL) and a hot-reload swap can never be observed torn.
        self._policy_cache: Dict[str, Tuple[str, ToolPolicy]] = {}

    # ── config / policy ──────────────────────────────────────────────────────
    def _config(self, ctx: RequestContext) -> Dict[str, Any]:
        return resolve_group_config(ctx, "G32_tool_eligibility")

    def _get_policy(self, cfg: Dict[str, Any], ctx: RequestContext) -> ToolPolicy:
        """Compile (and cache) this tenant's policy.

        On a malformed policy **this tenant's** last-good compiled policy is retained — a
        bad pattern arriving via hot-reload must not silently widen the gate. The
        last-good lookup is tenant-scoped: falling back to whatever policy another tenant
        last compiled would be an isolation break, applying one workspace's rules to
        another's traffic and making the verdict depend on request interleaving.

        When this tenant has no last-good (its very first load is already broken, which
        the portal's write-time 422 makes an operator-only path) we fall back to a no-op
        and log at ERROR: the alternative, denying every tool call across the tenant on a
        config typo, is an outage rather than a safeguard. The ERROR line is the signal;
        readiness surfaces it too.
        """
        raw = cfg.get("policy") or {}
        tenant_id = getattr(ctx, "tenant_id", "default")
        try:
            sig = json.dumps(raw, sort_keys=True, default=str)
        except (TypeError, ValueError):
            sig = repr(raw)
        cached = self._policy_cache.get(tenant_id)   # one atomic lookup (no torn pair)
        if cached is not None and cached[0] == sig:
            return cached[1]
        try:
            policy = normalize_policy(raw)
        except ToolPolicyError as exc:
            if cached is not None:
                logger.warning(
                    "[%s] G32 invalid tool policy for tenant %s (%s) — retaining that "
                    "tenant's last-good policy",
                    ctx.request_id, tenant_id, exc,
                )
                return cached[1]
            logger.error(
                "[%s] G32 invalid tool policy for tenant %s (%s) and no previously-valid "
                "policy for it to fall back on — the gate is INERT for this tenant until "
                "the config is fixed",
                ctx.request_id, tenant_id, exc,
            )
            return _NOOP_POLICY
        if len(self._policy_cache) >= _POLICY_CACHE_MAX and tenant_id not in self._policy_cache:
            # Bounded so a key-enumeration probe cannot grow this without limit. Rebuild
            # rather than pop so the swap stays a single atomic assignment; a dropped
            # entry only costs one recompile.
            self._policy_cache = {tenant_id: (sig, policy)}
        else:
            self._policy_cache[tenant_id] = (sig, policy)
        return policy

    # ── main entry point ─────────────────────────────────────────────────────
    async def process_response(
        self, ctx: RequestContext, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = self._config(ctx)
        if not cfg.get("enabled", True):
            return response
        mode = coerce_mode(cfg.get("mode"), _VALID_MODES, "flag")
        if mode == "off":
            return response
        if not isinstance(response, dict):
            return response

        policy = self._get_policy(cfg, ctx)
        if policy.is_noop:
            # Empty lists + `default: allow` can never deny anything, so skip the walk
            # entirely. This is what keeps a default install byte-identical.
            return response

        denied: List[str] = []
        stripped_any = False
        for choice in response.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            if self._apply_to_choice(ctx, choice, policy, mode, cfg, denied):
                stripped_any = True

        if not denied:
            return response

        ctx.tool_eligibility_action = mode
        ctx.tool_eligibility_denied = list(dict.fromkeys(denied))   # de-dup, keep order
        ctx.tool_eligibility_count = len(denied)
        if stripped_any:
            # The cached artifact would otherwise be POLICY-SPECIFIC: loosen the policy
            # and G05 keeps serving the stripped answer until TTL. Same reason G29 (mask)
            # and G30 (response block) set this. `flag` mutates nothing, so it never does.
            ctx.no_cache = True

        self._emit_metric(ctx, mode, len(denied), cfg)
        self._annotate(response, mode, ctx.tool_eligibility_denied, stripped_any)
        logger.warning(
            "[%s] G32 %s tool-eligibility: %d call(s) denied %s",
            ctx.request_id, mode, len(denied), ctx.tool_eligibility_denied,
        )
        return response

    # ── per-choice application ───────────────────────────────────────────────
    def _apply_to_choice(
        self,
        ctx: RequestContext,
        choice: Dict[str, Any],
        policy: ToolPolicy,
        mode: str,
        cfg: Dict[str, Any],
        denied: List[str],
    ) -> bool:
        """Evaluate one choice's tool calls. Returns True if anything was stripped.

        ``denied`` is appended to in place (one entry per denied call, so the metric
        counts calls rather than distinct names).
        """
        msg = choice.get("message")
        if not isinstance(msg, dict):
            return False
        calls = msg.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return False

        kept: List[Any] = []
        for call in calls:
            name = ""
            if isinstance(call, dict):
                fn = call.get("function")
                if isinstance(fn, dict):
                    name = fn.get("name") or ""
            try:
                verdict = evaluate_tool(policy, name)
            except Exception as exc:
                # Fail-CLOSED, but bounded to this one call: a gate that fails open is
                # not a gate, and a gate that fails the whole response is an outage.
                logger.error(
                    "[%s] G32 policy evaluation failed for tool %r (%s) — denying this call",
                    ctx.request_id, name, exc,
                )
                verdict = None
            if verdict is not None and verdict.allowed:
                kept.append(call)
                continue
            denied.append(name or "<unnamed>")
            if mode != "block":
                kept.append(call)          # flag: record only, response is untouched

        if len(kept) == len(calls):
            return False                   # nothing stripped (flag mode, or all allowed)

        if kept:
            msg["tool_calls"] = kept       # partial denial: finish_reason stays as-is
        else:
            # Full denial. Drop the key entirely rather than leaving `[]` — some SDKs
            # treat an empty list as a malformed assistant message.
            msg.pop("tool_calls", None)
            # Only correct a finish_reason that actually said "tool_calls". Providers
            # sometimes return "stop" alongside tool calls, and clobbering that is its
            # own bug.
            if choice.get("finish_reason") == "tool_calls":
                choice["finish_reason"] = "stop"
            if not msg.get("content"):
                # An assistant message with neither content nor tool_calls is invalid
                # for most clients.
                msg["content"] = cfg.get("block_message", _DEFAULT_BLOCK_MESSAGE)
        return True

    # ── observability ────────────────────────────────────────────────────────
    def _emit_metric(self, ctx: RequestContext, mode: str, n: int, cfg: Dict[str, Any]) -> None:
        if not cfg.get("metrics_enabled", True):
            return
        try:
            from middleware.quality_metrics import record_tool_denied
            record_tool_denied(getattr(ctx, "tenant_id", "default"), mode=mode, n=n)
        except Exception as exc:  # never let metrics break the response
            logger.debug("[%s] G32 metric emit failed: %s", ctx.request_id, exc)

    def _annotate(
        self, response: Dict[str, Any], mode: str, denied: List[str], stripped: bool
    ) -> None:
        """Surface the verdict on ``_token_opt`` so a caller can see WHY a tool call it
        expected is missing. Note ``_token_opt`` is dropped on Anthropic/Gemini egress —
        enforcement still applied, only this annotation is lost for those clients."""
        try:
            response.setdefault("_token_opt", {})["tool_eligibility"] = {
                "mode": mode,
                "denied": denied,
                "stripped": stripped,
            }
        except Exception:  # a non-dict response must never break on annotation
            pass
