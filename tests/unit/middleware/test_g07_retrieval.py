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


@pytest.mark.asyncio
class TestRerankFailClosed:
    """When the cross-encoder reranker errors, G07 must FAIL CLOSED — re-apply the
    retrieval cosine floor to cosine-scored chunks instead of injecting the
    unfiltered candidate set, while leaving RRF-fused chunks (different scale) intact."""

    async def test_fail_closed_drops_low_cosine_chunks(self):
        from middleware.g07_retrieval import _rerank
        chunks = [
            {"text": "relevant", "score": 0.90, "_score_kind": "cosine"},
            {"text": "marginal", "score": 0.30, "_score_kind": "cosine"},
        ]
        with patch("ml_models.get_cross_encoder", side_effect=RuntimeError("reranker down")):
            out = await _rerank("q", chunks, top_k=5, threshold=0.0, fallback_floor=0.85)
        texts = [c["text"] for c in out]
        assert "relevant" in texts
        assert "marginal" not in texts   # below the 0.85 cosine floor → dropped

    async def test_fail_closed_keeps_rrf_chunks(self):
        # RRF fusion scores (~0.016) are NOT on the cosine scale — a cosine floor must
        # not nuke them (that would break hybrid RAG on any reranker hiccup).
        from middleware.g07_retrieval import _rerank
        chunks = [{"text": "fused", "score": 0.016, "_score_kind": "rrf"}]
        with patch("ml_models.get_cross_encoder", side_effect=RuntimeError("reranker down")):
            out = await _rerank("q", chunks, top_k=5, threshold=0.0, fallback_floor=0.85)
        assert [c["text"] for c in out] == ["fused"]

    async def test_no_floor_preserves_legacy_passthrough(self):
        # Back-compat: a caller that supplies no fallback_floor keeps the old behaviour.
        from middleware.g07_retrieval import _rerank
        chunks = [{"text": "a", "score": 0.10, "_score_kind": "cosine"}]
        with patch("ml_models.get_cross_encoder", side_effect=RuntimeError("reranker down")):
            out = await _rerank("q", chunks, top_k=5, threshold=0.0)
        assert [c["text"] for c in out] == ["a"]

    async def test_fail_closed_still_caps_to_top_k(self):
        from middleware.g07_retrieval import _rerank
        chunks = [{"text": f"c{i}", "score": 0.95, "_score_kind": "cosine"} for i in range(6)]
        with patch("ml_models.get_cross_encoder", side_effect=RuntimeError("reranker down")):
            out = await _rerank("q", chunks, top_k=3, threshold=0.0, fallback_floor=0.85)
        assert len(out) == 3


@pytest.mark.asyncio
class TestG07Freshness:
    """Task 10 — chunk age computation + max_age_days soft filter (pure helpers,
    deterministic via an injected `now`)."""

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 18, tzinfo=timezone.utc)

    def _chunk(self, days_old, key="ingested_at"):
        from datetime import timedelta
        ts = (self._now() - timedelta(days=days_old)).isoformat()
        return {"text": f"{days_old}d", key: ts}

    async def test_age_prefers_source_date_over_ingested_at(self):
        from middleware.g07_retrieval import _chunk_age_seconds
        from datetime import timedelta
        c = {"text": "x",
             "ingested_at": (self._now() - timedelta(days=1)).isoformat(),
             "source_date": (self._now() - timedelta(days=10)).isoformat()}
        age_days = _chunk_age_seconds(c, self._now()) / 86400.0
        assert round(age_days) == 10   # source_date wins

    async def test_missing_timestamp_is_unknown_age(self):
        from middleware.g07_retrieval import _chunk_age_seconds
        assert _chunk_age_seconds({"text": "x"}, self._now()) is None

    async def test_filter_drops_stale_keeps_fresh(self):
        from middleware.g07_retrieval import _filter_by_freshness
        chunks = [self._chunk(2), self._chunk(400)]   # 2 days, 400 days
        kept = _filter_by_freshness(chunks, max_age_days=30, now=self._now())
        assert [c["text"] for c in kept] == ["2d"]

    async def test_filter_keeps_unknown_age_chunks(self):
        from middleware.g07_retrieval import _filter_by_freshness
        chunks = [self._chunk(400), {"text": "no-ts"}]
        kept = _filter_by_freshness(chunks, max_age_days=30, now=self._now())
        assert "no-ts" in [c["text"] for c in kept]   # unknown age never dropped

    async def test_no_max_age_is_passthrough(self):
        from middleware.g07_retrieval import _filter_by_freshness
        chunks = [self._chunk(400), self._chunk(2)]
        assert _filter_by_freshness(chunks, max_age_days=None, now=self._now()) == chunks
        assert _filter_by_freshness(chunks, max_age_days=0, now=self._now()) == chunks

    async def test_max_age_seconds(self):
        from middleware.g07_retrieval import _max_chunk_age_seconds
        chunks = [self._chunk(1), self._chunk(5)]
        assert round(_max_chunk_age_seconds(chunks, self._now()) / 86400.0) == 5
        assert _max_chunk_age_seconds([{"text": "x"}], self._now()) is None


@pytest.mark.asyncio
async def test_g07_emits_retrieval_metric(make_ctx):
    """G07 records a quality-metric (hit + chunk count) on a RAG retrieval (Task 11)."""
    from unittest.mock import AsyncMock, patch
    ctx = make_ctx([{"role": "user", "content": "q"}], params={"rag_query": "capital of France"})
    chunks = [{"text": "Paris is the capital of France.", "score": 0.95, "_score_kind": "cosine"}]
    with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock, return_value=chunks), \
         patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock, return_value=chunks), \
         patch("middleware.quality_metrics.record_retrieval") as rec:
        from middleware.g07_retrieval import G07Retrieval
        await G07Retrieval().process_request(ctx)
    rec.assert_called_once()
    assert rec.call_args.args[1] == 1   # (tenant_id, n_chunks, max_age) → 1 chunk (hit)
