"""Unit tests for G07 — Retrieval Optimisation (RAG)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestTheCallersTopKIsParsedAndCapped:
    """X-Rag-Top-K was parsed with int() before G07's error handling, so a non-number made
    the request a 500, and never capped, so a million sized every Qdrant prefetch."""

    async def _searched_top_k(self, make_ctx, minimal_config, value, **g7):
        import copy
        config = copy.deepcopy(minimal_config)
        config["groups"]["G7_retrieval"].update(g7)
        params = {"rag_query": "capital of France"}
        if value is not None:
            params["x_rag_top_k"] = value
        ctx = make_ctx([{"role": "user", "content": "q"}], params=params, config=config)
        search = AsyncMock(return_value=[])
        with patch("middleware.g07_retrieval._hybrid_search", search):
            from middleware.g07_retrieval import G07Retrieval
            g07 = G07Retrieval()
            g07._rag_fallback.search_with_fallback = AsyncMock(return_value=[])
            await g07.process_request(ctx)
        return search.await_args.args[1]

    @pytest.mark.parametrize("value, expected", [
        ("7", 7), (" 7 ", 7), (7, 7), (None, 3),
        ("abc", 3), ("", 3), ("0", 3), ("-2", 3), ("2.5", 3), (True, 3)])
    async def test_the_header_or_the_configured_top_k(self, make_ctx, minimal_config,
                                                      value, expected):
        assert await self._searched_top_k(make_ctx, minimal_config, value, top_k=3) == expected

    async def test_the_header_is_capped(self, make_ctx, minimal_config):
        assert await self._searched_top_k(make_ctx, minimal_config, "1000000", top_k=3) == 50
        assert await self._searched_top_k(make_ctx, minimal_config, "1000000", top_k=3,
                                          max_top_k=10) == 10
        assert await self._searched_top_k(make_ctx, minimal_config, "50", top_k=3) == 50

    async def test_the_operators_own_top_k_is_not_capped(self, make_ctx, minimal_config):
        assert await self._searched_top_k(make_ctx, minimal_config, None, top_k=80) == 80


def test_the_chunk_guard_follows_each_requests_config():
    # The first request's max_chunk_tokens / max_total_context_tokens held for the process:
    # another tenant's overlay, or a reload, changed nothing until restart.
    from middleware.g07_retrieval import G07Retrieval
    g07 = G07Retrieval()
    first = g07._get_chunk_guard({"max_chunk_tokens": 100, "max_total_context_tokens": 400})
    second = g07._get_chunk_guard({"max_chunk_tokens": 900, "max_total_context_tokens": 3000})
    assert (first.max_chunk_tokens, first.max_total_tokens) == (100, 400)
    assert (second.max_chunk_tokens, second.max_total_tokens) == (900, 3000)
    default = g07._get_chunk_guard({})
    assert (default.max_chunk_tokens, default.max_total_tokens) == (1000, 4000)


class TestRetrievedContextFollowsTheCallersPrompt:
    """Retrieved documents change with every query. Put first, they came before the tenant's
    own system prompt, so the provider's prompt cache never matched that prompt. They now sit
    just before the latest user turn, still as a system message, which G31 scans."""

    def test_the_documents_go_just_before_the_latest_user_turn(self):
        from middleware.g07_retrieval import _inject_context
        messages = [{"role": "system", "content": "You are support."},
                    {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"}]
        out = _inject_context(messages, "Paris is the capital.")
        assert [(m["role"], m["content"]) for m in out] == [
            ("system", "You are support."), ("user", "q1"), ("assistant", "a1"),
            ("system", "[Retrieved context]\nParis is the capital."), ("user", "q2")]
        assert len(messages) == 4                      # the caller's list is left as it was

    def test_with_no_user_turn_they_go_last(self):
        from middleware.g07_retrieval import _inject_context
        out = _inject_context([{"role": "system", "content": "S"}], "doc")
        assert [m["content"] for m in out] == ["S", "[Retrieved context]\ndoc"]

    @pytest.mark.asyncio
    async def test_the_caller_s_prompt_stays_first_through_retrieval(self, make_ctx):
        ctx = make_ctx([{"role": "system", "content": "You are support."},
                        {"role": "user", "content": "What is the capital?"}],
                       params={"rag_query": "capital of France"})
        chunks = [{"text": "Paris is the capital of France.", "score": 0.95}]
        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock,
                   return_value=chunks), \
             patch("middleware.g07_retrieval._rerank", new_callable=AsyncMock,
                   return_value=chunks):
            from middleware.g07_retrieval import G07Retrieval
            ctx = await G07Retrieval().process_request(ctx)
        assert ctx.messages[0]["content"] == "You are support."
        assert ctx.messages[1]["content"].startswith("[Retrieved context]")
        assert ctx.messages[-1]["content"] == "What is the capital?"


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

        # A [Retrieved context] system message is added before the user turn
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

    async def test_the_fallback_chain_gets_g3_s_settings(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Answer based on context."}],
                       params={"rag_query": "capital of France"})
        g3 = {"rag_fallback": {"strategies": ["dense_only"], "top_k": 3}}
        ctx.config["groups"]["G3_doc_pipeline"] = g3
        with patch("middleware.g07_retrieval._hybrid_search", new_callable=AsyncMock,
                   return_value=[]):
            from middleware.g07_retrieval import G07Retrieval
            g07 = G07Retrieval()
            g07._rag_fallback.search_with_fallback = AsyncMock(return_value=[])
            await g07.process_request(ctx)
        assert g07._rag_fallback.search_with_fallback.await_args.kwargs["cfg"] == g3

    @pytest.mark.parametrize("fail", [True, False])
    async def test_the_hybrid_search_client_is_always_closed(self, monkeypatch, fail):
        import qdrant_client
        from middleware import g07_retrieval as g07
        closed = []

        class _Client:
            def __init__(self, **kwargs):
                pass

            async def query_points(self, **kwargs):
                if fail:
                    raise ConnectionError("qdrant down")
                return type("R", (), {"points": []})()

            async def close(self):
                closed.append(True)

        async def _dense(query):
            return [0.1, 0.2]

        async def _sparse(query):
            return None

        monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", _Client)
        monkeypatch.setattr(g07, "_embed_dense", _dense)
        monkeypatch.setattr(g07, "_embed_sparse", _sparse)
        assert await g07._hybrid_search("q", 5, 5, "http://qdrant:6333", "rag_docs", {}) == []
        assert closed == [True]

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
    # G07 also stashes the injected chunk texts for response-path grounding coverage.
    assert ctx.rag_chunk_texts == ["Paris is the capital of France."]
