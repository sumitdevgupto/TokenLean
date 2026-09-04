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


REASON_NOT_INJECTED = "not_injected"
REASON_POLICY_DENIED = "policy_denied"
REASON_EVALUATION_ERROR = "evaluation_error"


class StreamToolGate:
    """Applies the tenant's tool policy to a STREAMED response, chunk by chunk.

    Backlog #25. ``main._stream_response`` relays provider chunks and never calls the
    response pipeline, so before this a tenant's DENY rule simply did not apply to
    streaming: the call was relayed and the caller's own agent loop executed it — a
    security control that silently did nothing on the most common agentic path.

    Note what this is NOT. Streaming also skips G15/G28, so the proxy never
    server-side-executes a streamed tool call. The exposure was policy NON-ENFORCEMENT,
    not proxy execution.

    Gating keys off the tool NAME, which arrives in the FIRST delta for a given tool-call
    index (arguments stream afterwards), so no full-argument buffering is needed and the
    added latency is nil in the normal case. Only deltas for an index whose name has not
    yet appeared are withheld — a shape providers do not currently emit, handled so a
    future one cannot slip past unevaluated.

    Modes match the non-streaming path exactly: ``off`` never evaluates, ``flag`` records
    but relays untouched (flag governs the record, not the response), ``block`` drops the
    denied call's deltas.
    """

    __slots__ = ("_ctx", "_cfg", "_mode", "_policy", "_verdicts", "_held",
                 "denied", "stripped")

    def __init__(self, ctx: RequestContext, cfg: Dict[str, Any], mode: str,
                 policy: ToolPolicy) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._mode = mode
        self._policy = policy
        self._verdicts: Dict[Any, bool] = {}
        self._held: Dict[Any, List[Any]] = {}
        self.denied: List[str] = []
        self.stripped = False

    def _allowed(self, name: str) -> bool:
        try:
            verdict = evaluate_tool(self._policy, name)
        except Exception as exc:
            # Fail-CLOSED, bounded to this one call — the same posture as the
            # non-streaming path, for the same reason.
            logger.error(
                "[%s] G32 stream policy evaluation failed for tool %r (%s) — denying",
                self._ctx.request_id, name, exc,
            )
            return False
        if verdict.allowed:
            return True
        self.denied.append(name or "<unnamed>")
        return False

    def _keep(self, entry: Any) -> bool:
        """Whether one ``tool_calls`` delta entry may be relayed."""
        if not isinstance(entry, dict):
            return True                       # not ours to interpret
        idx = entry.get("index", 0)
        if idx in self._verdicts:
            return self._verdicts[idx]
        fn = entry.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            allowed = self._allowed(name)
            self._verdicts[idx] = allowed
            return allowed
        # No name yet for this index: it cannot be evaluated, so it must not be relayed.
        self._held.setdefault(idx, []).append(entry)
        return False

    def filter(self, chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the chunk to emit, or None to drop it entirely."""
        if self._mode == "off" or not isinstance(chunk, dict):
            return chunk
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return chunk

        touched = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            calls = delta.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue
            kept = [c for c in calls if self._keep(c)]
            if len(kept) == len(calls):
                continue
            touched = True
            if self._mode != "block":
                continue                      # flag: recorded above, relayed untouched
            self.stripped = True
            if kept:
                delta["tool_calls"] = kept
            else:
                delta.pop("tool_calls", None)
                # Only correct a finish_reason that actually said "tool_calls" — same
                # care as the non-streaming path.
                if choice.get("finish_reason") == "tool_calls":
                    choice["finish_reason"] = "stop"

        if not touched or self._mode != "block":
            return chunk
        # Drop a chunk that now carries nothing: relaying an empty delta with no
        # finish_reason is noise a client has no reason to expect.
        for c in choices:
            if not isinstance(c, dict):
                continue
            d = c.get("delta") or {}
            if d.get("content") is not None or d.get("tool_calls") or c.get("finish_reason"):
                return chunk
        return None

    def finish(self) -> None:
        """Record the verdict once the stream is complete.

        Deltas still held (an index whose name never arrived) are never relayed: an
        unevaluable tool call is malformed provider output, and fail-closed is exactly
        what ``evaluate_tool`` does for a missing name.
        """
        for idx in self._held:
            if idx not in self._verdicts:
                self.denied.append("<unnamed>")
                if self._mode == "block":
                    self.stripped = True
        if not self.denied:
            return
        ctx = self._ctx
        ctx.tool_eligibility_action = self._mode
        ctx.tool_eligibility_denied = list(dict.fromkeys(self.denied))
        ctx.tool_eligibility_count = len(self.denied)
        if self.stripped:
            ctx.no_cache = True
        try:
            if self._cfg.get("metrics_enabled", True):
                from middleware.quality_metrics import record_tool_denied
                record_tool_denied(getattr(ctx, "tenant_id", "default"),
                                   mode=self._mode, n=len(self.denied))
        except Exception as exc:              # never let metrics break a stream
            logger.debug("[%s] G32 stream metric emit failed: %s", ctx.request_id, exc)
        logger.warning(
            "[%s] G32 %s tool-eligibility (streaming): %d call(s) denied %s",
            ctx.request_id, self._mode, len(self.denied), ctx.tool_eligibility_denied,
        )


def authorize_dispatch(ctx: RequestContext, tool_name: str) -> Optional[str]:
    """Second, INDEPENDENT authorization check at an auto-EXECUTION site.

    ``process_response`` above gates what the caller SEES. This gates what the proxy
    itself DOES, at the point of doing it — ``g15_server_compute`` and ``g28_ccr`` both
    dispatch server-side handlers by bare tool-name match. Until now that ordering
    (G32 runs before G14/G28/G15) was the only thing standing between a prompt-injected
    model and the proxy acting on its behalf, and ordering is a property of
    ``pipeline.py``, not of the dispatch site. Now the sink refuses on its own.

    Returns ``None`` to allow, else a short machine reason for the refusal.

    Three rules, in order:

    1. **Not advertised by us → refuse** (``not_injected``). Checked before policy and
       regardless of mode, because this is not policy — it is identity. Only G28's
       ``expose_mcp_tools`` path puts these names in front of a model, so a call to one
       we never offered is an injection, a hallucination, or a tenant's own same-named
       tool being hijacked. None of the three should execute.
    2. **``mode: off`` → allow.** ``off`` means the policy is not evaluated, and the
       dispatch site honours that so operators keep a real kill switch. Rule 1 still
       applies — no mode licenses running a name we never advertised.
    3. **Policy denies → refuse** (``policy_denied``) **in every mode, ``flag``
       included.** Deliberate: ``flag`` is documented as leaving the *response*
       untouched, which is not the same as declining to *act*. Recording a call as
       denied and then executing it anyway is worse than not checking at all — the audit
       row testifies that we knew.

    **Fail-CLOSED** on an unexpected error (``evaluation_error``) — the opposite of the
    cache/bypass hoist in ``main.py``, and safe here for a concrete reason: refusing to
    dispatch leaves the tool call sitting in the response unexecuted, which is the normal
    path for every tool the proxy does not host. The caller still receives it. There is
    no outage mode, so the availability argument that justifies failing open there buys
    nothing here.
    """
    if not getattr(ctx, "ccr_tools_injected", False):
        return REASON_NOT_INJECTED
    try:
        cfg = resolve_group_config(ctx, "G32_tool_eligibility")
        if not cfg.get("enabled", True):
            return None
        if coerce_mode(cfg.get("mode"), _VALID_MODES, "flag") == "off":
            return None
        policy = normalize_policy(cfg.get("policy") or {})
        if policy.is_noop:
            return None
        return None if evaluate_tool(policy, tool_name).allowed else REASON_POLICY_DENIED
    except Exception as exc:
        logger.error(
            "[%s] G32 dispatch authorization failed for tool %r (%s) — refusing to "
            "execute it (the call is still returned to the caller, unexecuted)",
            getattr(ctx, "request_id", "?"), tool_name, exc,
        )
        return REASON_EVALUATION_ERROR


def record_dispatch_block(ctx: RequestContext, tool_name: str, reason: str) -> None:
    """Observability for a refused auto-execution.

    Uses its OWN ctx field, counter and audit action rather than G32's response-path
    ones. ``ctx.tool_eligibility_*`` are assigned by ``process_response``, so writing
    them here would clobber G32's ``denied`` list in the audit row, and
    ``record_tool_denied`` is a bare ``Counter.inc`` that would double-count a call
    denied at both sites.
    """
    blocked = getattr(ctx, "tool_dispatch_blocked", None)
    if blocked is None:
        ctx.tool_dispatch_blocked = blocked = []
    if tool_name not in blocked:
        blocked.append(tool_name)
    try:
        from middleware.quality_metrics import record_dispatch_blocked
        record_dispatch_blocked(getattr(ctx, "tenant_id", "default"), reason=reason)
    except Exception as exc:  # never let metrics break the response
        logger.debug("[%s] G32 dispatch-block metric failed: %s",
                     getattr(ctx, "request_id", "?"), exc)
    logger.warning(
        "[%s] G32 refused server-side dispatch of %r (%s) — the call is returned to the "
        "caller unexecuted",
        getattr(ctx, "request_id", "?"), tool_name, reason,
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

    def stream_gate(self, ctx: RequestContext) -> Optional[StreamToolGate]:
        """Build a gate for a streamed response, or None when there is nothing to do.

        Returning None on the default install (no policy configured) is what keeps
        streaming byte-identical — no per-chunk work happens at all. main.py never
        compiles a policy itself, so there is exactly one policy engine and one cache.
        """
        cfg = self._config(ctx)
        if not cfg.get("enabled", True):
            return None
        mode = coerce_mode(cfg.get("mode"), _VALID_MODES, "flag")
        if mode == "off":
            return None
        policy = self._get_policy(cfg, ctx)
        if policy.is_noop:
            return None
        return StreamToolGate(ctx, cfg, mode, policy)

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
