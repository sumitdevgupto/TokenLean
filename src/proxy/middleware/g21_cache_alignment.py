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
import re
from typing import Any, Dict, List, Optional, Tuple

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




_RELOCATION_HEADER = "Request-specific values (excluded from the cached prefix):"


def _compile_patterns(patterns: List[str]) -> List[Any]:
    """Compile operator-supplied volatility patterns, skipping (and logging) bad ones.

    A malformed regex must not take the request down, and it must not silently behave as
    "matches nothing" either — that would look exactly like a working stabiliser that
    never fires, which is the failure mode hardest to notice on a cost line.
    """
    compiled = []
    for pat in patterns or []:
        try:
            compiled.append(re.compile(pat))
        except re.error as exc:
            logger.warning("G21 stabilise: ignoring invalid pattern %r (%s)", pat, exc)
    return compiled


def _stabilise_system_messages(
    system_msgs: List[Dict[str, Any]],
    patterns: List[Any],
    max_chars: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Excise volatile spans from system content and return them for relocation.

    Provider prompt caches match from token 0 and stop at the first byte that differs, so
    one changing value early in a system prompt (a timestamp, a session id) invalidates the
    entire cached prefix behind it: the turn pays a full cache WRITE instead of a discounted
    read, at an identical token count. Removing the span from the prefix and re-emitting it
    AFTER the cached block keeps the prefix byte-stable while the model still sees the value.

    Relocation, never deletion or rewriting: the content is preserved verbatim. Deleting a
    timestamp the prompt depends on would be a correctness bug wearing a savings costume.
    """
    if not patterns:
        return system_msgs, []

    relocated: List[str] = []
    budget = max_chars
    out: List[Dict[str, Any]] = []
    for msg in system_msgs:
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            out.append(msg)
            continue
        spans: List[Tuple[int, int]] = []
        for rx in patterns:
            for m in rx.finditer(content):
                if m.end() > m.start():
                    spans.append((m.start(), m.end()))
        if not spans:
            out.append(msg)
            continue
        # Longest-span-wins over overlaps, then excise back-to-front so earlier indices
        # stay valid while cutting.
        spans.sort(key=lambda sp: (sp[0], -(sp[1] - sp[0])))
        merged: List[Tuple[int, int]] = []
        for start, end in spans:
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        new_content = content
        taken: List[str] = []
        for start, end in reversed(merged):
            fragment = content[start:end]
            if budget - len(fragment) < 0:
                # Over budget: leave this span in place rather than relocating a partial
                # value. A truncated identifier reaching the model would be worse than an
                # unstable prefix.
                continue
            budget -= len(fragment)
            taken.append(fragment)
            new_content = new_content[:start] + new_content[end:]
        if not taken:
            out.append(msg)
            continue
        relocated.extend(reversed(taken))
        cleaned = re.sub(r"[ 	]{2,}", " ", new_content).strip()
        copied = dict(msg)
        copied["content"] = cleaned
        out.append(copied)
    return out, relocated


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

        # ── Prefix stabilisation (#33) ───────────────────────────────────────────
        # Must run BEFORE align_prefix AND before _apply_cache_policy: the latter hashes
        # the concatenated system CONTENT into OpenAI's prompt_cache_key, so a volatile
        # span breaks caching twice over — the prefix bytes stop matching AND the request
        # is routed to a different cache shard. Stabilising first fixes both.
        stab_cfg = cfg.get("stabilise") or {}
        relocated: List[str] = []
        if stab_cfg.get("enabled", False):
            try:
                patterns = _compile_patterns(stab_cfg.get("patterns") or [])
                system_msgs, relocated = _stabilise_system_messages(
                    system_msgs, patterns,
                    int(stab_cfg.get("max_relocated_chars", 2000)),
                )
                if relocated:
                    # Trailing system message ONLY. Never the first user turn: G05's L2
                    # store recomputes its semantic key from post-G21 USER turns, so
                    # prepending there would desynchronise store-from-lookup and poison
                    # the semantic cache.
                    note = _RELOCATION_HEADER + " " + " ".join(relocated)
                    variable_msgs = variable_msgs + [{"role": "system", "content": note}]
                    ctx.messages = system_msgs + variable_msgs
                    logger.debug(
                        "[%s] G21 stabilised prefix: relocated %d volatile span(s)",
                        ctx.request_id, len(relocated),
                    )
            except Exception as exc:
                # Never fail a request over a cost optimisation.
                logger.warning("[%s] G21 stabilise failed: %s", ctx.request_id, exc)
                relocated = []

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
        # Shared prefix profile (#33, cross-artefact): several internal artefacts that
        # share a system prompt should land on ONE provider cache shard instead of each
        # paying to build their own copy. Today convergence is accidental — the seed is the
        # concatenated system content, so prompts differing by a word get different shards.
        # An explicit profile makes it deliberate: every caller declaring the same profile
        # keys the same shard. It pins the KEY only; it cannot make genuinely different
        # prefixes match, so pair it with `stabilise` when artefacts still show writes.
        profile = (
            ctx.params.get("x_prefix_profile")
            or cfg.get("prefix_profile")
            or ""
        )
        if profile:
            cache_seed = f"profile:{profile}"
        else:
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
