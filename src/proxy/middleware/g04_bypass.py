"""
G04 · Rules-Based Bypass
Stage: At the Gate
Saving: 30–90% fewer LLM calls
Technique: Match request against configurable intent rules (regex + keyword).
           If matched, call a backend API directly and return without invoking the LLM.
           Zero tokens spent for matched requests.
           
Features:
  - Database-first resolution: Rules stored in PostgreSQL for dynamic updates
  - Confidence scoring: ML-based confidence in bypass match quality
  - Rule effectiveness tracking: Hit rates per rule for optimization
"""
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from middleware import RequestContext
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G04"

# Redis key prefix for rule stats
_BYPASS_STATS_PREFIX = "tok_opt:bypass:stats:"


class BypassRule:
    def __init__(self, rule: Dict[str, Any], default_confidence: float = 0.70,
                 keyword_weight: float = 0.4, pattern_weight: float = 0.6) -> None:
        self.name: str = rule.get("name", "")
        self.patterns: List[re.Pattern] = [
            re.compile(p, re.IGNORECASE) for p in rule.get("patterns", [])
        ]
        self.keywords: List[str] = [k.lower() for k in rule.get("keywords", [])]
        self.backend_url: Optional[str] = rule.get("backend_url")
        self.static_response: Optional[str] = rule.get("static_response")
        self.confidence_threshold: float = rule.get("confidence_threshold", default_confidence)
        self.rule_id: str = rule.get("rule_id", self.name)
        self.category: str = rule.get("category", "general")
        self._keyword_weight = keyword_weight
        self._pattern_weight = pattern_weight

    def matches(self, text: str) -> Tuple[bool, float]:
        """Returns (matches, confidence_score)."""
        text_lower = text.lower()
        
        # Keyword matching
        keyword_hits = sum(1 for k in self.keywords if k in text_lower)
        keyword_confidence = keyword_hits / len(self.keywords) if self.keywords else 0.0
        
        # Pattern matching
        pattern_hits = sum(1 for p in self.patterns if p.search(text))
        pattern_confidence = pattern_hits / len(self.patterns) if self.patterns else 0.0
        
        # Combined confidence: weighted average
        if self.keywords and self.patterns:
            confidence = (keyword_confidence * self._keyword_weight) + (pattern_confidence * self._pattern_weight)
        elif self.keywords:
            confidence = keyword_confidence
        elif self.patterns:
            confidence = pattern_confidence
        else:
            return False, 0.0
        
        matches = confidence >= self.confidence_threshold
        return matches, confidence


def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


async def _load_rules_from_db(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load bypass rules from PostgreSQL database for dynamic updates.

    Rows with tenant_id IS NULL are global rules that apply to every tenant;
    rows with a tenant_id only apply to that tenant. Without this filter a
    rule created for one tenant would bypass the LLM call for every other
    tenant too — a cross-tenant policy leak, not just a cache leak.
    """
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return []

    try:
        import asyncpg
        conn = await asyncpg.connect(db_url)
        try:
            rows = await conn.fetch(
                """
                SELECT rule_id, name, category, keywords, patterns,
                       backend_url, static_response, confidence_threshold, enabled
                FROM bypass_rules
                WHERE enabled = true AND (tenant_id IS NULL OR tenant_id = $1)
                ORDER BY priority DESC
                """,
                tenant_id,
            )
            rules = []
            for row in rows:
                rules.append({
                    "rule_id": row["rule_id"],
                    "name": row["name"],
                    "category": row["category"],
                    "keywords": row["keywords"] if isinstance(row["keywords"], list) else json.loads(row["keywords"]),
                    "patterns": row["patterns"] if isinstance(row["patterns"], list) else json.loads(row["patterns"]),
                    "backend_url": row["backend_url"],
                    "static_response": row["static_response"],
                    "confidence_threshold": row["confidence_threshold"],
                })
            return rules
        finally:
            await conn.close()
    except Exception as exc:
        logger.debug("Database-first rule loading failed (falling back to config): %s", exc)
        return []


async def _record_bypass_stat(rule_id: str, matched: bool, confidence: float, tenant_id: str = "default") -> None:
    """Record bypass rule effectiveness statistics, scoped per tenant so
    tenant-alpha's hit-rate stats never get blended into tenant-beta's."""
    try:
        redis = _get_redis()
        key = f"{_BYPASS_STATS_PREFIX}{tenant_id}:{rule_id}"
        
        # Increment counters
        await redis.hincrby(key, "checks", 1)
        if matched:
            await redis.hincrby(key, "hits", 1)
            await redis.hset(key, "last_hit", str(time.time()))
        
        # Store confidence histogram (binned)
        conf_bin = int(confidence * 10) / 10  # Round to 0.1
        await redis.zincrby(f"{key}:conf_dist", 1, conf_bin)
        
        # Set expiry on stats (30 days)
        await redis.expire(key, 30 * 86400)
    except Exception as exc:
        logger.debug("Bypass stat recording failed: %s", exc)


class G04Bypass:
    """Rules-based bypass with database-first resolution and confidence scoring."""
    
    def __init__(self) -> None:
        # Rules and load bookkeeping are keyed by tenant_id — a single shared
        # list would mean tenant A's database rules silently apply to every
        # other tenant on this process (and vice versa for config fallback).
        self._rules: Dict[str, List[BypassRule]] = {}
        self._rules_loaded_from: Dict[str, str] = {}
        self._last_db_load: Dict[str, float] = {}
        self._db_cache_ttl: int = 60  # Refresh DB rules every 60 seconds

    async def _load_rules(self, cfg: Dict[str, Any], tenant_id: str = "default") -> List[BypassRule]:
        """Load rules from database first, fall back to config. Scoped per tenant_id."""
        now = time.time()
        default_confidence = cfg.get("default_confidence_threshold", 0.70)
        keyword_weight = cfg.get("keyword_weight", 0.4)
        pattern_weight = cfg.get("pattern_weight", 0.6)
        db_cache_ttl = cfg.get("db_cache_ttl_seconds", self._db_cache_ttl)

        # Database-first resolution (if enabled)
        if cfg.get("database_first", True) and (now - self._last_db_load.get(tenant_id, 0)) > db_cache_ttl:
            db_rules = await _load_rules_from_db(tenant_id)
            if db_rules:
                self._rules[tenant_id] = [BypassRule(r, default_confidence, keyword_weight, pattern_weight) for r in db_rules]
                self._rules_loaded_from[tenant_id] = "database"
                self._last_db_load[tenant_id] = now
                logger.debug("G04 loaded %d rules from database for tenant=%s", len(self._rules[tenant_id]), tenant_id)
                return self._rules[tenant_id]

        # Fallback to config file rules
        if tenant_id not in self._rules or self._rules_loaded_from.get(tenant_id) != "config":
            rules_data = cfg.get("rules", [])
            self._rules[tenant_id] = [BypassRule(r, default_confidence, keyword_weight, pattern_weight) for r in rules_data]
            self._rules_loaded_from[tenant_id] = "config"
            logger.debug("G04 loaded %d rules from config for tenant=%s", len(self._rules[tenant_id]), tenant_id)

        return self._rules[tenant_id]

    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G4_bypass", {})
        if not cfg.get("enabled", False):
            return ctx

        tenant_id = getattr(ctx, "tenant_id", "default")
        rules = await self._load_rules(cfg, tenant_id)
        if not rules:
            return ctx

        # Build a single text from messages for matching
        last_user_msg = ""
        for msg in reversed(ctx.messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                last_user_msg = content if isinstance(content, str) else str(content)
                break

        if not last_user_msg:
            return ctx

        tokens_before = ctx.current_token_count

        for rule in rules:
            matched, confidence = rule.matches(last_user_msg)
            
            # Record stats for all rules checked
            await _record_bypass_stat(rule.rule_id, matched, confidence, tenant_id=tenant_id)
            
            if matched:
                logger.info(
                    "[%s] G04 bypass matched rule '%s' (confidence=%.2f)", 
                    ctx.request_id, rule.name, confidence
                )
                response_text = await _call_backend(rule, last_user_msg)
                if response_text is not None:
                    ctx.bypassed = True
                    ctx.cache_response = _make_bypass_response(
                        response_text, ctx.model, ctx.request_id, rule.name, confidence
                    )
                    ctx.savings.bypassed = True
                    ctx.savings.final_tokens_sent = 0
                    ctx.savings.proxy_optimised_tokens = 0   # B1: nothing sent to LLM
                    ctx.savings.provider_prompt_tokens = 0
                    ctx.savings.add_step(
                        GROUP,
                        f"Bypass rule '{rule.name}' (conf={confidence:.2f}) — LLM call eliminated",
                        tokens_before,
                        0,
                    )
                    return ctx

        return ctx


async def _call_backend(rule: BypassRule, user_text: str) -> Optional[str]:
    if rule.static_response:
        return rule.static_response
    if rule.backend_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    rule.backend_url, json={"query": user_text}
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("response") or data.get("answer") or str(data)
        except Exception as exc:
            logger.warning("G04 backend call failed for rule '%s': %s", rule.name, exc)
    return None


def _make_bypass_response(
    text: str, model: str, request_id: str, rule_name: str, confidence: float
) -> Dict[str, Any]:
    return {
        "id": f"bypass-{request_id}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "_token_opt": {
            "bypassed": True, 
            "rule": "G04",
            "rule_name": rule_name,
            "confidence": round(confidence, 3),
        },
    }
