"""
G09 · Context Schema & Inter-Agent Handoffs
Stage: Into the LLM
Saving: 20–70% on context blocks and cross-agent injection
Technique: Detect and flag prose-heavy inter-agent context blocks.
           Enforce compact typed schema for structured handoff messages.
           Records savings when structured context replaces prose blobs.
           Uses Instructor for typed LLM output at agent boundaries.
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G09"

_DEFAULT_PROSE_KEYWORDS = [
    "customer", "user", "he", "she", "they", "called", "about", "regarding",
    "mentioned", "requested", "explained", "told", "said", "asked",
]
_PROSE_MIN_LENGTH_DEFAULT = 80  # chars below which we don't bother compacting


def _compile_prose_indicators(keywords: List[str]) -> re.Pattern:
    pattern = r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


class G09ContextSchema:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G9_context_schema", {})
        if not cfg.get("enabled", False):
            return ctx

        tokens_before = ctx.current_token_count
        changed = False
        new_messages = []

        # Config
        schema_fields = cfg.get("schema_fields", {})
        use_instructor = cfg.get("use_instructor", True)
        instructor_model = cfg.get("instructor_model", "gpt-4o-mini")
        instructor_timeout_ms = cfg.get("instructor_timeout_ms", 3000)
        instructor_fallback = cfg.get("instructor_fallback_to_heuristic", True)
        prose_keywords = cfg.get("prose_indicators", _DEFAULT_PROSE_KEYWORDS)
        prose_min_length = cfg.get("prose_min_length_chars", _PROSE_MIN_LENGTH_DEFAULT)
        prose_pattern = _compile_prose_indicators(prose_keywords)

        # Get provider key for instructor if needed
        provider_key = None
        if use_instructor and schema_fields:
            try:
                from config_loader import get_provider_model_prefixes
                from providers.key_resolver import resolve_provider_key, ProviderKeyError

                provider_map = get_provider_model_prefixes()
                provider = None
                for fragment, prov in provider_map.items():
                    if fragment in instructor_model.lower():
                        provider = prov
                        break

                if provider:
                    # BYOK: instructor uses the TENANT's key; strict denial or no key → skip
                    # instructor and fall back to the heuristic compactor (existing behaviour).
                    try:
                        provider_key = await resolve_provider_key(
                            provider, getattr(ctx, "tenant_id", "default"), ctx
                        )
                    except ProviderKeyError:
                        provider_key = None
            except Exception as exc:
                logger.warning("G09 failed to get provider key for instructor: %s", exc)

        primary_system_seen = False
        for msg in ctx.messages:
            if msg.get("role") != "system":
                new_messages.append(msg)
                continue

            # Hard guard (quality is non-negotiable): never rewrite the primary
            # (first) system message — it is the developer's instruction/contract.
            # G09's lossy prose->schema compaction only ever applies to SUBSEQUENT
            # system context blocks (e.g. inter-agent handoff), never the main prompt.
            if not primary_system_seen:
                primary_system_seen = True
                new_messages.append(msg)
                continue

            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) < prose_min_length:
                new_messages.append(msg)
                continue

            # Detect JSON-encodable structured blocks already present
            if content.strip().startswith("{") or content.strip().startswith("["):
                new_messages.append(msg)
                continue

            # Detect prose-heavy context blocks
            if prose_pattern.search(content):
                compacted = None
                # Try schema-based compaction first if enabled
                if use_instructor and schema_fields and provider_key:
                    _t0 = time.time()
                    try:
                        compacted = await asyncio.wait_for(
                            _compact_with_schema(
                                content, schema_fields, instructor_model, provider_key
                            ),
                            timeout=instructor_timeout_ms / 1000.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[%s] G09 Instructor timed out after %dms — falling back to heuristic",
                            ctx.request_id, instructor_timeout_ms,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[%s] G09 Instructor compaction failed: %s",
                            ctx.request_id, exc,
                        )
                    finally:
                        # Real provider call inside the pipeline — attribute its
                        # wall-time to LLM (not proxy) time in the SLA split.
                        try:
                            ctx.llm_elapsed_ms += (time.time() - _t0) * 1000.0
                        except Exception:
                            pass

                    if compacted and len(compacted) < len(content) * 0.7:
                        new_messages.append({**msg, "content": compacted})
                        changed = True
                        logger.debug(
                            "[%s] G09 compacted prose with schema: %d → %d chars",
                            ctx.request_id,
                            len(content),
                            len(compacted),
                        )
                        continue

                    # Instructor returned nothing useful — attempt heuristic fallback
                    if instructor_fallback:
                        compacted = _try_compact_prose(content)

                # If Instructor wasn't used or fallback kicked in, use heuristic directly
                if not compacted:
                    compacted = _try_compact_prose(content)

                if compacted and len(compacted) < len(content) * 0.7:
                    new_messages.append({**msg, "content": compacted})
                    changed = True
                    logger.debug(
                        "[%s] G09 compacted prose block: %d → %d chars",
                        ctx.request_id,
                        len(content),
                        len(compacted),
                    )
                    continue

            new_messages.append(msg)

        if changed:
            from savings.calculator import count_messages_tokens
            ctx.messages = new_messages
            tokens_after = count_messages_tokens(ctx.messages, ctx.model)
            ctx.savings.add_step(
                GROUP,
                "Prose context block → compact typed schema",
                tokens_before,
                tokens_after,
            )

        return ctx


def _try_compact_prose(text: str) -> Optional[str]:
    """
    Attempt to extract key-value pairs from prose context and render as
    compact pipe-delimited TOON-style schema (per G09/G13 playbook).
    Returns None if extraction is not confident.
    """
    # Simple heuristic: extract entity=value patterns from known fields
    patterns = {
        "cust": re.compile(r"customer\s+([A-Z][a-zA-Z\s]+?)(?:\s+called|\s+about|\s+placed|,)", re.I),
        "order": re.compile(r"order\s+(?:#|id\s+)?(\w+)", re.I),
        "status": re.compile(r"\b(shipped|pending|delivered|cancelled|processing)\b", re.I),
        "action": re.compile(r"(?:requested|needs|wants)\s+([\w\s]+?)(?:\.|,|$)", re.I),
    }
    extracted = {}
    for key, pattern in patterns.items():
        m = pattern.search(text)
        if m:
            extracted[key] = m.group(1).strip().replace(" ", "_")

    if len(extracted) >= 2:
        return "|".join(f"{k}={v}" for k, v in extracted.items())
    return None


async def _compact_with_schema(
    text: str, schema: Dict[str, str], model: str, provider_key: str
) -> Optional[str]:
    """
    Use Instructor with a schema to extract structured data from prose.
    schema: {field_name: "description of what to extract"}
    Returns pipe-delimited compact string or None on failure.
    """
    try:
        import instructor
        import litellm
        from pydantic import create_model

        # Create a dynamic Pydantic model from schema keys
        DynamicModel = create_model(
            "DynamicModel",
            **{name: (str, ...) for name in schema.keys()}
        )

        # instructor.from_litellm() may patch attributes onto the callable it
        # is given. Pass a fresh per-call wrapper rather than the shared
        # litellm.acompletion module function, so concurrent requests can't
        # clobber each other's patched state on the same global object.
        async def _acompletion(*args, **kwargs):
            return await litellm.acompletion(*args, **kwargs)

        client = instructor.from_litellm(_acompletion)

        # Route via the adapter so non-OpenAI / custom-base-URL instructor models work.
        from providers import build_litellm_call
        from config_loader import get_providers
        _ix_model, _ix_kwargs = build_litellm_call(model, get_providers(), provider_key)

        # Extract structured data using Instructor's create pattern
        result = await client.chat.completions.create(
            model=_ix_model,
            **_ix_kwargs,
            response_model=DynamicModel,
            messages=[{"role": "user", "content": f"Extract from this text:\n\n{text}"}],
            max_tokens=256,
            temperature=0.0,
        )
        
        # Convert to compact pipe-delimited format
        extracted = result.model_dump()
        if extracted and len(extracted) >= 2:
            return "|".join(f"{k}={v}" for k, v in extracted.items() if v)
        return None
        
    except Exception as exc:
        logger.warning("G09 schema-based compaction failed: %s", exc)
        return None
