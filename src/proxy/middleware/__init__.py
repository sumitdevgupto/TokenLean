from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from savings.models import SavingsRecord
from savings.calculator import count_messages_tokens, count_request_tokens
from protocols.base import DEFAULT_PROTOCOL_NAME


@dataclass
class RequestContext:
    """Mutable request state carried through the G0–G24 middleware pipeline."""

    request_id: str
    user_id: str
    original_messages: List[Dict[str, Any]]   # immutable snapshot of raw request
    messages: List[Dict[str, Any]]             # current (optimised) messages
    model: str                                  # model as requested by developer
    routed_model: str                           # model after G6 routing
    params: Dict[str, Any]                      # other LLM params (temp, max_tokens…)
    config: Dict[str, Any]                      # full config.yaml contents
    savings: SavingsRecord

    bypassed: bool = False                      # G4 set True → skip LLM call
    cache_hit: bool = False                     # G5 set True → return cached response
    cache_level: Optional[str] = None           # "L1" | "L2"
    cache_response: Optional[Dict] = None       # response to return from cache
    batch_deferred: bool = False                # G13 batched this request
    langfuse_trace: Optional[Any] = None        # active Langfuse trace object
    skip_groups: List[str] = field(default_factory=list)  # G24 adaptive bypass

    # ── Multi-tenancy (A1) ──────────────────────────────────────────────────
    tenant_id: str = "default"
    # Redis key namespace prefix — all cache/session writes use this prefix
    # so tenant data never bleeds across tenants.  Empty string = no namespace
    # (used in tests and single-tenant deployments).
    redis_prefix: str = ""
    # Qdrant collection scoped to this tenant.  Default matches the legacy env
    # var so existing single-tenant deployments are unaffected.
    qdrant_collection: str = "rag_docs"
    # Pricing tier — free (self-host / $0 floor) or enterprise (managed SaaS). Billing/
    # console only; optimisations are never gated by tier.
    pricing_tier: str = "free"
    # True when the authenticated key carries the admin/impersonation scope.
    # Gates cross-tenant header impersonation (resolver), arbitrary
    # x_rag_collection (G07), and the cross-tenant admin/GDPR endpoints.
    is_admin_key: bool = False
    # Set by the pipeline when an admin key impersonates another tenant via
    # X-Tenant-ID — carries the impersonating (actor) key's own tenant so G18
    # can write an impersonation audit row (I6). None = no impersonation.
    impersonator_tenant_id: Optional[str] = None
    # OpenTelemetry span for the active pipeline trace (set by tracing layer).
    otel_span: Optional[Any] = None
    # Provider adapter — set by OptimisationPipeline early in process_request.
    # Type is Any to avoid importing providers here; callers cast as needed.
    provider_adapter: Optional[Any] = None
    # Wall-clock ms spent inside provider LLM calls — the main call plus any
    # provider calls made inside middleware (G06 cascade/judge, G10 summary,
    # G09 schema). 0 = no provider call yet (cache hit / bypass / pre-LLM
    # error). The SLA metrics use it to split proxy latency from LLM latency.
    llm_elapsed_ms: float = 0.0
    # G06 cascade execution result. When set, G06 already produced the final
    # answer by running the tier cascade, so main.py returns it directly and
    # MUST NOT call the LLM again (avoids a duplicate provider call). None =
    # normal path (main.py makes the call).
    cascade_response: Optional[Dict] = None
    # Provider failover trail (#1 resilience). Each element is a
    # providers.resilience.Attempt recording one provider/model try and its
    # outcome (success | error | skipped_*). Populated by call_with_resilience;
    # empty on cache hit / bypass / non-resilient paths. Used by G18 (failover
    # metric) and the SLA surface. List[Any] to avoid importing providers here.
    provider_attempts: List[Any] = field(default_factory=list)

    # ── Trust & Safety (#2 PII redaction G29 / #3 guardrails G30) ────────────
    # Short-circuit pair (mirrors bypassed/cache_response): set by G30 on a hard
    # guardrail block OR by G29 when the tenant's PII policy is `block`. When
    # security_blocked is True the pipeline returns immediately (no optimisation,
    # cache, or LLM call) and main.py serves security_block_response — an
    # OpenAI-shaped completion with finish_reason "content_filter". A blocked
    # request is a served proxy decision → billed like a bypass (one usage row).
    security_blocked: bool = False
    security_block_response: Optional[Dict] = None
    # G30 verdict for the request. action ∈ {None,"allow","flag","block"} (None =
    # guardrails did not run); categories are PII-free rule categories
    # (e.g. "instruction_override","system_prompt_exfil"). Consumed by the G18
    # metric + the PII-free security audit row + the commercial Security tab.
    guardrail_action: Optional[str] = None
    guardrail_categories: List[str] = field(default_factory=list)
    # G30 RESPONSE-side verdict — the model's OUTPUT scanned for injection/jailbreak
    # content (a model echoing an attack payload, or emitting unsafe instructions).
    # Kept separate from guardrail_action (the request verdict) so the two are never
    # conflated. action ∈ {None,"flag","block"}; block withholds the unsafe answer
    # with a content-filter response. Non-streaming only (see G30.process_response).
    guardrail_response_action: Optional[str] = None
    guardrail_response_categories: List[str] = field(default_factory=list)
    # G31 context-trust verdict. Distinct from guardrail_action (G30 scans the
    # untrusted *user* prompt) — G31 scans content INJECTED by retrieval/memory
    # (system/tool roles) for indirect prompt injection. action ∈
    # {None,"allow","flag","block","strip"}; categories are the same PII-free
    # attack classes. Consumed by the G18 context-trust metric + Security surface.
    context_trust_action: Optional[str] = None
    context_trust_categories: List[str] = field(default_factory=list)
    # G29 redaction summary. action ∈ {None,"flag","mask","block"}; entities are
    # PII-free entity TYPES only (e.g. "EMAIL","US_SSN") — never the matched text;
    # count is the number of spans redacted across request (+ non-stream response).
    pii_action: Optional[str] = None
    pii_entities: List[str] = field(default_factory=list)
    pii_redactions: int = 0
    # Ingress protocol the client used (default = the OpenAI identity protocol;
    # "anthropic" for /v1/messages, "gemini" for …:generateContent). The pipeline is
    # protocol-agnostic (OpenAI-shaped internally) — this only flows into
    # usage_events.protocol so per-protocol volume is filterable. (#4 ingress.)
    ingress_protocol: str = DEFAULT_PROTOCOL_NAME
    # Reversible-mask vault: placeholder token → original PII span, populated by G29
    # when mode=mask + reversible so the non-streaming response can restore the
    # caller's own data. IN-MEMORY ONLY for the request lifetime — the one field
    # that holds raw PII; it is never logged, audited, billed, or persisted.
    pii_vault: Dict[str, str] = field(default_factory=dict)
    # Set by G29 when it MASKS PII: masking makes the G05 cache key lossy (two
    # different PII values collapse to the same placeholder → the same key), so a
    # PII-bearing masked request must NOT read or write the shared cache — otherwise
    # one caller's answer could be served to another's look-alike query. G05 honours
    # this on both read and store. (flag mode leaves content unmasked → keys stay
    # unique → caching is safe and this stays False.)
    no_cache: bool = False

    @property
    def current_token_count(self) -> int:
        return count_messages_tokens(self.messages, self.model)

    @property
    def current_request_token_count(self) -> int:
        """Tools-inclusive token count of the current (optimised) request — same
        basis as baseline_tokens, so B1's y is apples-to-apples with x."""
        return count_request_tokens(self.messages, self.model, self.params.get("tools"))

    @classmethod
    def create(
        cls,
        request_id: str,
        user_id: str,
        messages: List[Dict[str, Any]],
        model: str,
        params: Dict[str, Any],
        config: Dict[str, Any],
        tenant_id: str = "default",
        redis_prefix: str = "",
        qdrant_collection: str = "rag_docs",
        pricing_tier: str = "free",
    ) -> "RequestContext":
        import copy

        # Include tool-definition tokens in the baseline so it matches what
        # the provider's usage.prompt_tokens actually bills for requests
        # carrying `tools` — otherwise live %Actual is skewed negative for
        # tool-heavy datasets (DS3/DS7).
        baseline_tokens = count_request_tokens(messages, model, params.get("tools"))
        savings = SavingsRecord(
            request_id=request_id,
            user_id=user_id,
            timestamp=datetime.now(timezone.utc),
            model_requested=model,
            routed_model=model,
            baseline_tokens=baseline_tokens,
        )
        return cls(
            request_id=request_id,
            user_id=user_id,
            original_messages=copy.deepcopy(messages),
            messages=copy.deepcopy(messages),
            model=model,
            routed_model=model,
            params=dict(params),
            config=config,
            savings=savings,
            tenant_id=tenant_id,
            redis_prefix=redis_prefix,
            qdrant_collection=qdrant_collection,
            pricing_tier=pricing_tier,
        )
