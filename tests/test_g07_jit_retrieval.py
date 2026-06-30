"""
G07 Retrieval — JIT Auto-Extraction Tests

Tests the change that wires _extract_rag_query() as a fallback when no
explicit rag_query param is provided but jit_retrieval_enabled=true.

Key behaviours verified:
- No explicit rag_query + JIT on  → auto-extract from last user message → retrieval fires
- No explicit rag_query + JIT off → skip retrieval (return ctx unchanged)
- Explicit rag_query provided     → use it directly (JIT extraction not called)
- x_jit_retrieval=false param     → skip retrieval even if jit_retrieval_enabled=true
- Messages with no user turn      → no query extracted → skip retrieval
- Long user message               → query capped at 500 chars
- G07 disabled entirely           → pass through immediately
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import RequestContext
from middleware.g07_retrieval import G07Retrieval, _extract_rag_query


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ctx(messages=None, params=None, jit_enabled=True, g07_enabled=True):
    ctx = MagicMock(spec=RequestContext)
    ctx.messages = messages or [{"role": "user", "content": "What is RAG?"}]
    ctx.params = params or {}
    ctx.config = {
        "groups": {
            "G7_retrieval": {
                "enabled": g07_enabled,
                "jit_retrieval_enabled": jit_enabled,
                "top_k": 3,
                "top_k_after_rerank": 1,
                "similarity_threshold": 0.85,
                "max_chunk_tokens": 500,
                "max_total_context_tokens": 2000,
                "use_pgvector_fallback": False,
            }
        }
    }
    ctx.current_token_count = 100
    ctx.request_id = "test-g07"
    ctx.model = "gpt-4o-mini"
    ctx.savings = MagicMock()
    ctx.savings.add_step = MagicMock()
    ctx.qdrant_collection = None
    return ctx


def _chunk(text="Relevant context about RAG.", score=0.92):
    return {"text": text, "score": score}


# ---------------------------------------------------------------------------
# _extract_rag_query unit tests
# ---------------------------------------------------------------------------

class TestExtractRagQuery:

    def test_returns_last_user_message(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        assert _extract_rag_query(messages) == "Second question"

    def test_returns_none_when_no_user_message(self):
        messages = [
            {"role": "system", "content": "System only."},
            {"role": "assistant", "content": "No user turn."},
        ]
        assert _extract_rag_query(messages) is None

    def test_caps_query_at_500_chars(self):
        long_content = "x" * 600
        messages = [{"role": "user", "content": long_content}]
        result = _extract_rag_query(messages)
        assert result == "x" * 500

    def test_skips_non_string_content(self):
        messages = [
            {"role": "user", "content": [{"type": "image_url", "url": "..."}]},
            {"role": "user", "content": "Text question"},
        ]
        assert _extract_rag_query(messages) == "Text question"

    def test_empty_messages(self):
        assert _extract_rag_query([]) is None


# ---------------------------------------------------------------------------
# JIT auto-extraction integration tests
# ---------------------------------------------------------------------------

class TestG07JITAutoExtraction:

    @pytest.mark.asyncio
    async def test_jit_on_without_explicit_param_fires_retrieval(self):
        """JIT enabled + no rag_query param → auto-extracts from messages → retrieval runs."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "Explain vector databases"}],
            jit_enabled=True,
        )
        chunks = [_chunk("Vector databases store embeddings.")]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock, return_value=chunks), \
             patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock, return_value=chunks), \
             patch("middleware.g07_retrieval.langfuse_tracing"), \
             patch("middleware.g07_retrieval.count_messages_tokens", return_value=120):
            result = await G07Retrieval().process_request(ctx)

        # A system message with retrieved context should have been injected
        system_msgs = [m for m in result.messages if m.get("role") == "system"]
        assert any("[Retrieved context]" in m["content"] for m in system_msgs)

    @pytest.mark.asyncio
    async def test_jit_off_skips_retrieval_without_explicit_param(self):
        """JIT disabled + no rag_query param → retrieval skipped, ctx unchanged."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "Explain vector databases"}],
            jit_enabled=False,
        )
        original_messages = list(ctx.messages)

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock) as mock_search:
            result = await G07Retrieval().process_request(ctx)

        mock_search.assert_not_called()
        assert result.messages == original_messages

    @pytest.mark.asyncio
    async def test_explicit_rag_query_param_takes_priority(self):
        """Explicit rag_query param is used as-is; auto-extraction is not needed."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "Unrelated user message"}],
            params={"rag_query": "Specific override query"},
            jit_enabled=True,
        )
        chunks = [_chunk("Result for override query.")]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock, return_value=chunks) as mock_search, \
             patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock, return_value=chunks), \
             patch("middleware.g07_retrieval.langfuse_tracing"), \
             patch("middleware.g07_retrieval.count_messages_tokens", return_value=120):
            result = await G07Retrieval().process_request(ctx)

        # _hybrid_search should have been called with the explicit param, not the user message
        call_args = mock_search.call_args
        assert call_args[0][0] == "Specific override query"

    @pytest.mark.asyncio
    async def test_x_jit_retrieval_false_param_overrides_config(self):
        """Per-request x_jit_retrieval=false disables retrieval even if config says enabled."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "Tell me about embeddings"}],
            params={"x_jit_retrieval": "false"},
            jit_enabled=True,
        )
        original_messages = list(ctx.messages)

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock) as mock_search:
            result = await G07Retrieval().process_request(ctx)

        mock_search.assert_not_called()
        assert result.messages == original_messages

    @pytest.mark.asyncio
    async def test_no_user_message_skips_retrieval(self):
        """No user turn in messages → auto-extraction returns None → retrieval skipped."""
        ctx = _ctx(
            messages=[
                {"role": "system", "content": "System prompt only."},
                {"role": "assistant", "content": "I am ready."},
            ],
            jit_enabled=True,
        )
        original_messages = list(ctx.messages)

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock) as mock_search:
            result = await G07Retrieval().process_request(ctx)

        mock_search.assert_not_called()
        assert result.messages == original_messages

    @pytest.mark.asyncio
    async def test_g07_disabled_passes_through_immediately(self):
        """Group disabled → return ctx immediately, no extraction attempted."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "Something"}],
            g07_enabled=False,
        )
        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock) as mock_search, \
             patch("middleware.g07_retrieval._extract_rag_query") as mock_extract:
            result = await G07Retrieval().process_request(ctx)

        mock_search.assert_not_called()
        mock_extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_jit_on_empty_search_results_leaves_messages_unchanged(self):
        """Retrieval fires but returns no chunks → messages unchanged, no savings step."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "Obscure topic with no docs"}],
            jit_enabled=True,
        )
        original_messages = list(ctx.messages)

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock, return_value=[]), \
             patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock, return_value=[]), \
             patch("middleware.g07_retrieval.langfuse_tracing"), \
             patch("middleware.g07_retrieval.RAGFallbackOrchestrator") as mock_fallback_cls:
            mock_fallback = AsyncMock()
            mock_fallback.search_with_fallback = AsyncMock(return_value=[])
            mock_fallback_cls.return_value = mock_fallback
            result = await G07Retrieval().process_request(ctx)

        # No context injection → messages should still be the same user message
        user_msgs = [m for m in result.messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "Obscure topic with no docs"

    @pytest.mark.asyncio
    async def test_savings_step_recorded_when_context_injected(self):
        """When retrieval succeeds, savings.add_step is called with G07 group."""
        ctx = _ctx(
            messages=[{"role": "user", "content": "What is HNSW?"}],
            jit_enabled=True,
        )
        chunks = [_chunk("HNSW is a graph-based index for ANN search.")]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock, return_value=chunks), \
             patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock, return_value=chunks), \
             patch("middleware.g07_retrieval.langfuse_tracing"), \
             patch("middleware.g07_retrieval.count_messages_tokens", return_value=140):
            await G07Retrieval().process_request(ctx)

        ctx.savings.add_step.assert_called_once()
        group_arg = ctx.savings.add_step.call_args[0][0]
        assert group_arg == "G07"
