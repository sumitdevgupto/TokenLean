"""
G21 · Provider Prompt Cache Alignment
Stage: Final request-side step (after G17, before LLM call)
Saving: 50-90% cost discount on reused prefix tokens
Technique:
  Reorder messages so shared prefixes (system prompts, tool definitions,
  few-shot examples) are contiguous at position 0, then inject provider-
  specific cache control markers.

  OpenAI: auto-caches contiguous prefixes >1024 tokens (since Dec 2024).
  Anthropic: cache_control markers on system + tool definitions (90% discount).

  This is a *cost* optimisation, not a token-count optimisation.
  Output is identical; only message order and metadata change.
"""
import copy
import logging
from typing import Any, Dict, List, Optional

from middleware import RequestContext, resolve_group_config
from middleware import langfuse_tracing
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G21"

# ─── Cache alignment strategy ─────────────────────────────────────────────────
# Done by the built-in _is_prefix_contiguous / reorder logic below.
# NOTE: Headroom's CacheAligner is intentionally NOT used — it performs prefix
# *stabilization* (whitespace / dynamic-content normalisation) and requires a
# Tokenizer(TokenCounter); that is a different, heavier technique than the
# system-first reordering here. Revisit as a dedicated enhancement if measured.




def _resolve_g21_cfg(ctx: RequestContext) -> Dict[str, Any]:
    """G21 config with the per-tenant override deep-merged in.

    Delegates to the shared ``middleware.resolve_group_config`` so every group
    resolves the tenant overlay identically AND inherits its type guards:
    ``config.yaml`` is operator-edited, and an unguarded ``.get()`` chain over a
    mis-indented ``tenants:`` block raised ``AttributeError`` here — a 500 on every
    request for that tenant.
    """
    return resolve_group_config(ctx, "G21_cache_alignment")



class G21CacheAlignment:
    """Reorder messages and inject cache markers for provider prompt caching."""

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = _resolve_g21_cfg(ctx)
        if not cfg.get("enabled", False):
            return ctx

        # No LLM call on bypassed / cached requests → cache alignment is moot.
        if ctx.bypassed or ctx.cache_hit:
            return ctx

        provider = self._get_provider(ctx, cfg)
        if provider == "unknown":
            logger.debug("[%s] G21 skipped: unknown provider for %s", ctx.request_id, ctx.routed_model)
            return ctx

        messages = ctx.messages
        tokens_before = ctx.current_token_count

        # Partition messages into stable prefix vs variable suffix
        system_msgs = [m for m in messages if m.get("role") == "system"]
        variable_msgs = [m for m in messages if m.get("role") != "system"]

        # Provider-specific prefix alignment lives on the adapter (no provider-name
        # branching here): OpenAI reorders system-first, Anthropic injects cache_control,
        # others no-op. Each honours its own per-provider config inside align_prefix.
        # ctx.provider_adapter is set by the pipeline; fall back to resolving from the model
        # (tests / pre-pipeline call sites) so alignment still fires.
        adapter = ctx.provider_adapter
        if adapter is None:
            from providers import get_adapter
            adapter = get_adapter(ctx.routed_model, ctx.config.get("providers", []))
        reordered = False
        try:
            reordered = adapter.align_prefix(ctx, system_msgs, variable_msgs, cfg)
        except Exception as exc:
            logger.debug("[%s] G21 align_prefix failed: %s", ctx.request_id, exc)
            reordered = False

        # Provider cache policy (e.g. OpenAI prompt_cache_key) — provider-agnostic,
        # delegated to the adapter so middleware stays free of provider strings.
        # Runs whether or not a reorder happened; the cache key tracks the prefix.
        self._apply_cache_policy(ctx, system_msgs, cfg)

        if reordered:
            tokens_after = ctx.current_token_count
            prefix_tokens = count_messages_tokens(system_msgs, ctx.model)
            # Cost saving only (reorder doesn't change token count).
            # Provider discount is config-driven; defaults reflect published rates
            # (OpenAI ~50% on cached prefix, Anthropic ~90%).
            # Report what the prefix cache actually DID, not what config predicts it
            # would do. The old text interpolated `discount_pct` straight from config and
            # was never reconciled against the response — so the single number the proxy
            # published about prefix caching was an assumption. On Anthropic the marker is
            # off by default, meaning it routinely claimed a 90% discount on a prefix that
            # had not been cached at all. The measured counts arrive on the response (G18),
            # after this request-side step, so this step now states only what it did.
            ctx.savings.add_step(
                GROUP,
                f"Cache-aligned {provider} prefix={prefix_tokens}t "
                f"(cost effect measured per call — see cache_read_tokens/cache_write_tokens)",
                tokens_before,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G21-cache-alignment",
                span_input={"tokens_before": tokens_before, "provider": provider},
                output={"tokens_after": tokens_after, "prefix_tokens": prefix_tokens},
                metadata={
                    "provider": provider,
                    "reordered": True,
                    "prefix_tokens": prefix_tokens,
                },
            )
            logger.debug(
                "[%s] G21 cache-aligned for %s (prefix=%d tokens)",
                ctx.request_id, provider, prefix_tokens,
            )

        return ctx

    def _apply_cache_policy(
        self,
        ctx: RequestContext,
        system_msgs: List[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> None:
        """Merge provider cache-policy request params (e.g. OpenAI prompt_cache_key) into ctx.params.

        The cache key is bucketed by tenant + the stable system-prompt prefix so identical
        prefixes from the same tenant route to the same provider cache shard. Provider-agnostic:
        all provider specifics live in ``adapter.cache_policy_params`` (Gate 3).
        """
        adapter = ctx.provider_adapter
        if adapter is None:
            return
        parts: List[str] = []
        for m in system_msgs:
            content = m.get("content", "")
            parts.append(content if isinstance(content, str) else str(content))
        cache_seed = "".join(parts) or "default"
        tenant_id = getattr(ctx, "tenant_id", "default") or "default"
        try:
            policy = adapter.cache_policy_params(ctx.routed_model, tenant_id, cache_seed, cfg)
        except Exception as exc:  # never break the request over a cache hint
            logger.debug("[%s] G21 cache_policy_params failed: %s", ctx.request_id, exc)
            return
        if policy:
            ctx.params.update(policy)
            logger.debug(
                "[%s] G21 cache policy params: %s", ctx.request_id, sorted(policy.keys())
            )

    def _get_provider(self, ctx: RequestContext, cfg: Dict[str, Any]) -> str:
        """Return provider name from ctx.provider_adapter when set, else fall back to model heuristics."""
        if ctx.provider_adapter is not None:
            return ctx.provider_adapter.name
        return self._detect_provider(ctx.routed_model, cfg)

    def _detect_provider(self, model: str, cfg: Dict[str, Any]) -> str:
        """Detect provider from model name using configurable prefix mappings."""
        # Use config-driven provider detection if available
        provider_prefixes = cfg.get("provider_detection", {})
        model_lower = model.lower()

        if provider_prefixes:
            for provider_name, prefixes in provider_prefixes.items():
                if isinstance(prefixes, list):
                    if any(p in model_lower for p in prefixes):
                        return provider_name
            return "unknown"

        # Fall back to global provider model_prefixes from config
        from config_loader import get_provider_model_prefixes
        for prefix, provider_name in get_provider_model_prefixes().items():
            if model_lower.startswith(prefix):
                return provider_name
        return "unknown"

    @staticmethod
    def _is_prefix_contiguous(messages: List[Dict], system_msgs: List[Dict]) -> bool:
        """Check if system messages are already contiguous at the start."""
        if not system_msgs:
            return True
        n = len(system_msgs)
        if len(messages) < n:
            return False
        for i in range(n):
            if messages[i].get("role") != "system":
                return False
        return True
