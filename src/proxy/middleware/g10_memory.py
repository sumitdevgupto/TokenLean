"""
G10 · Conversation & Memory Management
Stage: Into the LLM
Saving: 30–70% multi-turn input tokens
Technique:
  1. Sliding window: keep last N turns verbatim, summarise older turns with cheap model.
  2. State externalisation: inject only current-step context from Redis state store.
  3. SKILLS.md pattern: retrieve only relevant agent skills per task via hybrid RAG
     (saves ~1,700 tokens/call vs. embedding all procedures in system prompt).
  4. Mem0/Zep integration: Long-term memory with entity extraction
  5. Skills stored in Qdrant chunks for semantic retrieval
"""
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing
from savings.calculator import count_messages_tokens
from cache.redis_pool import get_redis as _get_redis

logger = logging.getLogger(__name__)
GROUP = "G10"

_SESSION_BASE = "tok_opt:session:"


def _session_prefix(ctx: Any) -> str:
    """Return tenant-scoped Redis session prefix for this request."""
    ns = getattr(ctx, "redis_prefix", "")
    return f"{ns}{_SESSION_BASE}"
_SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
_QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
_SKILLS_EMBEDDING_MODEL = os.getenv("SKILLS_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
_SKILLS_SIMILARITY_THRESHOLD = float(os.getenv("SKILLS_SIMILARITY_THRESHOLD", "0.70"))
_MEMORY_QUERY_MAX_CHARS = int(os.getenv("MEMORY_QUERY_MAX_CHARS", "400"))

# Mem0/Zep configuration
_MEM0_API_URL = os.getenv("MEM0_API_URL", "")  # Mem0 API endpoint
_ZEP_API_URL = os.getenv("ZEP_API_URL", "")    # Zep API endpoint
_SKILLS_COLLECTION = os.getenv("SKILLS_COLLECTION", "agent_skills")  # Qdrant collection for skills

# Optional integrations (graceful fallback if not installed)
_mem0_available = False
try:
    from mem0 import Mem0
    _mem0_available = True
except ImportError:
    pass

_zep_available = False
try:
    from zep_python import ZepClient
    _zep_available = True
except ImportError:
    pass


class Mem0MemoryClient:
    """Mem0 long-term memory integration for entity extraction and recall."""
    
    def __init__(self, api_url: str = ""):
        self.api_url = api_url or _MEM0_API_URL
        self._client = None
        
        if _mem0_available and self.api_url:
            try:
                self._client = Mem0(api_key=os.getenv("MEM0_API_KEY", ""))
            except Exception as exc:
                logger.debug("Mem0 init failed: %s", exc)
    
    async def store_memory(self, user_id: str, content: str, metadata: Dict = None) -> bool:
        """Store a memory for a user."""
        if not self._client:
            return False
        
        try:
            await self._client.add(content, user_id=user_id, metadata=metadata or {})
            return True
        except Exception as exc:
            logger.debug("Mem0 store failed: %s", exc)
            return False
    
    async def retrieve_memories(self, user_id: str, query: str, limit: int = 5,
                                tenant_id: str = "") -> List[str]:
        """Retrieve relevant memories for a user.

        When `tenant_id` is provided, applies a post-retrieval metadata filter as
        defense-in-depth: any memory whose stored `tenant_id` metadata differs from
        the caller's tenant is silently dropped, even if the scoped user_id matched.
        This catches future call sites that forget to scope the user_id.
        """
        if not self._client:
            return []

        try:
            results = await self._client.search(query, user_id=user_id, limit=limit)
            memories = []
            for r in results:
                stored_tenant = r.get("metadata", {}).get("tenant_id", "")
                if tenant_id and stored_tenant and stored_tenant != tenant_id:
                    logger.warning(
                        "Mem0 cross-tenant memory rejected: stored_tenant=%s caller_tenant=%s — "
                        "possible scoped_user_id bypass; check for new call sites",
                        stored_tenant, tenant_id,
                    )
                    continue
                memories.append(r.get("memory", ""))
            return memories
        except Exception as exc:
            logger.debug("Mem0 retrieve failed: %s", exc)
            return []


class ZepMemoryClient:
    """Zep long-term memory integration for conversation history."""
    
    def __init__(self, api_url: str = ""):
        self.api_url = api_url or _ZEP_API_URL
        self._client = None
        
        if _zep_available and self.api_url:
            try:
                self._client = ZepClient(base_url=self.api_url)
            except Exception as exc:
                logger.debug("Zep init failed: %s", exc)
    
    async def add_message(self, session_id: str, role: str, content: str) -> bool:
        """Add a message to Zep memory."""
        if not self._client:
            return False
        
        try:
            from zep_python import Message
            message = Message(role=role, content=content)
            await self._client.memory.add_memory(session_id, message)
            return True
        except Exception as exc:
            logger.debug("Zep add message failed: %s", exc)
            return False
    
    async def get_memory(self, session_id: str, last_n: int = 10) -> List[Dict]:
        """Get recent memory from Zep."""
        if not self._client:
            return []
        
        try:
            memory = await self._client.memory.get_memory(session_id)
            if memory and memory.messages:
                return [{"role": m.role, "content": m.content} for m in memory.messages[-last_n:]]
            return []
        except Exception as exc:
            logger.debug("Zep get memory failed: %s", exc)
            return []


def _tenant_skills_collection(cfg: Dict, tenant_id: str) -> str:
    """WS21: per-tenant skills collection. The 'default' tenant keeps the legacy
    global name (self-host back-compat); every real tenant gets <base>_<tenant> so
    one tenant's skills are never retrieved into another tenant's prompt."""
    base = cfg.get("skills_qdrant_collection", _SKILLS_COLLECTION)
    if not tenant_id or tenant_id == "default":
        return base
    try:
        from tenancy.context import sanitise_tenant_id
        tenant_id = sanitise_tenant_id(tenant_id)
    except Exception:
        pass
    return f"{base}_{tenant_id}"


async def store_skill_in_qdrant(skill_id: str, skill_text: str, metadata: Dict,
                                collection: str = _SKILLS_COLLECTION) -> bool:
    """Store an agent skill in Qdrant for semantic retrieval."""
    try:
        from qdrant_client import QdrantClient
        from ml_models import get_text_embedding

        client = QdrantClient(url=_QDRANT_URL)

        # Embed skill text
        model = get_text_embedding(_SKILLS_EMBEDDING_MODEL)
        embedding = list(model.embed([skill_text]))[0].tolist()

        # Store in Qdrant
        client.upsert(
            collection_name=collection,
            points=[{
                "id": hashlib.md5(skill_id.encode()).hexdigest(),
                "vector": embedding,
                "payload": {
                    "text": skill_text,
                    "skill_id": skill_id,
                    **metadata
                }
            }]
        )
        return True
    except Exception as exc:
        logger.debug("Skill storage in Qdrant failed: %s", exc)
        return False


class SkillsManager:
    """Manage agent skills in Qdrant chunks."""
    
    def __init__(self, collection: str = _SKILLS_COLLECTION):
        self.collection = collection
        self._qdrant_url = _QDRANT_URL
    
    async def add_skill(self, skill_id: str, name: str, description: str, 
                       procedures: List[str], tags: List[str]) -> bool:
        """Add a skill with its procedures to Qdrant."""
        skill_text = f"# {name}\n\n{description}\n\n## Procedures\n" + "\n".join(f"- {p}" for p in procedures)
        
        metadata = {
            "name": name,
            "description": description,
            "tags": tags,
            "created_at": time.time(),
            "version": "1.0"
        }
        
        return await store_skill_in_qdrant(skill_id, skill_text, metadata,
                                           collection=self.collection)
    
    async def search_skills(self, query: str, top_k: int = 3, 
                          tag_filter: Optional[str] = None) -> List[Dict]:
        """Search for relevant skills."""
        try:
            from qdrant_client import QdrantClient
            from ml_models import get_text_embedding

            client = QdrantClient(url=self._qdrant_url)
            model = get_text_embedding(_SKILLS_EMBEDDING_MODEL)
            embedding = list(model.embed([query]))[0].tolist()

            results = client.search(
                collection_name=self.collection,
                query_vector=embedding,
                limit=top_k,
                score_threshold=_SKILLS_SIMILARITY_THRESHOLD,
            )
            
            skills = []
            for r in results:
                payload = r.payload
                if tag_filter and tag_filter not in payload.get("tags", []):
                    continue
                skills.append({
                    "skill_id": payload.get("skill_id"),
                    "name": payload.get("name"),
                    "text": payload.get("text"),
                    "score": r.score
                })
            
            return skills
        except Exception as exc:
            logger.debug("Skills search failed: %s", exc)
            return []


class G10Memory:
    """Memory management with Mem0/Zep integration and Qdrant skills."""

    def __init__(self):
        self._mem0: Optional[Mem0MemoryClient] = None
        self._zep: Optional[ZepMemoryClient] = None
        # WS21: one manager per collection — the old single cached manager pinned
        # every tenant to whichever collection the first request resolved.
        self._skills: Dict[str, SkillsManager] = {}
        self._health_warned = False
    
    def _get_mem0(self, cfg: Dict) -> Optional[Mem0MemoryClient]:
        if not cfg.get("mem0_enabled", False):
            return None
        if self._mem0 is None:
            self._mem0 = Mem0MemoryClient()
        return self._mem0
    
    def _get_zep(self, cfg: Dict) -> Optional[ZepMemoryClient]:
        if not cfg.get("zep_enabled", False):
            return None
        if self._zep is None:
            self._zep = ZepMemoryClient()
        return self._zep
    
    def _get_skills_manager(self, cfg: Dict, tenant_id: str = "default") -> Optional[SkillsManager]:
        if not cfg.get("skills_qdrant_enabled", True):
            return None
        collection = _tenant_skills_collection(cfg, tenant_id)
        mgr = self._skills.get(collection)
        if mgr is None:
            mgr = self._skills[collection] = SkillsManager(collection)
        return mgr
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G10_memory", {})
        if not cfg.get("enabled", False):
            return ctx

        # One-time warning: long-term memory is configured on but no backend is reachable
        if not self._health_warned:
            mem0_want = cfg.get("mem0_enabled", False)
            zep_want = cfg.get("zep_enabled", False)
            mem0_url = _MEM0_API_URL or os.getenv("MEM0_API_URL", "")
            zep_url = _ZEP_API_URL or os.getenv("ZEP_API_URL", "")
            if mem0_want and not mem0_url:
                logger.warning(
                    "G10: mem0_enabled=true but MEM0_API_URL is not set — "
                    "long-term Mem0 memory is inactive. Set MEM0_API_URL + MEM0_API_KEY."
                )
            if zep_want and not zep_url:
                logger.warning(
                    "G10: zep_enabled=true but ZEP_API_URL is not set — "
                    "Zep conversation memory is inactive. Set ZEP_API_URL."
                )
            self._health_warned = True

        window: int = cfg.get("sliding_window_turns", 6)
        summary_model: str = cfg.get("summary_model", "")
        if not summary_model:
            logger.warning("[%s] G10 summary_model not set in config — sliding window only, no summarisation", ctx.request_id)

        user_id = ctx.params.get("user_id") or ctx.params.get("x_user_id", "anonymous")
        session_id = ctx.params.get("session_id") or ctx.params.get("x_session_id")

        # Mem0/Zep are external services keyed by bare user_id/session_id.
        # Neither client supports a tenant filter we can rely on, so scope the
        # id itself — this guarantees tenant-beta can never retrieve or
        # collide with tenant-alpha's long-term memory or conversation
        # history, even if two tenants reuse the same user_id/session_id.
        tenant_id = getattr(ctx, "tenant_id", "default")
        scoped_user_id = f"{tenant_id}::{user_id}" if user_id else user_id
        scoped_session_id = f"{tenant_id}::{session_id}" if session_id else session_id

        # 1. Mem0 long-term memory retrieval
        mem0 = self._get_mem0(cfg)
        if mem0 and user_id:
            try:
                # Extract last user message for memory query
                last_msg = ""
                for m in reversed(ctx.messages):
                    if m.get("role") == "user":
                        last_msg = m.get("content", "")[:_MEMORY_QUERY_MAX_CHARS]
                        break

                if last_msg:
                    memories = await mem0.retrieve_memories(scoped_user_id, last_msg, limit=3,
                                                           tenant_id=tenant_id)
                    if memories:
                        mem_context = "\n".join(f"- {m}" for m in memories)
                        ctx.messages.insert(0, {
                            "role": "system",
                            "content": f"[Long-term memories about user]\n{mem_context}"
                        })
                        logger.debug("[%s] G10 injected %d Mem0 memories", ctx.request_id, len(memories))
            except Exception as exc:
                logger.debug("Mem0 retrieval failed: %s", exc)

        # 2. Zep conversation memory
        zep = self._get_zep(cfg)
        if zep and session_id:
            try:
                zep_memory = await zep.get_memory(scoped_session_id, last_n=window)
                if zep_memory:
                    # Add Zep memory to context
                    zep_context = "\n".join(f"{m['role']}: {m['content'][:100]}" for m in zep_memory)
                    ctx.messages.insert(0, {
                        "role": "system",
                        "content": f"[Recent conversation history from Zep]\n{zep_context}"
                    })
                    logger.debug("[%s] G10 injected Zep memory", ctx.request_id)
            except Exception as exc:
                logger.debug("Zep memory retrieval failed: %s", exc)

        # 3. SKILLS.md from Qdrant — retrieve relevant skills
        if cfg.get("skills_enabled", False):
            skills_mgr = self._get_skills_manager(cfg, tenant_id)
            if skills_mgr:
                await self._inject_skills_from_qdrant(ctx, cfg, skills_mgr)
            else:
                await _inject_relevant_skills(ctx, cfg)

        # Store memories for future use
        if mem0 and user_id and ctx.messages:
            try:
                last_user_msg = None
                last_assistant_msg = None
                for m in reversed(ctx.messages):
                    if not last_user_msg and m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                    if not last_assistant_msg and m.get("role") == "assistant":
                        last_assistant_msg = m.get("content", "")
                
                if last_user_msg:
                    await mem0.store_memory(scoped_user_id, f"User asked: {last_user_msg[:500]}",
                                          {"type": "user_query", "session_id": scoped_session_id or "unknown",
                                           "tenant_id": tenant_id})
                if last_assistant_msg:
                    await mem0.store_memory(scoped_user_id, f"Assistant responded: {last_assistant_msg[:500]}",
                                          {"type": "assistant_response", "session_id": scoped_session_id or "unknown",
                                           "tenant_id": tenant_id})
            except Exception as exc:
                logger.debug("Mem0 store failed: %s", exc)

        # 4. Session state and sliding window
        if not session_id:
            # No session — apply sliding window to message list as-is
            await _apply_sliding_window(ctx, window, summary_model)
            return ctx

        # Externalise state: load from Redis, merge, apply window
        try:
            await _apply_session_state(ctx, session_id, window, summary_model)
        except Exception as exc:
            logger.warning("G10 session state error: %s — falling back to window only", exc)
            await _apply_sliding_window(ctx, window, summary_model)

        return ctx
    
    async def _inject_skills_from_qdrant(self, ctx: RequestContext, cfg: Dict, 
                                         skills_mgr: SkillsManager) -> None:
        """Inject relevant skills from Qdrant."""
        top_k: int = cfg.get("skills_top_k", 2)
        tokens_before = count_messages_tokens(ctx.messages, ctx.model)

        # Build query from last user message
        query = ""
        for m in reversed(ctx.messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    query = content[:_MEMORY_QUERY_MAX_CHARS]
                break

        if not query:
            return

        try:
            skills = await skills_mgr.search_skills(query, top_k=top_k)
            
            if skills:
                skills_text = "\n\n".join(f"## {s['name']}\n{s['text']}" for s in skills)
                skill_msg = {
                    "role": "system",
                    "content": f"[Relevant agent skills from Qdrant]\n{skills_text}",
                }
                # Inject after existing system messages
                system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
                other_msgs = [m for m in ctx.messages if m.get("role") != "system"]
                ctx.messages = system_msgs + [skill_msg] + other_msgs

                tokens_after = count_messages_tokens(ctx.messages, ctx.model)
                ctx.savings.add_step(
                    GROUP,
                    f"SKILLS Qdrant: {len(skills)} skill(s) injected (top-{top_k})",
                    tokens_before,
                    tokens_after,
                )
                langfuse_tracing.add_span(
                    ctx,
                    name="G10-memory",
                    span_input={"tokens_before": tokens_before},
                    output={"skills_injected": len(skills), "tokens_after": tokens_after},
                    metadata={
                        "top_k": top_k,
                        "source": "qdrant",
                        "skill_names": [s['name'] for s in skills],
                    },
                )
                logger.debug(
                    "[%s] G10 Qdrant skills injected: %d → %d tokens",
                    ctx.request_id, tokens_before, tokens_after,
                )
        except Exception as exc:
            logger.warning("G10 Qdrant skills retrieval failed: %s", exc)


async def _apply_sliding_window(
    ctx: RequestContext, window: int, summary_model: str
) -> None:
    messages = ctx.messages
    tokens_before = count_messages_tokens(messages, ctx.model)

    # Separate system messages from conversation turns
    system_msgs = [m for m in messages if m.get("role") == "system"]
    turns = [m for m in messages if m.get("role") != "system"]

    if len(turns) <= window * 2:
        return  # Nothing to trim

    old_turns = turns[: -(window * 2)]
    recent_turns = turns[-(window * 2) :]

    summary = await _summarise(old_turns, summary_model, ctx)
    summary_msg = {
        "role": "system",
        "content": f"[Conversation summary — earlier turns]\n{summary}",
    }

    ctx.messages = system_msgs + [summary_msg] + recent_turns
    tokens_after = count_messages_tokens(ctx.messages, ctx.model)

    ctx.savings.add_step(
        GROUP,
        f"Sliding window: {len(old_turns)} turns → summary ({window}-turn window)",
        tokens_before,
        tokens_after,
    )
    langfuse_tracing.add_span(
        ctx,
        name="G10-memory",
        span_input={"turns_before": len(turns), "tokens_before": tokens_before},
        output={"turns_after": len(recent_turns) + 1, "tokens_after": tokens_after},
        metadata={
            "window": window,
            "old_turns_summarised": len(old_turns),
            "summary_model": summary_model,
        },
    )
    logger.debug(
        "[%s] G10 sliding window: %d → %d tokens",
        ctx.request_id,
        tokens_before,
        tokens_after,
    )


async def _apply_session_state(
    ctx: RequestContext, session_id: str, window: int, summary_model: str
) -> None:
    redis = _get_redis()
    key = f"{_session_prefix(ctx)}{session_id}"
    stored = await redis.get(key)

    if stored:
        session_data = json.loads(stored)
        stored_summary = session_data.get("summary", "")
        # Prepend stored summary as system context
        if stored_summary:
            ctx.messages = (
                [m for m in ctx.messages if m.get("role") == "system"]
                + [{"role": "system", "content": f"[Session context]\n{stored_summary}"}]
                + [m for m in ctx.messages if m.get("role") != "system"]
            )

    await _apply_sliding_window(ctx, window, summary_model)

    # Save updated session state
    all_non_system = [m for m in ctx.messages if m.get("role") != "system"]
    summary = await _summarise(all_non_system, summary_model, ctx) if all_non_system else ""
    await redis.set(
        key,
        json.dumps({"summary": summary, "turn_count": len(all_non_system)}),
        ex=_SESSION_TTL,
    )



async def _inject_relevant_skills(ctx: RequestContext, cfg: Dict) -> None:
    """
    SKILLS.md pattern: retrieve relevant agent skill chunks from Qdrant via hybrid
    search and inject as a compact system message.  Replaces always-on skill blobs.
    Config keys used: skills_qdrant_collection, skills_top_k.
    """
    collection = _tenant_skills_collection(cfg, getattr(ctx, "tenant_id", "default"))
    top_k: int = cfg.get("skills_top_k", 2)
    tokens_before = count_messages_tokens(ctx.messages, ctx.model)

    # Build a query from the last user message
    query = ""
    for m in reversed(ctx.messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                query = content[:400]
            break

    if not query:
        return

    try:
        from middleware.g07_retrieval import _hybrid_search, _rerank
        import os as _os

        qdrant_url = _os.getenv("QDRANT_URL", "http://localhost:6333")
        chunks = await _hybrid_search(query, top_k, top_k, qdrant_url, collection, cfg)
        ranked = await _rerank(query, chunks, top_k, cfg.get("skills_similarity_threshold", 0.70))

        if ranked:
            skills_text = "\n\n".join(c["text"] for c in ranked)
            skill_msg = {
                "role": "system",
                "content": f"[Relevant agent skills]\n{skills_text}",
            }
            # Inject after existing system messages
            system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
            other_msgs = [m for m in ctx.messages if m.get("role") != "system"]
            ctx.messages = system_msgs + [skill_msg] + other_msgs

            tokens_after = count_messages_tokens(ctx.messages, ctx.model)
            ctx.savings.add_step(
                GROUP,
                f"SKILLS retrieval: {len(ranked)} skill chunk(s) injected (top-{top_k})",
                tokens_before,
                tokens_after,
            )
            langfuse_tracing.add_span(
                ctx,
                name="G10-memory",
                span_input={"tokens_before": tokens_before},
                output={"skills_injected": len(ranked), "tokens_after": tokens_after},
                metadata={
                    "top_k": top_k,
                    "skill_scores": [round(c.get("score", 0.0), 3) for c in ranked],
                },
            )
            logger.debug(
                "[%s] G10 skills injected: %d → %d tokens",
                ctx.request_id, tokens_before, tokens_after,
            )
    except Exception as exc:
        logger.warning("G10 skills retrieval failed: %s", exc)


async def _summarise(turns: List[Dict], summary_model: str, ctx: RequestContext) -> str:
    """Summarise old conversation turns using a cheap model."""
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
                "G10 summarisation: provider key unavailable for %s", summary_adapter.name
            )
            return "[summary unavailable]"

        text = "\n".join(
            f"{m.get('role','')}: {m.get('content','')}" for m in turns[:20]
        )
        _call_model, _call_kwargs = summary_adapter.build_call(
            summary_model,
            get_provider_entry(summary_model, ctx.config.get("providers", [])) or {},
            provider_key,
        )
        # This is a real provider call made inside the request pipeline; count its
        # wall-time as LLM (not proxy) time so the SLA latency split stays honest.
        _t0 = time.time()
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
                max_tokens=150,
            )
        finally:
            try:
                ctx.llm_elapsed_ms += (time.time() - _t0) * 1000.0
            except Exception:
                pass
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("G10 summarisation failed: %s", exc)
        return "[summary unavailable]"
