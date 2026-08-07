"""
Shared conversation-history helpers.

Two history primitives are needed by more than one middleware group (G10 memory's
sliding window and G26 budget-aware compaction), so they live here rather than
being imported across groups:

  * ``safe_window_split`` — tool-pairing-aware cut index.
  * ``summarise_turns``   — cheap-model summary of an old conversation span.

Both were extracted verbatim from ``g10_memory`` (which now alias-imports them),
so their behaviour — including the ``"[summary unavailable]"`` failure string,
BYOK key resolution, LLM-time accounting and circuit-breaker feed — is unchanged.
"""
import logging
import time
from typing import Any, Dict, List

from middleware import RequestContext

logger = logging.getLogger(__name__)


def safe_window_split(turns: List[Dict], keep: int) -> int:
    """Index at which to split ``turns`` into (summarised-old, kept-recent) so the
    kept tail never begins with an orphaned tool result.

    A blind positional cut — ``turns[-keep:]`` — can start the kept tail on a
    ``role:"tool"`` message whose declaring ``assistant``/``tool_calls`` turn was
    pushed into the summarised region. That leaves a ``tool_call_id`` with no
    matching assistant ``tool_calls[].id`` in the list handed to the provider,
    which litellm / OpenAI / Anthropic reject with a **400** — and it strikes
    exactly on the long multi-turn agentic (tool-calling) conversation that trips
    the window in the first place. Tool results always immediately follow their
    assistant turn in OpenAI shape, so snap the boundary earlier over any leading
    run of tool results: the tail then starts on the assistant turn that declared
    them and the whole tool exchange stays intact. Returns 0 when no clean cut
    exists (pathological all-tool history) so the caller trims nothing.
    """
    start = max(0, len(turns) - keep)
    while start > 0 and turns[start].get("role") == "tool":
        start -= 1
    return start


async def summarise_turns(
    turns: List[Dict],
    summary_model: str,
    ctx: RequestContext,
    max_turns: int = 20,
    max_tokens: int = 150,
) -> str:
    """Summarise old conversation turns using a cheap model.

    ``max_turns`` bounds how much of the span is actually shown to the summariser and
    ``max_tokens`` bounds the summary it writes back. The defaults reproduce G10's original
    behaviour exactly. G26 raises both: its span is a whole conversation rather than a
    sliding window, so a 20-turn view would silently drop everything said in the middle of
    a long thread — and losing mid-conversation facts is precisely the failure the budget
    compaction is supposed to avoid.
    """
    if not turns:
        return ""
    try:
        import litellm
        from providers import get_adapter, get_provider_entry
        from providers.key_resolver import resolve_provider_key, ProviderKeyError
        summary_adapter = get_adapter(summary_model, ctx.config.get("providers", []))
        # BYOK: resolve the summary model's key for THIS tenant (strict denial or no key →
        # skip summarisation gracefully, exactly like the prior missing-key path).
        try:
            provider_key = await resolve_provider_key(
                summary_adapter.name, getattr(ctx, "tenant_id", "default"), ctx
            )
        except ProviderKeyError:
            return "[summary unavailable]"
        if not provider_key and summary_adapter.requires_api_key():
            logger.warning(
                "summarisation: provider key unavailable for %s", summary_adapter.name
            )
            return "[summary unavailable]"

        text = "\n".join(
            f"{m.get('role','')}: {m.get('content','')}" for m in turns[:max(1, max_turns)]
        )
        _call_model, _call_kwargs = summary_adapter.build_call(
            summary_model,
            get_provider_entry(summary_model, ctx.config.get("providers", [])) or {},
            provider_key,
        )
        # This is a real provider call made inside the request pipeline; count its
        # wall-time as LLM (not proxy) time so the SLA latency split stays honest.
        _t0 = time.time()
        _exc = None
        try:
            response = await litellm.acompletion(
                model=_call_model,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Summarise this conversation history in 3-4 compact sentences. "
                            f"Preserve key facts, decisions, and context:\n\n{text}"
                        ),
                    }
                ],
                **_call_kwargs,
                max_tokens=max_tokens,
            )
        except BaseException as e:
            _exc = e
            raise
        finally:
            try:
                ctx.llm_elapsed_ms += (time.time() - _t0) * 1000.0
            except Exception:
                pass
            # Feed the breaker (observation only; review K7 — summary calls are real
            # provider traffic the breaker must see).
            try:
                from providers.resilience import note_provider_outcome
                note_provider_outcome(
                    getattr(summary_adapter, "name", ""), _exc, ctx.config or {}
                )
            except Exception:
                pass
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("summarisation failed: %s", exc)
        return "[summary unavailable]"
