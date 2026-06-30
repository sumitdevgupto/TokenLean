"""
G05 L3 Cache — headroom.SemanticCache(scorer="hybrid")

Replaces the previous GPTCache integration with headroom's hybrid-scorer
semantic cache. Falls back gracefully to L1/L2 if headroom is unavailable.

GPTCacheL3 is kept as a backward-compatible alias for HeadroomSemanticCacheL3.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# headroom availability
_semantic_cache_available = False
try:
    import headroom as _headroom_mod
    _semantic_cache_available = True
except ImportError:
    pass


class HeadroomSemanticCacheL3:
    """headroom.SemanticCache(scorer="hybrid") L3 cache integration."""

    def __init__(self, scorer: str = "hybrid") -> None:
        self._scorer = scorer
        self._cache: Optional[Any] = None
        self._initialized = False

        if not _semantic_cache_available:
            logger.debug("headroom not available — L3 SemanticCache disabled")
            return

        try:
            self._cache = _headroom_mod.SemanticCache(scorer=scorer)
            self._initialized = True
            logger.info("HeadroomSemanticCacheL3 initialized (scorer=%s)", scorer)
        except Exception as exc:
            logger.warning("HeadroomSemanticCacheL3 initialization failed: %s", exc)

    async def get(self, messages: List[Dict[str, str]], threshold: float = 0.85) -> Optional[Dict[str, Any]]:
        """Get cached response for messages using hybrid semantic scoring."""
        if not self._initialized or self._cache is None:
            return None

        query = self._messages_to_query(messages)
        try:
            result = self._cache.search(query, threshold)
            if result and len(result) > 0:
                data = result[0]
                logger.debug("HeadroomSemanticCacheL3 hit (score=%.3f)", data.get("similarity", 0.0))
                return {"response": data.get("response"), "cache_level": "L3"}
            return None
        except Exception as exc:
            logger.debug("HeadroomSemanticCacheL3 get failed: %s", exc)
            return None

    async def put(self, messages: List[Dict[str, str]], response: Dict[str, Any], ttl: int = 86400) -> bool:
        """Store response in the semantic cache."""
        if not self._initialized or self._cache is None:
            return False

        query = self._messages_to_query(messages)
        try:
            self._cache.put(query, {"response": response}, ttl=ttl)
            logger.debug("HeadroomSemanticCacheL3 stored")
            return True
        except Exception as exc:
            logger.debug("HeadroomSemanticCacheL3 put failed: %s", exc)
            return False

    def _messages_to_query(self, messages: List[Dict[str, str]]) -> str:
        user_parts = [
            m.get("content", "")
            for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        return "\n".join(user_parts) if user_parts else json.dumps(messages)


# Backward-compatible alias — callers that imported GPTCacheL3 continue to work
GPTCacheL3 = HeadroomSemanticCacheL3
