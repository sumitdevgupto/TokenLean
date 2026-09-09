"""
G02 · Prompt Template Registry
Stage: Before the Request
Saving: NONE — G02 is a read-only budget OBSERVER. It reports, it never rewrites.
Technique: At runtime, check that the current request does not exceed the registered
           token budget for a named template (passed via X-Template-ID header / param)
           and WARN when it does. The request is always forwarded unchanged.
           Budget enforcement in CI/CD is handled by scripts/ci/validate-templates.sh;
           runtime protection against an over-budget prompt is G26's job (budget-aware
           context management), which never touches `system` messages.

Features:
  - 30-day deprecation auto-flag for templates approaching EOL
  - Per-version token count history tracking
  - Template metadata registry with versioning
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from middleware import RequestContext
# NOTE: G02 deliberately imports NEITHER `cache_floor` NOR a token counter. It is a
# read-only budget observer: it never edits ctx.messages, so there is no rewrite for the
# prefix-cache floor to re-snapshot and nothing to re-count. Re-adding either import is a
# signal that a content-mutating path is creeping back in — see the note in
# process_request and tests/unit/middleware/test_g02_template_registry.py.

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
    
    async def _load_template_meta(self, template_id: str, prefix: str = "") -> Optional[TemplateMetadata]:
        """Load template metadata from Redis (tenant-prefixed — WS21)."""
        try:
            redis = self._get_redis()
            key = f"{prefix}{_TEMPLATE_META_PREFIX}{template_id}"
            data = await redis.get(key)
            if data:
                return TemplateMetadata.from_dict(json.loads(data))
        except Exception as exc:
            logger.debug("Failed to load template metadata: %s", exc)
        return None
    
    async def _save_template_meta(self, meta: TemplateMetadata, prefix: str = "") -> None:
        """Save template metadata to Redis (tenant-prefixed — WS21: one tenant's
        deprecation/SUNSET of a template id must never block another tenant's)."""
        try:
            redis = self._get_redis()
            key = f"{prefix}{_TEMPLATE_META_PREFIX}{meta.template_id}"
            await redis.set(key, json.dumps(meta.to_dict()))
        except Exception as exc:
            logger.warning("Failed to save template metadata: %s", exc)
    
    async def _record_token_history(
        self, template_id: str, version: str, token_count: int, request_id: str,
        prefix: str = "",
    ) -> None:
        """Record per-version token count history in Redis (tenant-prefixed — WS21)."""
        try:
            redis = self._get_redis()
            key = f"{prefix}{_TEMPLATE_HISTORY_PREFIX}{template_id}:{version}"
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
    
    async def _get_token_history(self, template_id: str, version: str, prefix: str = "") -> List[Dict]:
        """Get token count history for a template version (tenant-prefixed — WS21)."""
        try:
            redis = self._get_redis()
            key = f"{prefix}{_TEMPLATE_HISTORY_PREFIX}{template_id}:{version}"
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
        _rp = getattr(ctx, "redis_prefix", "")
        meta = await self._load_template_meta(template_id, prefix=_rp)
        if meta is None:
            # Auto-create metadata from config if not exists
            version = budget.get("version", "1.0")
            meta = TemplateMetadata(
                template_id=template_id,
                version=version,
                author=budget.get("author", ""),
                description=budget.get("description", ""),
            )
            await self._save_template_meta(meta, prefix=_rp)

        current_tokens = ctx.current_token_count
        max_input = budget.get("total_input_max", 0)
        
        # Record token history for this version
        await self._record_token_history(
            template_id, meta.version, current_tokens, ctx.request_id, prefix=_rp
        )
        
        # Check deprecation status
        status, days_remaining, message = meta.get_deprecation_status()
        if status == "SUNSET":
            logger.error("[%s] G02 %s", ctx.request_id, message)
            # Surfaced to the caller through `_token_opt.metadata.warnings`
            # (g18_observability.py) — that is the whole mechanism. A sunset template is
            # REPORTED, never blocked: the request is served normally.
            #
            # This branch used to say "# Block sunset templates", record the step as
            # "BLOCKED (sunset)" and set `ctx.params["_template_sunset"] = True` "that can be
            # checked by main.py". Nothing anywhere read that flag (W17-R-G02 F4), so the
            # only thing the code blocked was an operator's understanding of it. A claim with
            # no reader is removed, not implemented — blocking a request on a registry state
            # is a product decision nobody has taken.
            ctx.params.setdefault("_token_opt_warnings", []).append(message)
            ctx.savings.add_step(
                GROUP,
                f"Template '{template_id}' SUNSET (reported; request served unchanged)",
                current_tokens,
                current_tokens,
            )
        elif status == "DEPRECATION_WARNING":
            logger.warning("[%s] G02 %s", ctx.request_id, message)
            ctx.params.setdefault("_token_opt_warnings", []).append(message)
        elif status == "DEPRECATED":
            logger.warning("[%s] G02 %s", ctx.request_id, message)
        
        # Budget enforcement is REPORT-ONLY. G02 never edits ctx.messages.
        #
        # Until 2026-09-08 an opt-in `budget.truncate_enabled` path cut the tail off the
        # caller's system prompt until the request fit `total_input_max`, with no
        # faithfulness contract of any kind. Measured on the DS1 enterprise-support
        # workload it removed 691 characters of policy text per request and CHANGED THE
        # ANSWERS on a billed 200: a refund reply stopped naming the disputed amount, an
        # SLA reply stopped naming the breach — while the source text for both facts was
        # still in the prompt it sent. It was deleted rather than guarded, because any
        # faithfulness guard strong enough to prevent that refuses essentially every
        # truncation of a real policy prompt, leaving a lossy path alive for no benefit.
        #
        # Runtime protection against an over-budget prompt belongs to G26 (budget-aware
        # context management), which never touches `system` messages and snaps every cut
        # to a tool-safe boundary. Template budgets are enforced at BUILD time by
        # scripts/ci/validate-templates.sh — you fix an over-budget template, you do not
        # mutilate the request.
        if max_input and current_tokens > max_input:
            logger.warning(
                "[%s] G02 template '%s' budget exceeded: %d > %d tokens "
                "(reported only — the request is sent unchanged)",
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
            history = await self._get_token_history(template_id, meta.version, prefix=_rp)
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
