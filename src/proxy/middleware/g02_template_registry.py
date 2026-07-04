"""
G02 · Prompt Template Registry
Stage: Before the Request
Saving: Structural — prevents fleet-wide token budget overruns.
Technique: At runtime, check that the current request does not exceed the registered
           token budget for a named template (passed via X-Template-ID header / param).
           Budget enforcement in CI/CD is handled by scripts/ci/validate-templates.sh.
           
Features:
  - 30-day deprecation auto-flag for templates approaching EOL
  - Per-version token count history tracking
  - Template metadata registry with versioning
"""
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from middleware import RequestContext
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G02"

# Redis key prefixes for template registry
_TEMPLATE_META_PREFIX = "tok_opt:template:meta:"
_TEMPLATE_HISTORY_PREFIX = "tok_opt:template:history:"
import os as _os
_DEPRECATION_WARNING_DAYS = int(_os.getenv("TEMPLATE_DEPRECATION_WARN_DAYS", "30"))
_TEMPLATE_HISTORY_TTL_SECONDS = int(_os.getenv("TEMPLATE_HISTORY_TTL_DAYS", "90")) * 86400
_TEMPLATE_MAX_HISTORY = int(_os.getenv("TEMPLATE_MAX_HISTORY_PER_VERSION", "1000"))


# ── Config-first knob resolution (item 83a) ───────────────────────────────────
# Env-derived constants above are the *fallback defaults*; values under
# `groups.G2_template_registry.*` in the hot-reloaded proxy config win. Keeps
# existing TEMPLATE_* env deployments working while making the documented config
# keys take effect. Resolved per-use so config hot-reload applies.
def _g2_cfg() -> Dict[str, Any]:
    try:
        from config_loader import get_proxy_config
        return get_proxy_config().get("groups", {}).get("G2_template_registry", {}) or {}
    except Exception:
        return {}

def _deprecation_warn_days() -> int:
    return int(_g2_cfg().get("deprecation_warn_days", _DEPRECATION_WARNING_DAYS))

def _template_history_ttl_seconds() -> int:
    return int(_g2_cfg().get("template_history_ttl_days", _TEMPLATE_HISTORY_TTL_SECONDS // 86400)) * 86400

def _template_max_history() -> int:
    return int(_g2_cfg().get("max_history_per_version", _TEMPLATE_MAX_HISTORY))


def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


class TemplateMetadata:
    """Template metadata with versioning and deprecation tracking."""
    
    def __init__(
        self,
        template_id: str,
        version: str = "1.0",
        created_at: Optional[float] = None,
        deprecated_at: Optional[float] = None,
        sunset_at: Optional[float] = None,
        replaced_by: Optional[str] = None,
        author: str = "",
        description: str = "",
    ):
        self.template_id = template_id
        self.version = version
        self.created_at = created_at or time.time()
        self.deprecated_at = deprecated_at
        self.sunset_at = sunset_at
        self.replaced_by = replaced_by
        self.author = author
        self.description = description
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "created_at": self.created_at,
            "deprecated_at": self.deprecated_at,
            "sunset_at": self.sunset_at,
            "replaced_by": self.replaced_by,
            "author": self.author,
            "description": self.description,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateMetadata":
        return cls(
            template_id=data.get("template_id", ""),
            version=data.get("version", "1.0"),
            created_at=data.get("created_at"),
            deprecated_at=data.get("deprecated_at"),
            sunset_at=data.get("sunset_at"),
            replaced_by=data.get("replaced_by"),
            author=data.get("author", ""),
            description=data.get("description", ""),
        )
    
    def get_deprecation_status(self) -> tuple:
        """Returns (status, days_remaining, message) for deprecation."""
        now = time.time()
        
        if self.sunset_at and now > self.sunset_at:
            return ("SUNSET", 0, f"Template {self.template_id} has been sunset and is no longer supported")
        
        if self.deprecated_at and self.sunset_at:
            days_remaining = (self.sunset_at - now) / 86400
            if days_remaining <= _deprecation_warn_days():
                if self.replaced_by:
                    return ("DEPRECATION_WARNING", days_remaining, 
                            f"Template {self.template_id} deprecated. Migrate to {self.replaced_by} ({days_remaining:.0f} days remaining)")
                return ("DEPRECATION_WARNING", days_remaining,
                        f"Template {self.template_id} deprecated ({days_remaining:.0f} days remaining)")
        
        if self.deprecated_at:
            return ("DEPRECATED", -1, f"Template {self.template_id} is deprecated")
        
        return ("ACTIVE", -1, f"Template {self.template_id} is active")


class G02TemplateRegistry:
    """Template registry with budget enforcement, deprecation tracking, and token history."""
    
    def __init__(self):
        self._redis = None
    
    def _get_redis(self):
        if self._redis is None:
            self._redis = _get_redis()
        return self._redis
    
    async def _load_template_meta(self, template_id: str) -> Optional[TemplateMetadata]:
        """Load template metadata from Redis."""
        try:
            redis = self._get_redis()
            key = f"{_TEMPLATE_META_PREFIX}{template_id}"
            data = await redis.get(key)
            if data:
                return TemplateMetadata.from_dict(json.loads(data))
        except Exception as exc:
            logger.debug("Failed to load template metadata: %s", exc)
        return None
    
    async def _save_template_meta(self, meta: TemplateMetadata) -> None:
        """Save template metadata to Redis."""
        try:
            redis = self._get_redis()
            key = f"{_TEMPLATE_META_PREFIX}{meta.template_id}"
            await redis.set(key, json.dumps(meta.to_dict()))
        except Exception as exc:
            logger.warning("Failed to save template metadata: %s", exc)
    
    async def _record_token_history(
        self, template_id: str, version: str, token_count: int, request_id: str
    ) -> None:
        """Record per-version token count history in Redis."""
        try:
            redis = self._get_redis()
            key = f"{_TEMPLATE_HISTORY_PREFIX}{template_id}:{version}"
            entry = {
                "timestamp": time.time(),
                "tokens": token_count,
                "request_id": request_id,
            }
            # Store in sorted set by timestamp, keep last 1000 entries per version
            await redis.zadd(key, {json.dumps(entry): entry["timestamp"]})
            await redis.expire(key, _template_history_ttl_seconds())
            # Trim to max history entries per version
            count = await redis.zcard(key)
            if count > _template_max_history():
                await redis.zremrangebyrank(key, 0, count - _template_max_history() - 1)
        except Exception as exc:
            logger.debug("Failed to record token history: %s", exc)
    
    async def _get_token_history(self, template_id: str, version: str) -> List[Dict]:
        """Get token count history for a template version."""
        try:
            redis = self._get_redis()
            key = f"{_TEMPLATE_HISTORY_PREFIX}{template_id}:{version}"
            entries = await redis.zrevrange(key, 0, 99)  # Last 100 entries
            return [json.loads(e) for e in entries]
        except Exception as exc:
            logger.debug("Failed to get token history: %s", exc)
            return []
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G2_template_registry", {})
        if not cfg.get("enabled", False):
            return ctx

        template_id = ctx.params.get("template_id") or ctx.params.get("x_template_id")
        if not template_id:
            return ctx

        registry: dict = cfg.get("budgets", {})
        budget = registry.get(template_id)
        if not budget:
            return ctx

        # Load or create template metadata
        meta = await self._load_template_meta(template_id)
        if meta is None:
            # Auto-create metadata from config if not exists
            version = budget.get("version", "1.0")
            meta = TemplateMetadata(
                template_id=template_id,
                version=version,
                author=budget.get("author", ""),
                description=budget.get("description", ""),
            )
            await self._save_template_meta(meta)

        current_tokens = ctx.current_token_count
        max_input = budget.get("total_input_max", 0)
        
        # Record token history for this version
        await self._record_token_history(
            template_id, meta.version, current_tokens, ctx.request_id
        )
        
        # Check deprecation status
        status, days_remaining, message = meta.get_deprecation_status()
        if status == "SUNSET":
            logger.error("[%s] G02 %s", ctx.request_id, message)
            ctx.params.setdefault("_token_opt_warnings", []).append(message)
            # Block sunset templates
            ctx.savings.add_step(
                GROUP,
                f"Template '{template_id}' BLOCKED (sunset)",
                current_tokens,
                current_tokens,
            )
            # Set flag that can be checked by main.py
            ctx.params["_template_sunset"] = True
        elif status == "DEPRECATION_WARNING":
            logger.warning("[%s] G02 %s", ctx.request_id, message)
            ctx.params.setdefault("_token_opt_warnings", []).append(message)
        elif status == "DEPRECATED":
            logger.warning("[%s] G02 %s", ctx.request_id, message)
        
        # Budget enforcement with optional truncation (Phase 2 implementation)
        if max_input and current_tokens > max_input:
            budget_cfg = ctx.config.get("groups", {}).get("G2_template_registry", {}).get("budget", {})
            truncate_enabled = budget_cfg.get("truncate_enabled", False)
            
            if truncate_enabled:
                # Perform actual truncation to meet budget
                tokens_before = current_tokens
                strategy = budget_cfg.get("truncate_strategy", "tail_system")
                min_keep_user = budget_cfg.get("min_keep_user_turns", 1)
                
                ctx.messages = self._truncate_messages(
                    ctx.messages, max_input, strategy, min_keep_user, ctx.model
                )
                tokens_after = count_messages_tokens(ctx.messages, ctx.model)
                
                logger.warning(
                    "[%s] G02 template '%s' truncated: %d -> %d tokens (strategy=%s)",
                    ctx.request_id, template_id, tokens_before, tokens_after, strategy
                )
                ctx.savings.add_step(
                    GROUP,
                    f"Template '{template_id}' truncated to budget ({strategy})",
                    tokens_before,
                    tokens_after,
                )
            else:
                logger.warning(
                    "[%s] G02 template '%s' budget exceeded: %d > %d tokens",
                    ctx.request_id, template_id, current_tokens, max_input,
                )
                ctx.savings.add_step(
                    GROUP,
                    f"Template '{template_id}' budget check (OVER by {current_tokens - max_input}t)",
                    current_tokens,
                    current_tokens,
                )
        else:
            # Get token history stats for insights
            history = await self._get_token_history(template_id, meta.version)
            if history:
                avg_tokens = sum(h["tokens"] for h in history) / len(history)
                ctx.savings.add_step(
                    GROUP,
                    f"Template '{template_id}' v{meta.version} OK (avg={avg_tokens:.0f}t, n={len(history)})",
                    current_tokens,
                    current_tokens,
                )
            else:
                ctx.savings.add_step(
                    GROUP,
                    f"Template '{template_id}' v{meta.version} budget check (OK)",
                    current_tokens,
                    current_tokens,
                )

        return ctx

    def _truncate_messages(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        strategy: str,
        min_keep_user: int,
        model: str,
    ) -> List[Dict[str, Any]]:
        """Truncate messages to fit within max_tokens budget.
        
        Strategy 'tail_system': Trim from the end of system prompts first,
        preserving the last N user turns.
        """
        import copy
        result = copy.deepcopy(messages)
        
        # Count current tokens
        current = count_messages_tokens(result, model)
        if current <= max_tokens:
            return result
        
        if strategy == "tail_system":
            # Strategy: trim system prompts from the tail, keep last N user turns
            user_turns = sum(1 for m in result if m.get("role") == "user")
            
            # First pass: try to trim system message content from the end
            for i in range(len(result) - 1, -1, -1):
                if current <= max_tokens:
                    break
                msg = result[i]
                if msg.get("role") != "system":
                    continue
                    
                content = msg.get("content", "")
                if not isinstance(content, str) or len(content) < 100:
                    continue
                    
                # Gradually trim from the end (remove last sentences/paragraphs)
                while len(content) > 200 and current > max_tokens:
                    # Find last paragraph break or sentence break
                    last_para = content.rfind("\n\n")
                    last_sent = content.rfind(". ")
                    cut = max(last_para, last_sent)
                    if cut < len(content) // 2:
                        cut = len(content) - max(100, (current - max_tokens) * 4)
                    if cut < 50:
                        break
                    content = content[:cut].rstrip() + "."
                    msg["content"] = content
                    current = count_messages_tokens(result, model)
            
            # Second pass: if still over budget, drop non-essential system messages entirely
            # but preserve at least one system message and min_keep_user user turns
            for i in range(len(result) - 1, -1, -1):
                if current <= max_tokens:
                    break
                msg = result[i]
                if msg.get("role") == "system" and len([m for m in result if m.get("role") == "system"]) > 1:
                    removed_content = msg.get("content", "")
                    msg["content"] = ""  # Empty but keep the message structure
                    current = count_messages_tokens(result, model)
        
        return result
