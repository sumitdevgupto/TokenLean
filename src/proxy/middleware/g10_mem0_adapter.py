"""
G10 Mem0 OSS Integration — Long-Horizon Memory

Provides Mem0 OSS integration for persistent user memory across conversations:
- Store user facts, preferences, context
- Retrieve relevant memories for personalization
- Memory versioning and expiration
- Integration with Qdrant for vector storage
"""
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G10_MEM0"


class Mem0MemoryStore:
    """Mem0 OSS-compatible memory store using Qdrant."""
    
    def __init__(self, qdrant_url: str, collection_name: str = "mem0_memories"):
        self.qdrant_url = qdrant_url
        self.collection_name = collection_name
        self._client: Optional[Any] = None
    
    def _get_client(self):
        """Lazy-init Qdrant client."""
        if self._client is None:
            from qdrant_client import QdrantClient
            self._client = QdrantClient(url=self.qdrant_url)
        return self._client
    
    async def ensure_collection(self):
        """Ensure memory collection exists."""
        try:
            client = self._get_client()
            
            # Check if collection exists
            collections = client.get_collections()
            collection_names = [c.name for c in collections.collections]
            
            if self.collection_name not in collection_names:
                # Create collection with proper schema
                from qdrant_client.models import Distance, VectorParams
                
                client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                )
                
                logger.info("Created Mem0 collection: %s", self.collection_name)
                
        except Exception as exc:
            logger.warning("Failed to ensure Mem0 collection: %s", exc)
    
    async def add_memory(
        self,
        user_id: str,
        memory: str,
        metadata: Optional[Dict] = None,
        expiration_days: Optional[int] = None,
    ) -> str:
        """
        Add a memory for a user.
        
        Args:
            user_id: Unique user identifier
            memory: Memory text to store
            metadata: Additional metadata (tags, category, etc.)
            expiration_days: Optional TTL for memory
            
        Returns:
            memory_id: Unique identifier for stored memory
        """
        try:
            from qdrant_client.models import PointStruct
            
            client = self._get_client()
            
            # Generate memory ID
            memory_id = hashlib.sha256(
                f"{user_id}:{memory}:{time.time()}".encode()
            ).hexdigest()[:16]
            
            # Embed memory
            from fastembed import TextEmbedding
            model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
            embedding = list(model.embed([memory]))[0].tolist()
            
            # Calculate expiration
            expires_at = None
            if expiration_days:
                expires_at = time.time() + (expiration_days * 86400)
            
            # Store in Qdrant
            point = PointStruct(
                id=memory_id,
                vector=embedding,
                payload={
                    "user_id": user_id,
                    "memory": memory,
                    "metadata": metadata or {},
                    "created_at": time.time(),
                    "expires_at": expires_at,
                    "memory_id": memory_id,
                }
            )
            
            client.upsert(
                collection_name=self.collection_name,
                points=[point],
            )
            
            logger.debug("Stored memory %s for user %s", memory_id, user_id)
            return memory_id
            
        except Exception as exc:
            logger.error("Failed to store memory: %s", exc)
            raise
    
    async def get_memories(
        self,
        user_id: str,
        query: Optional[str] = None,
        top_k: int = 5,
        category: Optional[str] = None,
    ) -> List[Dict]:
        """
        Retrieve memories for a user.
        
        If query provided, performs semantic search.
        Otherwise returns most recent memories.
        """
        try:
            client = self._get_client()
            
            # Build filter
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            
            must_conditions = [
                FieldCondition(key="user_id", match=MatchValue(value=user_id))
            ]
            
            if category:
                must_conditions.append(
                    FieldCondition(key="metadata.category", match=MatchValue(value=category))
                )
            
            # Check expiration
            must_conditions.append(
                FieldCondition(
                    key="expires_at",
                    range={"gt": time.time()} if query else None,
                )
            )
            
            if query:
                # Semantic search
                from fastembed import TextEmbedding
                model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
                query_embedding = list(model.embed([query]))[0].tolist()
                
                results = client.search(
                    collection_name=self.collection_name,
                    query_vector=query_embedding,
                    query_filter=Filter(must=must_conditions),
                    limit=top_k,
                    with_payload=True,
                )
            else:
                # Recent memories
                results = client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=Filter(must=must_conditions),
                    limit=top_k,
                    with_payload=True,
                )[0]
            
            return [
                {
                    "memory_id": r.payload.get("memory_id"),
                    "memory": r.payload.get("memory"),
                    "metadata": r.payload.get("metadata"),
                    "created_at": r.payload.get("created_at"),
                    "score": r.score if hasattr(r, 'score') else None,
                }
                for r in results
            ]
            
        except Exception as exc:
            logger.error("Failed to retrieve memories: %s", exc)
            return []
    
    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory."""
        try:
            client = self._get_client()
            client.delete(
                collection_name=self.collection_name,
                points_selector=[memory_id],
            )
            return True
        except Exception as exc:
            logger.error("Failed to delete memory: %s", exc)
            return False
    
    async def get_user_facts(self, user_id: str) -> Dict[str, Any]:
        """Get consolidated facts about a user."""
        memories = await self.get_memories(user_id, top_k=20)
        
        # Extract structured facts from memories
        facts = {
            "preferences": [],
            "context": [],
            "history": [],
        }
        
        for mem in memories:
            meta = mem.get("metadata", {})
            category = meta.get("category", "general")
            
            if category == "preference":
                facts["preferences"].append(mem["memory"])
            elif category == "context":
                facts["context"].append(mem["memory"])
            else:
                facts["history"].append(mem["memory"])
        
        return facts


class G10Mem0Adapter:
    """G10 Mem0 OSS integration middleware."""
    
    def __init__(self):
        self._store: Optional[Mem0MemoryStore] = None
    
    def _get_store(self, qdrant_url: str) -> Mem0MemoryStore:
        if self._store is None:
            self._store = Mem0MemoryStore(qdrant_url)
        return self._store
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        """
        Enrich request with relevant memories.
        
        Retrieves user memories and adds to context.
        """
        cfg = ctx.config.get("groups", {}).get("G10_memory", {})
        if not cfg.get("mem0_enabled", False):
            return ctx
        
        qdrant_url = cfg.get("qdrant_url", "http://localhost:6333")
        
        try:
            store = self._get_store(qdrant_url)
            await store.ensure_collection()
            
            # Get user ID from context
            user_id = ctx.params.get("user_id") or ctx.user_id
            if not user_id:
                return ctx
            
            # Extract query from messages for semantic search
            query = self._extract_query(ctx.messages)
            
            # Retrieve relevant memories
            memories = await store.get_memories(
                user_id=user_id,
                query=query,
                top_k=cfg.get("mem0_top_k", 3),
            )
            
            if memories:
                # Format memories for context
                memory_text = "\n".join([
                    f"- {m['memory']}" for m in memories
                ])
                
                # Add to system message or create new one
                has_system = any(m.get("role") == "system" for m in ctx.messages)
                
                memory_context = f"[User Memory]\n{memory_text}"
                
                if has_system:
                    # Append to existing system message
                    new_messages = []
                    for msg in ctx.messages:
                        if msg.get("role") == "system":
                            new_content = f"{msg.get('content', '')}\n\n{memory_context}"
                            new_messages.append({"role": "system", "content": new_content})
                        else:
                            new_messages.append(msg)
                    ctx.messages = new_messages
                else:
                    # Prepend new system message
                    ctx.messages.insert(0, {"role": "system", "content": memory_context})
                
                ctx.memories_enriched = True
                ctx.savings.add_step(
                    GROUP,
                    f"Mem0: enriched with {len(memories)} memories",
                    ctx.current_token_count,
                    ctx.current_token_count,  # Memories add tokens
                )
                
                logger.debug(
                    "[%s] G10 Mem0: enriched with %d memories",
                    ctx.request_id,
                    len(memories),
                )
            
        except Exception as exc:
            logger.warning("[%s] G10 Mem0 enrichment failed: %s", ctx.request_id, exc)
        
        return ctx
    
    async def store_conversation_memory(
        self,
        ctx: RequestContext,
        memory_text: str,
        category: str = "general",
        expiration_days: Optional[int] = None,
    ):
        """Store a memory from the current conversation."""
        cfg = ctx.config.get("groups", {}).get("G10_memory", {})
        if not cfg.get("mem0_enabled", False):
            return
        
        qdrant_url = cfg.get("qdrant_url", "http://localhost:6333")
        
        try:
            store = self._get_store(qdrant_url)
            user_id = ctx.params.get("user_id") or ctx.user_id
            
            if user_id:
                await store.add_memory(
                    user_id=user_id,
                    memory=memory_text,
                    metadata={
                        "category": category,
                        "conversation_id": ctx.request_id,
                        "timestamp": time.time(),
                    },
                    expiration_days=expiration_days,
                )
                
        except Exception as exc:
            logger.warning("Failed to store memory: %s", exc)
    
    def _extract_query(self, messages: List[Dict]) -> str:
        """Extract query text from messages."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content[:200]  # First 200 chars for search
        return ""
