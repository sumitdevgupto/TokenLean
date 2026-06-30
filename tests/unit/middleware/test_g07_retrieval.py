"""Unit tests for G07 — Retrieval Optimisation (RAG)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG07Retrieval:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G7_retrieval"]["enabled"] = False
        original = [m.copy() for m in ctx.messages]
        from middleware.g07_retrieval import G07Retrieval
        ctx = await G07Retrieval().process_request(ctx)
        assert ctx.messages == original

    async def test_no_rag_marker_passes_through(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Plain question."}])
        original = [m.copy() for m in ctx.messages]
        from middleware.g07_retrieval import G07Retrieval
        ctx = await G07Retrieval().process_request(ctx)
        assert ctx.messages == original

    async def test_rag_query_param_triggers_retrieval(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Answer based on context."}],
            params={"rag_query": "capital of France"},
        )
        chunks = [{"text": "Paris is the capital of France.", "score": 0.95}]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock,
                   return_value=chunks):
            with patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock,
                       return_value=chunks):
                from middleware.g07_retrieval import G07Retrieval
                ctx = await G07Retrieval().process_request(ctx)

        assert any(s.group == "G07" for s in ctx.savings.step_savings)

    async def test_retrieval_injects_context_into_messages(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is the capital?"}],
            params={"rag_query": "capital of France"},
        )
        original_count = len(ctx.messages)
        chunks = [{"text": "Paris is the capital of France.", "score": 0.95}]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock,
                   return_value=chunks):
            with patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock,
                       return_value=chunks):
                from middleware.g07_retrieval import G07Retrieval
                ctx = await G07Retrieval().process_request(ctx)

        # A [Retrieved context] system message is prepended
        assert len(ctx.messages) > original_count
        assert any("Retrieved context" in str(m.get("content", "")) for m in ctx.messages)

    async def test_empty_hybrid_results_escalate_to_rag_fallback_chain(self, make_ctx):
        """When the primary hybrid search returns no chunks, G07 must escalate
        through G3's strict→relaxed→dense→sparse fallback chain rather than
        giving up with empty context."""
        ctx = make_ctx(
            [{"role": "user", "content": "Answer based on context."}],
            params={"rag_query": "capital of France"},
        )
        fallback_chunks = [{"text": "Paris is the capital of France.", "score": 0.80}]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock,
                   return_value=[]):
            with patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock,
                       return_value=fallback_chunks):
                from middleware.g07_retrieval import G07Retrieval
                g07 = G07Retrieval()
                g07._rag_fallback.search_with_fallback = AsyncMock(return_value=fallback_chunks)
                ctx = await g07.process_request(ctx)

        g07._rag_fallback.search_with_fallback.assert_awaited_once()
        assert any("Retrieved context" in str(m.get("content", "")) for m in ctx.messages)

    async def test_nonempty_hybrid_results_skip_rag_fallback_chain(self, make_ctx):
        """When the primary hybrid search already returns results, the G3
        fallback chain must NOT be invoked — avoids extra latency on the
        normal-similarity path."""
        ctx = make_ctx(
            [{"role": "user", "content": "Answer based on context."}],
            params={"rag_query": "capital of France"},
        )
        chunks = [{"text": "Paris is the capital of France.", "score": 0.95}]

        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock,
                   return_value=chunks):
            with patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock,
                       return_value=chunks):
                from middleware.g07_retrieval import G07Retrieval
                g07 = G07Retrieval()
                g07._rag_fallback.search_with_fallback = AsyncMock(return_value=[])
                ctx = await g07.process_request(ctx)

        g07._rag_fallback.search_with_fallback.assert_not_awaited()

    async def test_hybrid_search_error_fallback(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Answer based on docs."}],
            params={"rag_query": "something"},
        )
        original = [m.copy() for m in ctx.messages]

        with patch("middleware.g07_retrieval._hybrid_search",
                   new_callable=AsyncMock, side_effect=Exception("qdrant down")):
            from middleware.g07_retrieval import G07Retrieval
            ctx = await G07Retrieval().process_request(ctx)

        assert ctx.messages == original


@pytest.mark.asyncio
class TestG07PgVectorPool:
    """Pool-lifecycle tests for the G7 pgvector fallback search (acquire/
    release under a shared asyncpg pool, instead of per-request connect/close)."""

    def _make_mock_pool(self, fetch_result=None):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=fetch_result or [])

        mock_acquire_cm = AsyncMock()
        mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_cm.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
        return mock_pool, mock_conn

    async def test_pgvector_search_uses_shared_pool_acquire(self):
        from middleware.g07_retrieval import _pgvector_search

        row = {"text": "Paris is the capital of France.", "source": "doc1", "score": 0.9}
        mock_pool, mock_conn = self._make_mock_pool(fetch_result=[row])

        mock_model = MagicMock()
        mock_model.embed = MagicMock(return_value=iter([MagicMock(tolist=lambda: [0.1, 0.2])]))

        with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
            with patch("ml_models.get_text_embedding", return_value=mock_model):
                results = await _pgvector_search(
                    "capital of France", top_k=3, db_url="postgresql://test/db",
                    collection="docs", cfg={"similarity_threshold": 0.8},
                )

        assert results == [{"text": "Paris is the capital of France.", "source": "doc1", "score": 0.9}]
        mock_pool.acquire.assert_called_once()
        mock_conn.fetch.assert_awaited_once()

    async def test_concurrent_pgvector_searches_reuse_same_pool(self):
        """Concurrent fallback searches must all resolve via get_pg_pool,
        which returns the same shared pool instance — not a fresh connection
        each time."""
        import asyncio
        from middleware.g07_retrieval import _pgvector_search

        mock_pool, mock_conn = self._make_mock_pool(fetch_result=[])

        mock_model = MagicMock()
        mock_model.embed = MagicMock(
            side_effect=lambda *a, **k: iter([MagicMock(tolist=lambda: [0.1, 0.2])])
        )

        with patch("cache.pg_pool.get_pg_pool", new_callable=AsyncMock, return_value=mock_pool) as mock_get_pool:
            with patch("ml_models.get_text_embedding", return_value=mock_model):
                results = await asyncio.gather(
                    _pgvector_search("q1", top_k=3, db_url="postgresql://test/db", collection="docs", cfg={}),
                    _pgvector_search("q2", top_k=3, db_url="postgresql://test/db", collection="docs", cfg={}),
                )

        assert all(r == [] for r in results)
        assert mock_pool.acquire.call_count == 2
        for call in mock_get_pool.await_args_list:
            assert call.args[0] == "postgresql://test/db"
