"""
G12 · Reasoning Budget Control
Stage: Inside the LLM (parameter injection)
Saving: 50–90% reasoning tokens
Technique: Inject provider-specific reasoning budget parameters via ctx.provider_adapter.
           WARNING: over-constraining hurts accuracy — validate on your workload.
"""
import logging
from typing import Any, Dict, List

from middleware import RequestContext
from middleware import langfuse_tracing
from providers import REASONING_OFF

logger = logging.getLogger(__name__)
GROUP = "G12"


class G12ReasoningBudget:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G12_reasoning", {})
        if not cfg.get("enabled", False):
            return ctx

        effort: str = (
            ctx.params.get("reasoning_effort")
            or cfg.get("default_effort", "medium")
        )

        applied = False
        tokens_before_note = ctx.current_token_count

        # Resolve adapter; fall back to OpenAI when not set (e.g. in unit tests)
        adapter = ctx.provider_adapter
        if adapter is None:
            from providers.openai_adapter import OpenAIAdapter
            adapter = OpenAIAdapter()

        if adapter.supports_reasoning(ctx.routed_model.lower(), ctx.config):
            reasoning_params = adapter.map_reasoning_effort(effort, ctx.config)
            for key, value in reasoning_params.items():
                if key not in ctx.params:
                    ctx.params[key] = value
                    applied = True
            # When the provider expresses reasoning via a native param (Anthropic
            # `thinking`, Gemini `thinking_config`) rather than `reasoning_effort`,
            # drop the now-redundant `reasoning_effort`. Otherwise litellm ALSO
            # expands it into a second thinking budget downstream — which Anthropic
            # 400s on when max_tokens is small (cap_reasoning_params caps the native
            # param but never sees litellm's reasoning_effort→thinking expansion).
            if applied and "reasoning_effort" not in reasoning_params:
                ctx.params.pop("reasoning_effort", None)

            if effort == REASONING_OFF:
                # `off` emits nothing, so `applied` is False and the pop above never
                # runs. A `reasoning_effort` already sitting in ctx.params would then
                # survive and litellm would expand it straight back into a thinking
                # budget — re-enabling precisely what was just turned off. Clear every
                # reasoning key the provider layer recognises, except any the adapter
                # deliberately emitted for `off` (Gemini's explicit thinking_budget: 0).
                for _rk in adapter.reasoning_param_keys():
                    if _rk not in reasoning_params:
                        ctx.params.pop(_rk, None)
                # Record what the provider can actually DELIVER, not what was asked.
                # Omitting `reasoning_effort` on an o-series model selects the model's
                # default; it does not stop it reasoning. Collapsing these two into one
                # "reasoning disabled" would credit a saving that never happened.
                ctx.reasoning_mode = (
                    "off_honoured" if adapter.can_disable_reasoning(ctx.routed_model)
                    else "off_unsupported"
                )
            else:
                ctx.reasoning_mode = effort

        # Inject reasoning-suppression prompt for low/medium effort
        suppression_prompts = cfg.get("reasoning_suppression_prompts", {})
        suppression = suppression_prompts.get(effort) if suppression_prompts else None
        if suppression:
            ctx.messages = _inject_suppression(ctx.messages, suppression)
            logger.debug(
                "[%s] G12 reasoning suppression injected for effort=%s",
                ctx.request_id,
                effort,
            )

        if applied or suppression or effort == REASONING_OFF:
            tokens_after = ctx.current_token_count
            if suppression:
                # Suppression appends prompt text; recount only to record G12's
                # token investment via add_step below. Do NOT mutate
                # ctx.savings.baseline_tokens — it is the immutable ingress
                # baseline set once from original_messages (A1; billing-critical).
                from savings.calculator import count_messages_tokens
                tokens_after = count_messages_tokens(ctx.messages, ctx.model)

            ctx.savings.add_step(
                GROUP,
                f"Reasoning budget: effort={effort} mode={ctx.reasoning_mode} "
                f"provider={adapter.name} (investment: +{tokens_after - tokens_before_note}t)",
                tokens_before_note,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G12-reasoning-budget",
                span_input={"effort": effort, "model": ctx.routed_model},
                output={
                    "applied": applied,
                    "suppression_injected": bool(suppression),
                    "params_changed": list(ctx.params.keys()),
                },
                metadata={"effort": effort, "provider": adapter.name,
                          "reasoning_mode": ctx.reasoning_mode},
            )
            logger.debug(
                "[%s] G12 reasoning budget injected: effort=%s provider=%s model=%s",
                ctx.request_id,
                effort,
                adapter.name,
                ctx.routed_model,
            )

        return ctx


def _inject_suppression(messages: list, prompt: str) -> list:
    """Append suppression prompt to the last system message, or prepend a new one."""
    messages = list(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "system":
            messages[i] = {
                **messages[i],
                "content": messages[i].get("content", "") + "\n" + prompt,
            }
            return messages
    return [{"role": "system", "content": prompt}] + messages
