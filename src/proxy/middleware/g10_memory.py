"""
G10 · Conversation & Memory Management
Stage: Into the LLM
Saving: 30–70% multi-turn input tokens
Technique:
  1. Sliding window: keep last N turns verbatim, summarise older turns with cheap model.
  2. State externalisation: inject only current-step context from Redis state store.
  3. SKILLS.md pattern: retrieve only relevant agent skills per task via hybrid RAG
     (saves ~1,700 tokens/call vs. embedding all procedures in system prompt).
  4. Mem0 integration: Long-term memory with entity extraction
  5. Skills stored in Qdrant chunks for semantic retrieval
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from middleware import langfuse_tracing
# Shared history primitives (extracted to middleware.history_utils so G26
# budget-aware compaction can reuse them without a cross-group import). The
# private aliases are kept so existing call sites — and test patch targets like
# ``middleware.g10_memory._summarise`` — keep working unchanged.
from middleware.history_utils import (
    safe_window_split as _safe_window_split,
    summarise_turns as _summarise,
)
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
# Defaults only: a tenant's G10 settings (skills_similarity_threshold,
# memory_query_max_chars) win over these env vars.
_SKILLS_SIMILARITY_THRESHOLD = float(os.getenv("SKILLS_SIMILARITY_THRESHOLD", "0.70"))
_MEMORY_QUERY_MAX_CHARS = int(os.getenv("MEMORY_QUERY_MAX_CHARS", "400"))


def _query_max_chars(cfg: Dict) -> int:
    """How much of the last user message a memory or skills lookup sends."""
    try:
        return int(cfg.get("memory_query_max_chars", _MEMORY_QUERY_MAX_CHARS))
    except (TypeError, ValueError):
        return _MEMORY_QUERY_MAX_CHARS


def _skills_threshold(cfg: Dict) -> float:
    """The lowest score at which a retrieved skill is injected."""
    try:
        return float(cfg.get("skills_similarity_threshold", _SKILLS_SIMILARITY_THRESHOLD))
    except (TypeError, ValueError):
        return _SKILLS_SIMILARITY_THRESHOLD


def _memory_user(ctx: Any) -> str:
    """The user whose long-term memory this request reads and writes, or "" for none.

    It is the AUTHENTICATED user, ctx.user_id: a legacy key's user, or an X-User-ID the
    tenant's allowlist accepted. A user id in the request body is the caller's choice and is
    never used. A tenant key authenticates the tenant, not a user: without an accepted
    X-User-ID its user_id is the tenant id, and one memory for that would be shared by every
    user of the tenant, so such a request gets none."""
    user = str(getattr(ctx, "user_id", "") or "")
    tenant_key = (getattr(ctx, "params", None) or {}).get("_auth_tenant_id")
    if not user or (tenant_key and user == str(tenant_key)):
        return ""
    return user


def _last_text(messages: List[Dict[str, Any]], role: str) -> str:
    """The content of the last ``role`` message when it is plain text, else ""."""
    for m in reversed(messages):
        if m.get("role") == role:
            content = m.get("content", "")
            return content if isinstance(content, str) else ""
    return ""


def _last_exchange(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The last assistant turn and the last user turn, in the order they came, each cut to
    500 characters: what G10 gives Mem0 to remember. A turn that is not plain text is left
    out."""
    picked: Dict[str, Tuple[int, str]] = {}
    for i in range(len(messages) - 1, -1, -1):
        role = messages[i].get("role")
        if role in ("user", "assistant") and role not in picked:
            content = messages[i].get("content", "")
            picked[role] = (i, content if isinstance(content, str) else "")
            if len(picked) == 2:
                break
    return [{"role": role, "content": text[:500]}
            for role, (_, text) in sorted(picked.items(), key=lambda kv: kv[1][0])
            if text.strip()]

# Mem0 configuration
_MEM0_API_URL = os.getenv("MEM0_API_URL", "")  # Mem0 API endpoint
_SKILLS_COLLECTION = os.getenv("SKILLS_COLLECTION", "agent_skills")  # Qdrant collection for skills
# Mem0: a request waits at most _MEM0_SEARCH_WAIT_S for its memories; each Mem0 API call gives
# up after _MEM0_HTTP_TIMEOUT_S (the library's own default is 300 s); a client that failed to
# start is tried again after _MEM0_RETRY_S; and while _MEM0_MAX_PENDING_STORES stores are
# waiting on Mem0, a further one is skipped.
_MEM0_SEARCH_WAIT_S = 2.0
_MEM0_HTTP_TIMEOUT_S = 10.0
_MEM0_RETRY_S = 300.0
_MEM0_MAX_PENDING_STORES = 100

# mem0ai reports usage to its own analytics service unless MEM0_TELEMETRY is false, and reads
# the variable when it is imported: off unless the operator turns it on.
os.environ.setdefault("MEM0_TELEMETRY", "false")

# Optional integrations (graceful fallback if not installed)
_mem0_available = False
try:
    from mem0 import AsyncMemoryClient          # mem0ai 2.x
    _mem0_available = True
except (ImportError, OSError):                  # OSError: it creates ~/.mem0 on import
    pass


def _build_mem0_client(api_key: str, host: str):
    """mem0ai's AsyncMemoryClient for ``host``. Its constructor checks the key with a
    blocking HTTP call, so Mem0MemoryClient builds it in a worker thread."""
    import httpx
    return AsyncMemoryClient(api_key=api_key, host=host,
                             client=httpx.AsyncClient(timeout=_MEM0_HTTP_TIMEOUT_S))


class Mem0MemoryClient:
    """Mem0 long-term memory through mem0ai's AsyncMemoryClient: the Mem0 platform API at
    MEM0_API_URL, with MEM0_API_KEY. A request never waits on Mem0 for long: the client is
    built in a worker thread (requests go without memories until it is ready), a lookup
    waits at most _MEM0_SEARCH_WAIT_S, and a store runs in the background."""

    def __init__(self, api_url: str = "", api_key: Optional[str] = None, factory=None):
        self.api_url = api_url or _MEM0_API_URL
        self.api_key = os.getenv("MEM0_API_KEY", "") if api_key is None else api_key
        self._factory = factory or _build_mem0_client
        self._client = None
        self._starting: Optional[asyncio.Task] = None
        self._failed_at: Optional[float] = None
        self._stores: set = set()

    def client(self):
        """The ready library client, or None. The first call starts building it."""
        if self._client is not None or self._starting is not None:
            return self._client
        if not (_mem0_available and self.api_url and self.api_key):
            return None
        if self._failed_at is not None and time.monotonic() - self._failed_at < _MEM0_RETRY_S:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._starting = loop.create_task(self._start())
        return None

    async def _start(self) -> None:
        try:
            self._client = await asyncio.to_thread(self._factory, self.api_key, self.api_url)
            logger.info("G10: Mem0 long-term memory connected (%s)", self.api_url)
        except Exception as exc:
            self._failed_at = time.monotonic()
            logger.warning("G10: the Mem0 client did not start (%s: %.200s); long-term memory "
                           "is off until the next try in %d s", type(exc).__name__, exc,
                           int(_MEM0_RETRY_S))
        finally:
            self._starting = None

    def store_exchange(self, user_id: str, exchange: List[Dict[str, str]],
                       metadata: Dict[str, Any]) -> bool:
        """Send one exchange to Mem0 in the background, so the request does not wait on it.
        False when it was not sent: no client yet, nothing to send, or
        _MEM0_MAX_PENDING_STORES stores already waiting on Mem0."""
        client = self.client()
        if client is None or not exchange:
            return False
        if len(self._stores) >= _MEM0_MAX_PENDING_STORES:
            logger.debug("Mem0 store skipped: %d stores already waiting", len(self._stores))
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(self._store(client, user_id, exchange, metadata))
        self._stores.add(task)
        task.add_done_callback(self._stores.discard)
        return True

    @staticmethod
    async def _store(client, user_id: str, exchange: List[Dict[str, str]],
                     metadata: Dict[str, Any]) -> None:
        try:
            await client.add(exchange, user_id=user_id, metadata=metadata)
        except Exception as exc:
            logger.debug("Mem0 store failed: %s", type(exc).__name__)

    async def retrieve_memories(self, user_id: str, query: str, limit: int = 5,
                                tenant_id: str = "") -> List[str]:
        """The memories Mem0 holds for ``user_id`` that match ``query`` (at most ``limit``).

        When `tenant_id` is provided, applies a post-retrieval metadata filter as
        defense-in-depth: any memory whose stored `tenant_id` metadata differs from
        the caller's tenant is silently dropped, even if the scoped user_id matched.
        This catches future call sites that forget to scope the user_id.
        """
        client = self.client()
        if client is None:
            return []
        try:
            # mem0ai 2.x takes the user only inside filters (a top-level user_id is refused)
            response = await asyncio.wait_for(
                client.search(query, filters={"user_id": user_id}, top_k=limit),
                _MEM0_SEARCH_WAIT_S)
        except Exception as exc:                        # a timeout included
            logger.debug("Mem0 retrieve failed: %s", type(exc).__name__)
            return []
        results = response.get("results") if isinstance(response, dict) else None
        memories = []
        for r in results if isinstance(results, list) else []:
            if not isinstance(r, dict):
                continue
            metadata = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
            stored_tenant = metadata.get("tenant_id", "")
            if tenant_id and stored_tenant and stored_tenant != tenant_id:
                logger.warning(
                    "Mem0 cross-tenant memory rejected: stored_tenant=%s caller_tenant=%s — "
                    "possible scoped_user_id bypass; check for new call sites",
                    stored_tenant, tenant_id,
                )
                continue
            text = r.get("memory")
            if isinstance(text, str) and text:
                memories.append(text)
        return memories



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
    except Exception as exc:
        logger.debug("tenant id not sanitised: %r", exc)
    return f"{base}_{tenant_id}"


class SkillsManager:
    """Manage agent skills in Qdrant chunks."""
    
    def __init__(self, collection: str = _SKILLS_COLLECTION):
        self.collection = collection
        self._qdrant_url = _QDRANT_URL
    
    async def search_skills(self, query: str, top_k: int = 3,
                          tag_filter: Optional[str] = None, *,
                          score_threshold: float) -> List[Dict]:
        """Search for relevant skills scoring at least ``score_threshold``."""
        try:
            from qdrant_client import QdrantClient
            from ml_models import get_text_embedding, qdrant_client_kwargs

            # qdrant_client_kwargs: version-compat + app-layer api-key + GCP IAM bearer.
            client = QdrantClient(**qdrant_client_kwargs(url=self._qdrant_url))
            model = get_text_embedding(_SKILLS_EMBEDDING_MODEL)
            embedding = list(model.embed([query]))[0].tolist()

            results = client.search(
                collection_name=self.collection,
                query_vector=embedding,
                limit=top_k,
                score_threshold=score_threshold,
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
    """Memory management with Mem0 integration and Qdrant skills."""

    def __init__(self):
        self._mem0: Optional[Mem0MemoryClient] = None
        # WS21: one manager per collection — the old single cached manager pinned
        # every tenant to whichever collection the first request resolved.
        self._skills: Dict[str, SkillsManager] = {}
        self._health_warned = False
        self._mem0_user_warned = False

    def _get_mem0(self, cfg: Dict) -> Optional[Mem0MemoryClient]:
        if not cfg.get("mem0_enabled", False):
            return None
        if self._mem0 is None:
            self._mem0 = Mem0MemoryClient()
        return self._mem0

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
            mem0_url = _MEM0_API_URL or os.getenv("MEM0_API_URL", "")
            if mem0_want and not mem0_url:
                logger.warning(
                    "G10: mem0_enabled=true but MEM0_API_URL is not set — "
                    "long-term Mem0 memory is inactive. Set MEM0_API_URL + MEM0_API_KEY."
                )
            elif mem0_want and not os.getenv("MEM0_API_KEY"):
                logger.warning(
                    "G10: mem0_enabled=true but MEM0_API_KEY is not set — "
                    "long-term Mem0 memory is inactive. Set MEM0_API_KEY.")
            # A URL is not enough: the client is built only if its library imported.
            if mem0_want and not _mem0_available:
                logger.warning(
                    "G10: mem0_enabled=true but the mem0 client library could not be "
                    "imported, so this memory is inactive.")
            if cfg.get("zep_enabled"):
                logger.warning(
                    "G10: zep_enabled is set, but Zep support was removed (its client never "
                    "matched the library it imported), so nothing reads it: remove it. Memory "
                    "is the session window, plus Mem0 when mem0_enabled is on.")
            self._health_warned = True

        window: int = cfg.get("sliding_window_turns", 6)
        summary_model: str = cfg.get("summary_model", "")
        if not summary_model:
            logger.warning("[%s] G10 summary_model not set in config — sliding window only, no summarisation", ctx.request_id)

        memory_user = _memory_user(ctx)
        session_id = ctx.params.get("session_id") or ctx.params.get("x_session_id")

        # Mem0 is an external service keyed by a bare user id, with no tenant filter we
        # can rely on, so scope the id itself — tenant-beta can never retrieve or collide
        # with tenant-alpha's long-term memory, even if two tenants reuse a user id. The
        # session id (the trace's session) is scoped the same way.
        tenant_id = getattr(ctx, "tenant_id", "default")
        scoped_user_id = f"{tenant_id}::{memory_user}" if memory_user else ""
        scoped_session_id = f"{tenant_id}::{session_id}" if session_id else session_id

        # 1. Mem0 long-term memory retrieval
        mem0 = self._get_mem0(cfg)
        if mem0 and not memory_user and not self._mem0_user_warned:
            logger.warning(
                "G10: Mem0 is on, but a request came with no per-user identity (a tenant key "
                "without an allow-listed X-User-ID), so it gets no long-term memory: one memory "
                "for the whole tenant would mix its users. Send X-User-ID, with "
                "proxy.allow_user_id_header_override on.")
            self._mem0_user_warned = True
        if mem0 and memory_user:
            try:
                last_msg = _last_text(ctx.messages, "user")[:_query_max_chars(cfg)]
                if last_msg.strip():
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

        # 2. SKILLS.md from Qdrant — retrieve relevant skills
        if cfg.get("skills_enabled", False):
            skills_mgr = self._get_skills_manager(cfg, tenant_id)
            if skills_mgr:
                await self._inject_skills_from_qdrant(ctx, cfg, skills_mgr)
            else:
                await _inject_relevant_skills(ctx, cfg)

        # Store memories for future use: one exchange, sent in the background
        if mem0 and memory_user:
            try:
                exchange = _last_exchange(ctx.messages)
                if exchange:
                    mem0.store_exchange(scoped_user_id, exchange,
                                        {"session_id": scoped_session_id or "unknown",
                                         "tenant_id": tenant_id})
            except Exception as exc:
                logger.debug("Mem0 store failed: %s", exc)

        # 3. Session state and sliding window
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
                    query = content[:_query_max_chars(cfg)]
                break

        if not query:
            return

        try:
            skills = await skills_mgr.search_skills(query, top_k=top_k,
                                                    score_threshold=_skills_threshold(cfg))
            
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

    # Tool-pairing-aware cut: never orphan a role:"tool" result from the
    # assistant/tool_calls turn that declared it (an unmatched tool_call_id
    # 400s the provider). Snap the boundary back over a split tool exchange.
    start = _safe_window_split(turns, window * 2)
    if start == 0:
        return  # No clean boundary — trimming here would orphan a tool pair

    old_turns = turns[:start]
    recent_turns = turns[start:]

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
    """Session memory for a client that relies on the proxy to remember, at no cost to one
    that resends its conversation. The session records how many turns the client sent
    (before the window). A request that brings more than last time resent the conversation:
    it gets no stored summary and costs no summary call, and the window still shortens a long
    history. Otherwise (the session's first request, or no more turns than last time) the
    stored summary is prepended, and the update summarises it with this request's turns, so
    the memory builds up rather than lasting one request."""
    redis = _get_redis()
    key = f"{_session_prefix(ctx)}{session_id}"
    stored = await redis.get(key)
    session_data = json.loads(stored) if stored else None
    turns = [m for m in ctx.messages if m.get("role") != "system"]

    if session_data is not None and len(turns) > int(session_data.get("turn_count") or 0):
        await _apply_sliding_window(ctx, window, summary_model)
        await redis.set(
            key,
            json.dumps({"summary": session_data.get("summary", ""), "turn_count": len(turns)}),
            ex=_SESSION_TTL,
        )
        return

    stored_summary = (session_data or {}).get("summary", "")
    context = ([{"role": "system", "content": f"[Session context]\n{stored_summary}"}]
               if stored_summary else [])
    if context:
        # Prepend stored summary as system context
        ctx.messages = (
            [m for m in ctx.messages if m.get("role") == "system"]
            + context
            + turns
        )

    await _apply_sliding_window(ctx, window, summary_model)

    history = context + turns
    summary = await _summarise(history, summary_model, ctx) if history else ""
    await redis.set(
        key,
        json.dumps({"summary": summary, "turn_count": len(turns)}),
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
                query = content[:_query_max_chars(cfg)]
            break

    if not query:
        return

    try:
        from middleware.g07_retrieval import _hybrid_search, _rerank
        import os as _os

        qdrant_url = _os.getenv("QDRANT_URL", "http://localhost:6333")
        chunks = await _hybrid_search(query, top_k, top_k, qdrant_url, collection, cfg)
        ranked = await _rerank(query, chunks, top_k, _skills_threshold(cfg))

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


# ``_summarise`` / ``_safe_window_split`` now live in ``middleware.history_utils``
# and are alias-imported at the top of this module (shared with G26).
