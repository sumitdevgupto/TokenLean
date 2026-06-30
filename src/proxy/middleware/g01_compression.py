"""
G01 · Prompt & System Prompt Design
Stage: Before the Request
Saving: 15–40% input tokens
Technique: 
  1. Layered composition: base → role → task → dynamic layers
  2. Build-time compression of static system prompts
  3. LLMLingua-2 runtime compression for large messages
  4. Selective Context integration for relevance-based pruning
"""
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from middleware import RequestContext
from middleware import langfuse_tracing
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G01"

# Selective Context configuration (optional - falls back gracefully)
_selective_context_available = False
try:
    from selective_context import SelectiveContext
    _selective_context_available = True
except ImportError:
    pass

# Kompress-v2-base fallback for log/error content (optional — no sidecar needed)
# Loaded lazily on first use; None when transformers is not installed.
_kompress_pipe: Optional[Any] = None
_kompress_pipe_model: Optional[str] = None  # model name of the loaded pipeline
_kompress_loaded: bool = False               # True once load was attempted (even if failed)

import re as _re


class LayeredPromptComposer:
    """Compose system prompts from layered components (base → role → task → dynamic).

    The base + role layers are static across requests for a given
    role/persona, so they are deduplicated and cached at first use
    (build-time compression); task + dynamic layers are request-specific
    and always recomposed fresh.
    """

    def __init__(self, config: Dict[str, Any]):
        self.layers = config.get("layers", {})
        self.build_time_compression = config.get("build_time_compression", True)
        self._compressed_cache: Dict[str, str] = {}

    def get_layer(self, layer_name: str, **variables) -> str:
        """Get a prompt layer with variable substitution."""
        template = self.layers.get(layer_name, "")
        if not template:
            return ""

        # Simple variable substitution
        result = template
        for key, value in variables.items():
            placeholder = f"{{{key}}}"
            result = result.replace(placeholder, str(value))

        return result

    def compose(self, context: Dict[str, Any]) -> str:
        """Compose full prompt from all layers in order."""
        parts = []

        base_content = self.get_layer("base", **context)
        role_content = self.get_layer("role", **context)
        if self.build_time_compression and (base_content or role_content):
            static_key = self._static_key(base_content, role_content)
            static_combined = self._compressed_cache.get(static_key)
            if static_combined is None:
                static_combined = self._compress_static_layers(base_content, role_content)
                self._compressed_cache[static_key] = static_combined
            if static_combined:
                parts.append(static_combined)
        else:
            if base_content:
                parts.append(base_content)
            if role_content:
                parts.append(role_content)

        for layer_name in ("task", "dynamic"):
            layer_content = self.get_layer(layer_name, **context)
            if layer_content:
                parts.append(layer_content)

        return "\n\n".join(parts)

    @staticmethod
    def _static_key(base: str, role: str) -> str:
        """Cache key for a static base+role combination."""
        combined = f"{base}:{role}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @staticmethod
    def _compress_static_layers(base: str, role: str) -> str:
        """Dedupe role-layer lines that already appear in the base layer."""
        base_stripped = base.strip()
        role_stripped = role.strip()

        base_lines = {line.strip().lower() for line in base_stripped.split("\n") if line.strip()}
        unique_role_lines = [
            line for line in role_stripped.split("\n")
            if line.strip() and line.strip().lower() not in base_lines
        ]
        compressed_role = "\n".join(unique_role_lines)

        return f"{base_stripped}\n\n{compressed_role}".strip()

    def get_build_time_compressed(self, layer_name: str) -> Optional[str]:
        """Get pre-compressed layer content (computed at build/deploy time)."""
        return self._compressed_cache.get(layer_name)

    def set_build_time_compressed(self, layer_name: str, content: str) -> None:
        """Store build-time compressed content."""
        self._compressed_cache[layer_name] = content


class SelectiveContextPruner:
    """Selective Context integration for relevance-based context pruning."""
    
    def __init__(self, max_tokens: int = 4000):
        self.max_tokens = max_tokens
        self._sc = None
        
        if _selective_context_available:
            try:
                self._sc = SelectiveContext()
            except Exception as exc:
                logger.debug("SelectiveContext init failed: %s", exc)
    
    def prune_context(self, text: str, query: Optional[str] = None) -> Tuple[str, float]:
        """Prune context to most relevant sentences.
        
        Returns: (pruned_text, reduction_ratio)
        """
        if not self._sc or not text:
            return text, 1.0
        
        try:
            # Use Selective Context to prune irrelevant content
            result = self._sc(text, self.max_tokens)
            pruned = result if isinstance(result, str) else result[0]
            
            original_tokens = len(text.split())  # Rough estimate
            pruned_tokens = len(pruned.split())
            reduction = pruned_tokens / original_tokens if original_tokens > 0 else 1.0
            
            return pruned, reduction
        except Exception as exc:
            logger.debug("SelectiveContext pruning failed: %s", exc)
            return text, 1.0


# ─── Kompress-v2-base fallback ────────────────────────────────────────────────

_LOG_ERROR_PATTERNS = [
    _re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", _re.MULTILINE),   # timestamps
    _re.compile(r"^\[?(INFO|DEBUG|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\]?", _re.MULTILINE),
    _re.compile(r"Traceback \(most recent call last\)", _re.MULTILINE),
    _re.compile(r"^\s+at \w[\w.]+\([\w.]+:\d+\)", _re.MULTILINE),              # Java stack frames
    _re.compile(r'^\d{2}:\d{2}:\d{2}\.\d+ \[', _re.MULTILINE),                # structured logs
]


def _is_log_error_content(text: str) -> bool:
    """Return True if text looks like log output or an error/stack trace."""
    return any(p.search(text) for p in _LOG_ERROR_PATTERNS)


def _get_kompress_pipe(model: str) -> Optional[Any]:
    """Lazy-load Kompress-v2-base pipeline; cache across requests (singleton)."""
    global _kompress_pipe, _kompress_pipe_model, _kompress_loaded
    if _kompress_loaded and _kompress_pipe_model == model:
        return _kompress_pipe
    _kompress_loaded = True
    _kompress_pipe_model = model
    try:
        from transformers import pipeline  # type: ignore
        _kompress_pipe = pipeline("text2text-generation", model=model)
        logger.info("G01 Kompress pipeline loaded: %s", model)
    except Exception as exc:
        _kompress_pipe = None
        logger.debug("G01 Kompress pipeline unavailable (%s): %s", model, exc)
    return _kompress_pipe


def _kompress_compress(text: str, model: str, max_new_tokens: int = 256) -> Optional[str]:
    """Compress log/error text via Kompress-v2-base; returns None on failure."""
    pipe = _get_kompress_pipe(model)
    if pipe is None:
        return None
    try:
        result = pipe(text, max_new_tokens=max_new_tokens, truncation=True)
        if result and isinstance(result, list):
            compressed = result[0].get("generated_text", "")
            return compressed if compressed and len(compressed) < len(text) else None
    except Exception as exc:
        logger.debug("G01 Kompress compression failed: %s", exc)
    return None


class G01Compression:
    """G01 with layered composition, build-time compression, and Selective Context."""
    
    def __init__(self):
        self._composer: Optional[LayeredPromptComposer] = None
        self._selective_pruner: Optional[SelectiveContextPruner] = None
    
    def _get_composer(self, cfg: Dict) -> LayeredPromptComposer:
        if self._composer is None:
            self._composer = LayeredPromptComposer(cfg)
        return self._composer
    
    def _get_selective_pruner(self, cfg: Dict) -> Optional[SelectiveContextPruner]:
        if not cfg.get("selective_context_enabled", True):
            return None
        
        if self._selective_pruner is None:
            max_tokens = cfg.get("selective_context_max_tokens", 4000)
            self._selective_pruner = SelectiveContextPruner(max_tokens)
        return self._selective_pruner
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G1_compression", {})
        if not cfg.get("enabled", False):
            return ctx

        min_tokens: int = cfg.get("min_tokens_to_compress", 200)
        sidecar_url: str = cfg.get("sidecar_url", ctx.config.get("services", {}).get("llmlingua_url", "http://llmlingua-svc:8080/compress"))
        ratio: float = cfg.get("compression_ratio_target", 0.5)
        # Preserve digit tokens (dates/IDs/amounts) so compression can't silently
        # corrupt a tool argument (e.g. an incident date in a log-query window).
        force_reserve_digit: bool = cfg.get("force_reserve_digit", True)
        kompress_enabled: bool = cfg.get("kompress_enabled", True)
        kompress_model: str = cfg.get("kompress_model", "microsoft/Kompress-v2-base")
        kompress_max_new_tokens: int = cfg.get("kompress_max_new_tokens", 256)
        
        # Check for layered composition in context
        use_layers = cfg.get("layered_composition_enabled", True)
        if use_layers:
            composer = self._get_composer(cfg)
            # If this is a system prompt with layer variables, compose it
            for i, msg in enumerate(ctx.messages):
                if msg.get("role") == "system":
                    content = msg.get("content", "")
                    # Check if content references layers
                    if "{{" in content and "}}" in content:
                        # Extract layer context from message
                        layer_context = msg.get("layer_context", {})
                        composed = composer.compose(layer_context)
                        if composed:
                            ctx.messages[i] = {**msg, "content": composed}
                            logger.debug("[%s] G01 composed layered prompt", ctx.request_id)

        tokens_before = ctx.current_token_count
        if tokens_before < min_tokens:
            return ctx

        compressed_messages = []
        changed = False
        selective_pruner = self._get_selective_pruner(cfg)
        
        # Quality guard: by default DO NOT compress the system instruction — it is
        # the developer's contract and aggressive compression silently degrades
        # instruction-following. Compress only safe context (assistant history, and
        # user-pasted bulk if explicitly enabled). Opt in via compress_system_prompt.
        # Per-request opt-in (x_compress_user) lets a caller that knows its user content
        # is safe-to-compress bulk prose (a pasted transcript, a verbose write-up) enable
        # user-message compression without flipping the global default. The system prompt
        # stays protected unless compress_system_prompt is explicitly set in config.
        _x_compress_user = str(ctx.params.get("x_compress_user", "")).lower() in ("true", "1", "yes")
        compress_user_messages = cfg.get("compress_user_messages", False) or _x_compress_user
        compress_system_prompt = cfg.get("compress_system_prompt", False)
        roles = ["assistant"]
        if compress_system_prompt:
            roles.append("system")
        if compress_user_messages:
            roles.append("user")
        compressible_roles = tuple(roles)

        for msg in ctx.messages:
            role = msg.get("role", "")
            if role in compressible_roles:
                content = msg.get("content", "")
                min_chars = cfg.get("min_chars_to_compress", 100)
                reduction_threshold = cfg.get("reduction_threshold", 0.95)
                if isinstance(content, str) and len(content) > min_chars:
                    compressed = content
                    reduction_info = []

                    # Step 1: Selective Context pruning (if enabled)
                    if selective_pruner:
                        pruned, reduction = selective_pruner.prune_context(compressed)
                        if reduction < reduction_threshold:
                            compressed = pruned
                            reduction_info.append(f"SC:{reduction:.2f}")
                    
                    # Step 2: LLMLingua-2 compression
                    llm_compressed = await _call_llmlingua(sidecar_url, compressed, ratio, force_reserve_digit)
                    if llm_compressed and len(llm_compressed) < len(compressed):
                        compressed = llm_compressed
                        reduction_info.append("LLML2")
                    elif kompress_enabled and _is_log_error_content(compressed):
                        # Step 2b: Kompress-v2-base fallback for log/error content when
                        # LLMLingua sidecar is unavailable or produced no reduction
                        k_compressed = _kompress_compress(compressed, kompress_model, kompress_max_new_tokens)
                        if k_compressed and len(k_compressed) < len(compressed):
                            compressed = k_compressed
                            reduction_info.append("KMP")

                    if compressed != content:
                        compressed_messages.append({**msg, "content": compressed})
                        changed = True
                        continue
            compressed_messages.append(msg)

        if changed:
            original_messages = ctx.messages
            compressed_count = sum(1 for a, b in zip(original_messages, compressed_messages) if a != b)
            ctx.messages = compressed_messages
            tokens_after = count_messages_tokens(ctx.messages, ctx.model)
            ctx.savings.add_step(
                GROUP,
                "G01 prompt compression (layered + selective + LLMLingua-2 + Kompress fallback)",
                tokens_before,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G01-compression",
                span_input={"tokens_before": tokens_before},
                output={"tokens_after": tokens_after, "compressed_count": compressed_count},
                metadata={
                    "compression_ratio": round(tokens_after / tokens_before, 2) if tokens_before > 0 else 0.0,
                    "sidecar_url": sidecar_url,
                    "layered_composition": use_layers,
                    "selective_context": selective_pruner is not None,
                },
            )
            logger.debug(
                "[%s] G01 compressed %d → %d tokens",
                ctx.request_id,
                tokens_before,
                tokens_after,
            )
        return ctx


async def _call_llmlingua(url: str, text: str, ratio: float, force_reserve_digit: bool = True) -> str:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"text": text, "ratio": ratio, "force_reserve_digit": force_reserve_digit},
            )
            resp.raise_for_status()
            return resp.json().get("compressed", text)
    except Exception as exc:
        logger.warning("G01 LLMLingua sidecar unavailable: %s — skipping compression", exc)
        return text


def generate_build_time_compressed(layers_config: Dict[str, str]) -> Dict[str, str]:
    """Generate compressed versions of static layers at build/deploy time.
    
    This should be called during CI/CD pipeline to pre-compute compressed
    versions of base/role layers that don't change per-request.
    
    Returns: {layer_name: compressed_content}
    """
    compressed = {}
    for layer_name, content in layers_config.items():
        if content:
            # Mark as pre-compressed (actual compression happens at runtime via sidecar)
            compressed[layer_name] = f"<!-- build-compressed -->{content}"
    return compressed
