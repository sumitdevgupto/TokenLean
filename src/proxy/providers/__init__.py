"""
Provider adapter layer — abstracts OpenAI / Anthropic / Gemini API differences.

Downstream middleware calls ctx.provider_adapter.<method>() instead of
embedding provider-name string checks. Unknown model prefixes fall back to
the OpenAI adapter so single-provider deployments need no config change.
"""
from __future__ import annotations

import copy
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Request-param keys never forwarded into a batch item body.
_NON_FORWARDED_BATCH_PARAMS = {"model", "batch_topic"}


def build_batch_jsonl(items: List[Dict]) -> str:
    """Build OpenAI-style batch JSONL: one line per item with custom_id=request_id.

    Shared by the OpenAI direct-SDK lane and the litellm unified-batch lane so both
    produce an identical request envelope.
    """
    lines = []
    for it in items:
        body: Dict[str, Any] = {"model": it.get("model"), "messages": it.get("messages", [])}
        for k, v in (it.get("params") or {}).items():
            if k.startswith("_") or k.startswith("x_") or k in _NON_FORWARDED_BATCH_PARAMS:
                continue
            body[k] = v
        lines.append(json.dumps({
            "custom_id": it["request_id"],
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }))
    return "\n".join(lines)


def parse_batch_jsonl_results(text: str) -> List[Dict]:
    """Parse an OpenAI-format batch output JSONL into
    ``[{"request_id", "response"}]`` (status 200) or ``[{"request_id", "error"}]``.
    """
    results: List[Dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        custom_id = rec.get("custom_id")
        resp = rec.get("response") or {}
        if resp.get("status_code") == 200:
            results.append({"request_id": custom_id, "response": resp.get("body", {})})
        else:
            results.append({"request_id": custom_id, "error": json.dumps(resp.get("body", {}))})
    return results


def _file_content_text(content: Any) -> str:
    """Extract text from a batch output-file content object (litellm or openai SDK)."""
    if hasattr(content, "text"):
        return content.text
    if hasattr(content, "content"):
        raw = content.content
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if hasattr(content, "read"):
        raw = content.read()
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return str(content)


class UnsupportedProviderError(Exception):
    pass


# Open adapter registry — populated by @register_adapter on each adapter class and
# filled at import time by _autodiscover_adapters() (bottom of this module). Replaces
# the old hardcoded 3-entry dict so providers are added by dropping in an adapter file
# (or, for OpenAI-compatible providers, by config alone via GenericLiteLLMAdapter).
_REGISTRY: Dict[str, type] = {}


def register_adapter(name: str):
    """Class decorator: register an adapter under its canonical provider name."""

    def _decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return _decorator


def _first_present(source: Dict, keys: tuple) -> Optional[int]:
    """First key actually present in ``source``, coerced to int — else ``None``.

    Distinguishes "the provider reported zero" from "the provider reported nothing".
    Every cache-token field uses this: reporting an unknown as 0 would state a fact we
    do not have, which is the exact reporting defect these fields were added to close.
    """
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source:
            value = source[key]
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


class ProviderAdapter(ABC):
    """Base interface every provider adapter must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical provider name matching the `providers[].name` config key."""

    @abstractmethod
    def map_structured_output(
        self,
        format_type: str,
        schema: Optional[Dict] = None,
    ) -> Dict:
        """
        Return provider-specific params for structured/JSON output.

        format_type: "json_object" | "json_schema" | "text"
        schema: JSON-Schema dict (used when format_type == "json_schema")
        Returns dict to be merged into the LLM request params.
        """

    @abstractmethod
    def map_reasoning_effort(self, tier: str, config: Dict) -> Dict:
        """
        Return provider-specific reasoning-effort params.

        tier: "low" | "medium" | "high"
        config: full ctx.config dict (reads groups.g12_reasoning_budget.effort_tiers)
        Returns dict to be merged into the LLM request params.
        """

    def supports_reasoning(self, model: str) -> bool:
        """
        Return True if model supports reasoning-budget injection for this provider.
        Default True (Anthropic and Gemini: all models support it).
        OpenAI overrides to restrict to o-series models only.
        """
        return True

    def reasoning_param_keys(self) -> set:
        """
        Request-param keys that are only valid when the model supports reasoning.
        These are stripped before the LLM call when the routed model is
        non-reasoning (e.g. a developer sent reasoning_effort but G06 routing
        downgraded o4-mini → gpt-4o-mini). Union across providers by default;
        the strip only fires when supports_reasoning() is False, so listing a
        sibling provider's key here is harmless.
        """
        return {"reasoning_effort", "thinking"}

    def render_tools_for_counting(self, tools: List[Dict]) -> Optional[Tuple[str, int]]:
        """(body_text, constant_overhead) approximating how THIS provider bills tool
        definitions, or None to use the calculator's default (OpenAI packed-TS form).

        Measured 2026-08-08 (free count_tokens endpoints, DS13's 11-tool set): the same
        tools bill ~270-300 on OpenAI, 625 on Gemini, and 1,307 on Anthropic — provider
        serialisation differs up to 4.4x, so one render cannot serve all (review S10).
        Default None keeps every adapter byte-identical to the pre-S10 behaviour unless
        it overrides. The caller counts body_text with its own estimator and adds the
        constant (fixed prompt overhead the provider injects for tool use).
        """
        return None

    def inject_cache_control(self, messages: List[Dict]) -> List[Dict]:
        """
        Return messages annotated for provider-side prefix caching.
        Default no-op; Anthropic overrides to add cache_control headers.
        """
        return messages

    def apply_context_management(self, params: Dict, cfg: Dict) -> Dict:
        """
        Inject provider-native context management into the outgoing request params.

        Default no-op so OpenAI-only deployments are unaffected. Anthropic overrides
        this to add its native context-editing beta. `cfg` is the already-resolved
        `groups.context_editing` config (per-tenant override applied by the caller).
        """
        return params

    def cache_policy_params(
        self,
        model: str,
        tenant_id: str,
        cache_seed: str,
        cfg: Dict,
    ) -> Dict:
        """
        Return provider-specific request params that raise prompt-cache hit rate
        and lower cached-input cost (e.g. OpenAI ``prompt_cache_key`` /
        ``prompt_cache_retention``).

        Default no-op — OpenAI-only deployments and providers that need no request
        hint (Gemini implicit caching; Anthropic, whose cache_control is handled by
        ``inject_cache_control`` / G21) are unaffected. ``cache_seed`` is a stable
        identifier of the cacheable prefix (e.g. a hash of the system messages) used
        to bucket the cache key; ``cfg`` is the resolved ``G21_cache_alignment`` config.
        """
        return {}

    def cache_read_cost_multiplier(self, config: Dict) -> float:
        """
        Multiplier applied to provider-reported cached input tokens when computing
        actual cost. ``1.0`` = no discount (safe default for unknown providers).
        Providers override with their published cached-input discount (OpenAI ~0.5,
        Anthropic ~0.1, Gemini ~0.25), read from
        ``groups.G21_cache_alignment.providers.<name>.cache_read_multiplier``.
        """
        return 1.0

    def cache_write_cost_multiplier(self, config: Dict) -> float:
        """
        Multiplier applied to provider-reported cache-WRITE (cache-creation) tokens when
        computing actual cost, read from
        ``groups.G21_cache_alignment.providers.<name>.cache_write_multiplier``.

        ``1.0`` = billed at the normal input rate (correct for OpenAI, which does not
        surcharge cache writes, and the safe default for unknown providers). Anthropic
        charges a premium for writes (~1.25x on the 5-minute tier, ~2x on the 1-hour
        tier), which is why this exists at all: without it a write-heavy tenant's cost
        line understates their invoice while the token counts look identical.
        """
        return 1.0

    def cache_write_1h_cost_multiplier(self, config: Dict) -> float:
        """Multiplier for the subset of cache-write tokens stored on a 1-hour TTL.

        Only meaningful where a provider prices cache-creation by TTL tier (Anthropic).
        Defaults to :meth:`cache_write_cost_multiplier` so providers with a single write
        rate need no override and the split is a no-op for them.
        """
        return self.cache_write_cost_multiplier(config)

    def supports_service_tier(self) -> bool:
        """True if the provider accepts the OpenAI ``service_tier`` param (e.g. Flex).

        Default False so ``service_tier`` is stripped before non-supporting providers
        (Anthropic/Gemini reject it). OpenAI overrides to True.
        """
        return False

    # ── Provider routing & request hygiene (multi-provider) ──────────────────
    # Added for the 10-provider refactor. All have behaviour-preserving defaults so
    # the existing OpenAI/Anthropic/Gemini adapters are unaffected until call sites
    # and middleware opt in (see plan rows 5, 11, 12, 13).

    def requires_api_key(self) -> bool:
        """True if a single bearer key must be present before the provider is called.

        Default True. Providers using ambient/multi-field credentials (AWS SigV4 for
        Bedrock, Vertex ADC) override to False so the proxy's pre-call key guard is
        skipped and litellm reads credentials from the environment.
        """
        return True

    def build_call(
        self,
        model: str,
        provider_cfg: Dict,
        api_key: Optional[str],
    ) -> tuple:
        """Return ``(model_str, kwargs)`` for ``litellm.acompletion``.

        Centralises provider routing so every call site tells litellm *where* to send the
        request instead of relying on model-name heuristics. The default is
        behaviour-preserving for providers litellm auto-detects from the model name
        (OpenAI/Anthropic/Gemini): the bare model plus the api_key — note it deliberately
        does NOT forward ``api_base`` for these, since their config carries a cosmetic
        api_base today that was never passed (forwarding it could break litellm's native
        routing). Config-driven extensions, no per-provider code needed:
          * ``litellm_prefix``    → routes as ``"<prefix>/<model>"`` (e.g. ``mistral/…``).
          * ``openai_compatible`` + ``api_base`` → routes via litellm's OpenAI path with a
            custom ``base_url`` (covers OpenAI-compatible endpoints litellm doesn't list).
        Subclasses (Azure, Bedrock) override for non-uniform routing/auth.
        """
        kwargs: Dict[str, Any] = {}
        if api_key and self.requires_api_key():
            kwargs["api_key"] = api_key

        if provider_cfg.get("openai_compatible"):
            api_base = provider_cfg.get("api_base")
            if api_base:
                kwargs["base_url"] = api_base
            kwargs["custom_llm_provider"] = "openai"
            # Optional routing-only prefix: strip it before the call so the upstream
            # OpenAI-compatible API receives the bare model name (e.g. a request for
            # "opencode/deepseek-v4-pro" is sent to OpenCode Zen as "deepseek-v4-pro").
            route_prefix = provider_cfg.get("route_prefix")
            if route_prefix and model.startswith(route_prefix):
                model = model[len(route_prefix):]
            return model, kwargs

        prefix = provider_cfg.get("litellm_prefix")
        if prefix and not model.startswith(f"{prefix}/"):
            return f"{prefix}/{model}", kwargs
        return model, kwargs

    def unsupported_params(self) -> set:
        """Request-param keys this provider rejects, stripped before the call.

        Default empty. Used alongside litellm ``drop_params`` as an explicit belt to
        remove params the provider 400s on (e.g. ``parallel_tool_calls``/``logprobs`` on
        non-OpenAI, ``thinking`` on non-Anthropic). Subclasses extend.
        """
        return set()

    def cap_reasoning_params(self, params: Dict, max_tokens: Optional[int]) -> Dict:
        """Reconcile an injected reasoning/thinking budget with ``max_tokens``.

        Default no-op (OpenAI uses a string ``reasoning_effort`` with no numeric budget).
        Anthropic/Gemini override to keep the thinking budget below ``max_tokens`` —
        Anthropic 400s when ``max_tokens <= thinking.budget_tokens``.
        """
        return params

    def extract_usage(self, response: Dict) -> Dict:
        """Normalise provider usage into cached/reasoning/cache-write counts.

        Default reads the OpenAI shape (``prompt_tokens_details.cached_tokens`` and
        ``completion_tokens_details.reasoning_tokens``), with ``thinking_tokens`` honoured
        as a fallback so litellm-normalised Anthropic usage is still captured — identical
        to G18's current inline logic, so switching G18 to this is a no-op for OpenAI.
        Subclasses override for provider-specific fields (Anthropic ``cache_read_input_tokens``,
        Gemini ``cached_content_token_count``).

        Returns four keys:
          * ``cached_tokens`` / ``reasoning_tokens`` — ints, unchanged back-compat aliases
            (load-bearing in G18's cost math, which treats a missing value as 0);
          * ``cache_read_tokens`` / ``cache_write_tokens`` — ``Optional[int]``, where
            **None means the provider reported nothing** and 0 means it reported zero.
            A silent zero here is precisely the defect this field exists to fix: it would
            claim "no cache writes happened" when the truth is "we do not know".

        litellm normalises cache writes onto ``prompt_tokens_details.cache_write_tokens``
        (mirrored to ``cache_creation_tokens``) for every provider it routes, so the base
        implementation covers them all; the Anthropic/Gemini overrides only add a
        native-shape fallback for responses that never passed through litellm.
        """
        usage = response.get("usage", {}) or {}
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0) or 0
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        if not reasoning and usage.get("thinking_tokens"):
            reasoning = usage.get("thinking_tokens", 0) or 0
        return {
            "cached_tokens": cached,
            "reasoning_tokens": reasoning,
            "cache_read_tokens": _first_present(details, ("cached_tokens",)),
            "cache_write_tokens": _first_present(
                details, ("cache_write_tokens", "cache_creation_tokens")
            ),
        }

    def align_prefix(
        self,
        ctx: Any,
        system_msgs: List[Dict],
        variable_msgs: List[Dict],
        cfg: Dict,
    ) -> bool:
        """Provider-specific G21 prefix-cache alignment. Return True if messages changed.

        Default no-op. OpenAI reorders system-first; Anthropic injects cache_control
        markers. Lets G21 stay free of provider-name branching.
        """
        return False

    def requires_json_keyword(self) -> bool:
        """True if the provider's JSON mode requires the word 'json' in the prompt.

        OpenAI's ``response_format={"type":"json_object"}`` 400s without it. Default
        False; OpenAI overrides to True. Replaces a hardcoded ``name == "openai"`` check
        in G11.
        """
        return False

    # ── Provider-native async batch lane (G13 P2) ────────────────────────────
    # Captures the provider 50% batch discount for latency-tolerant traffic.
    # The base implementation routes through litellm's unified batch API
    # (custom_llm_provider=self.name), which normalises results to OpenAI shape —
    # no per-provider response conversion needed. Default supports_native_batch()
    # is False (opt-in): a provider must override it to True, else G13 uses the
    # per-item loop. Any litellm error → G13 catches it and falls back.

    def supports_native_batch(self) -> bool:
        """True if this adapter opts into the native batch lane. Default False."""
        return False

    async def submit_batch(self, items: List[Dict], api_key: str, cfg: Dict) -> str:
        """Submit ``items`` as one batch job via litellm and return its job id.

        ``custom_id`` is the ``request_id`` so results map back. Each item is
        ``{"request_id", "messages", "model", "params"}``.
        """
        import litellm
        payload = build_batch_jsonl(items).encode("utf-8")
        file_obj = await litellm.acreate_file(
            file=payload, purpose="batch", custom_llm_provider=self.name,
        )
        batch = await litellm.acreate_batch(
            input_file_id=file_obj.id,
            endpoint="/v1/chat/completions",
            completion_window=cfg.get("completion_window", "24h"),
            custom_llm_provider=self.name,
        )
        return batch.id

    async def poll_batch(self, job_id: str, api_key: str) -> str:
        """Return the job status: ``"pending"`` | ``"completed"`` | ``"failed"``."""
        import litellm
        batch = await litellm.aretrieve_batch(batch_id=job_id, custom_llm_provider=self.name)
        status = getattr(batch, "status", "") or ""
        if status == "completed":
            return "completed"
        if status in ("failed", "expired", "cancelled", "cancelling"):
            return "failed"
        return "pending"

    async def fetch_batch_results(self, job_id: str, api_key: str) -> List[Dict]:
        """Return completed results as
        ``[{"request_id", "response"}]`` / ``[{"request_id", "error"}]`` via litellm.
        """
        import litellm
        batch = await litellm.aretrieve_batch(batch_id=job_id, custom_llm_provider=self.name)
        out_id = getattr(batch, "output_file_id", None)
        if not out_id:
            return []
        content = await litellm.afile_content(file_id=out_id, custom_llm_provider=self.name)
        return parse_batch_jsonl_results(_file_content_text(content))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _match_entry(model: str, providers_config: List[Dict]) -> Optional[Dict]:
    """Return the providers[] entry that owns this model, else the default-provider entry.

    Shared by get_adapter / get_provider_entry / build_litellm_call so model→provider
    resolution is defined exactly once. Falls back to the configured
    proxy.default_provider (default "openai") — no longer hardcoded to OpenAI.
    """
    for provider in providers_config:
        if any(model.startswith(p) for p in provider.get("model_prefixes", [])):
            return provider
    try:
        from config_loader import get_default_provider
        default_name = get_default_provider()
    except Exception:
        default_name = "openai"
    entry = next((p for p in providers_config if p.get("name") == default_name), None)
    if entry is None:
        logger.debug(
            "No provider matched model=%r and default %r not in providers list",
            model, default_name,
        )
    return entry


def get_provider_entry(model: str, providers_config: List[Dict]) -> Optional[Dict]:
    """Public: the resolved providers[] config entry for this model (or None)."""
    return _match_entry(model, providers_config)


def get_adapter(model: str, providers_config: List[Dict]) -> ProviderAdapter:
    """
    Return the adapter for whichever provider owns this model.

    Iterates providers_config (list from config.yaml `providers:` key) and
    matches the first entry whose model_prefixes list has a prefix that the
    model string starts with.  Falls back to the configured proxy.default_provider
    (default "openai") for unknown models — no longer hardcoded to OpenAI.
    """
    entry = _match_entry(model, providers_config)
    if entry is not None:
        return _adapter_for_entry(entry)
    # providers list empty or default not present — resolve the default by name.
    try:
        from config_loader import get_default_provider
        return get_adapter_by_name(get_default_provider())
    except Exception:
        return get_adapter_by_name("openai")


def _adapter_for_entry(entry: Dict) -> ProviderAdapter:
    """Build the adapter for a matched providers[] entry.

    A registered adapter class wins; otherwise (``adapter: generic`` / ``openai_compatible``
    / a configured provider with no dedicated adapter class) the config-driven
    GenericLiteLLMAdapter is used — this is the config-only extension path.
    """
    name = entry.get("name", "")
    if (
        entry.get("adapter") == "generic"
        or entry.get("openai_compatible")
        or name not in _REGISTRY
    ):
        from providers.generic_adapter import GenericLiteLLMAdapter
        return GenericLiteLLMAdapter(name, entry)
    return get_adapter_by_name(name)


def build_litellm_call(
    model: str,
    providers_config: List[Dict],
    api_key: Optional[str],
) -> tuple:
    """Resolve ``(model_str, kwargs)`` for ``litellm.acompletion`` for this model.

    The single entry point every call site uses so api_base / api_version /
    custom_llm_provider and the routable model string are set consistently. Resolves
    the providers[] entry + adapter and delegates to ``adapter.build_call``.
    """
    entry = _match_entry(model, providers_config) or {}
    adapter = _adapter_for_entry(entry) if entry else get_adapter(model, providers_config)
    return adapter.build_call(model, entry, api_key)


def apply_context_management(
    params: Dict[str, Any],
    adapter: ProviderAdapter,
    config: Dict[str, Any],
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """
    Resolve the per-tenant `groups.context_editing` config and delegate to the adapter.

    Provider-agnostic entry point for the request path: non-Anthropic adapters'
    ``apply_context_management`` is a no-op, so this is safe to call unconditionally
    regardless of the routed provider. Default off; a tenant opts in via
    ``tenants.<id>.groups.context_editing.enabled: true``.
    """
    base = config.get("groups", {}).get("context_editing", {})
    tenant_cfg = (
        config.get("tenants", {})
        .get(tenant_id, {})
        .get("groups", {})
        .get("context_editing", {})
    )
    ce_cfg = {**base, **tenant_cfg}
    return adapter.apply_context_management(params, ce_cfg)


# Body params consumed by middleware for routing/retrieval/loop-control —
# not recognized by the provider's completion API and must not be forwarded.
# (Relocated from main.py 2026-08-08 so the deferred G06 cascade shares the
# same hygiene without a middleware→main circular import.)
INTERNAL_PARAM_KEYS = {"template_id", "workflow_id", "rag_query", "batch_id", "burst_block"}


def outgoing_params_for(ctx, adapter: ProviderAdapter, model: str,
                        eff_cfg: Dict[str, Any], request_id: str = "") -> Dict[str, Any]:
    """Build provider-hygienic outgoing params for `model` under `adapter`.

    THE single source of the provider param-hygiene contract — used by main.py's
    primary path, every failover target, and the deferred G06 cascade tiers alike
    (reviews K4 + S2: no divergent copies). Steps: strip internal keys, strip
    reasoning-only params on non-reasoning models, strip service_tier / unsupported
    params, cap the thinking budget, apply native context editing.
    """
    outgoing = {
        k: v for k, v in ctx.params.items()
        if not k.startswith("_") and not k.startswith("x_") and k not in INTERNAL_PARAM_KEYS
    }
    if not adapter.supports_reasoning(model):
        for rk in adapter.reasoning_param_keys():
            if outgoing.pop(rk, None) is not None and request_id:
                logger.debug(
                    "[%s] Stripped reasoning param '%s' — model %s does not support reasoning",
                    request_id, rk, model,
                )
    if "service_tier" in outgoing and not adapter.supports_service_tier():
        outgoing.pop("service_tier", None)
        if request_id:
            logger.debug("[%s] Stripped service_tier — %s does not support it", request_id, model)
    for _uk in adapter.unsupported_params():
        if outgoing.pop(_uk, None) is not None and request_id:
            logger.debug("[%s] Stripped unsupported param '%s' for %s", request_id, _uk, model)
    outgoing = adapter.cap_reasoning_params(outgoing, outgoing.get("max_tokens"))
    outgoing = apply_context_management(outgoing, adapter, eff_cfg, ctx.tenant_id)
    return outgoing


def get_adapter_by_name(name: str) -> ProviderAdapter:
    """
    Return an adapter instance for an explicit provider name from the open registry.
    Raises UnsupportedProviderError for names with no registered adapter.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnsupportedProviderError(
            f"No adapter registered for provider {name!r}. "
            f"Known providers: {sorted(_REGISTRY)}"
        )
    return cls()


def _autodiscover_adapters() -> None:
    """Import every ``providers/*_adapter.py`` so @register_adapter decorators run.

    Per-module errors are logged and skipped (an optional adapter whose SDK isn't
    installed must not take down the whole package).
    """
    import importlib
    import pkgutil

    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.endswith("_adapter"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Adapter module %r failed to import: %s", mod.name, exc)


_autodiscover_adapters()
