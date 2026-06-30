"""G20 — Runtime prompt optimiser.

Loads pre-computed DSPy/Opik optimised system-prompt templates from Redis at
startup (keys: ``{prefix}tok_opt:g20:tpl:{sha256_prefix}``).  On each request
the system prompt is fingerprinted (SHA-256 of the first 512 chars); when a
match is found the system message is swapped to the optimised version.  Falls
back to the original prompt on any error or cache miss.

Reference: G20 in token_optimization_playbook_v7.md
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SHA_PREFIX_LEN = 16  # first 16 hex chars used as fingerprint key


def _fp(text: str) -> str:
    """Return short SHA-256 fingerprint of text (first _SHA_PREFIX_LEN chars)."""
    return hashlib.sha256(text[:512].encode("utf-8")).hexdigest()[:_SHA_PREFIX_LEN]


class G20PromptOptimizer:
    """
    Runtime prompt optimisation: swap system prompts to pre-computed DSPy templates.
    Reference: G20 in token_optimization_playbook_v7.md
    """

    def __init__(self, redis_client: Optional[Any] = None):
        self._redis = redis_client

    def _cfg(self, ctx: Any) -> Dict[str, Any]:
        return ctx.config.get("groups", {}).get("g20_prompt_optimizer", {})

    def _tpl_key(self, prefix: str, fp: str) -> str:
        return f"{prefix}tok_opt:g20:tpl:{fp}"

    async def _load_template(self, ctx: Any, fp: str) -> Optional[str]:
        """Fetch optimised template from Redis; returns None on miss or error."""
        if self._redis is None:
            return None
        ns = getattr(ctx, "redis_prefix", "")
        key = self._tpl_key(ns, fp)
        try:
            val = await self._redis.get(key)
            if val is None:
                return None
            return val.decode("utf-8") if isinstance(val, bytes) else val
        except Exception as exc:
            logger.debug("G20: Redis fetch error for key %s: %s", key, exc)
            return None

    async def process_request(self, ctx: Any) -> Any:
        cfg = self._cfg(ctx)
        if not cfg.get("enabled", False):
            return ctx

        messages: List[Dict] = ctx.messages
        system_msgs = [i for i, m in enumerate(messages) if m.get("role") == "system"]
        if not system_msgs:
            return ctx

        idx = system_msgs[0]
        original_content = messages[idx].get("content", "")
        if not original_content:
            return ctx

        fp = _fp(original_content)
        optimised = await self._load_template(ctx, fp)
        if optimised is None:
            logger.debug("G20: no template for fingerprint %s — keeping original", fp)
            return ctx

        original_tokens = len(original_content.split())
        optimised_tokens = len(optimised.split())

        messages[idx] = dict(messages[idx])
        messages[idx]["content"] = optimised
        ctx.messages = messages

        ctx.savings.add_step(
            group="G20",
            description=f"G20: swapped system prompt (fp={fp})",
            tokens_before=original_tokens,
            tokens_after=optimised_tokens,
        )
        tokens_saved = max(0, original_tokens - optimised_tokens)
        logger.debug("G20: optimised system prompt fp=%s saved=%d tokens", fp, tokens_saved)
        return ctx

    async def process_response(self, ctx: Any, response: Dict) -> Dict:
        return response
