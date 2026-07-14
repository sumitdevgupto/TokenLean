"""G07 retrieve() helper — the ctx-free retrieval core factored out for reuse.

Verifies the public `retrieve(query, collection, ...)` helper (used by the commercial
docs-chat endpoint) around the mocked Qdrant/rerank primitives:
  - rejects an invalid collection name (SQL/identifier-injection guard reused from G07)
  - returns [] for empty query/collection
  - runs hybrid search → ChunkGuard → rerank and preserves `source` for citations
  - falls back to pgvector when Qdrant is unset but DATABASE_URL is present
"""
import pytest
from unittest.mock import AsyncMock, patch

from middleware.g07_retrieval import retrieve


@pytest.mark.asyncio
async def test_retrieve_rejects_invalid_collection():
    # A name with a quote / space can never be a safe collection identifier.
    assert await retrieve("q", 'bad"; DROP') == []
    assert await retrieve("q", "has space") == []


@pytest.mark.asyncio
async def test_retrieve_empty_inputs():
    assert await retrieve("", "rag_product_docs") == []
    assert await retrieve("q", "") == []


@pytest.mark.asyncio
async def test_retrieve_hybrid_then_rerank_preserves_source():
    hits = [
        {"text": "Rotate your key from Settings.", "score": 0.9, "source": "api-key-lifecycle"},
        {"text": "Unrelated.", "score": 0.2, "source": "onboarding"},
    ]
    with patch("middleware.g07_retrieval._hybrid_search", new=AsyncMock(return_value=hits)), \
         patch("middleware.g07_retrieval._rerank", new=AsyncMock(return_value=hits[:1])):
        out = await retrieve("how do I rotate my key", "rag_product_docs",
                             top_k=8, top_k_final=4, use_pgvector=False)
    assert len(out) == 1
    assert out[0]["source"] == "api-key-lifecycle"
    assert "score" in out[0]  # rerank score / retrieval score preserved for citation ranking


@pytest.mark.asyncio
async def test_retrieve_passes_top_k_final_not_top_k_to_hybrid_search():
    """Regression: retrieve() must forward its OWN top_k_final to _hybrid_search's fusion-limit
    param, not top_k again — otherwise a caller asking for more final results than the prefetch
    width (top_k_final > top_k) gets silently capped at top_k before rerank ever runs."""
    with patch("middleware.g07_retrieval._hybrid_search", new=AsyncMock(return_value=[])) as hybrid, \
         patch("middleware.g07_retrieval._rerank", new=AsyncMock(return_value=[])):
        await retrieve("q", "rag_product_docs", top_k=3, top_k_final=10, use_pgvector=False)
    # _hybrid_search(query, top_k, top_k_final, qdrant_url, collection, cfg)
    args = hybrid.await_args.args
    assert args[1] == 3    # top_k (prefetch width) unchanged
    assert args[2] == 10   # top_k_final (fusion limit) — NOT top_k again


@pytest.mark.asyncio
async def test_retrieve_pgvector_path(monkeypatch):
    # Force the pgvector branch: Qdrant unset, DATABASE_URL present.
    monkeypatch.setattr("middleware.g07_retrieval._QDRANT_URL", "")
    monkeypatch.setattr("middleware.g07_retrieval._PGVECTOR_URL", "postgres://x")
    pg_hits = [{"text": "From pgvector.", "score": 0.8, "source": "portal-guide"}]
    with patch("middleware.g07_retrieval._pgvector_search", new=AsyncMock(return_value=pg_hits)) as pg, \
         patch("middleware.g07_retrieval._hybrid_search", new=AsyncMock()) as hy:
        out = await retrieve("q", "rag_product_docs", use_pgvector=None)
    pg.assert_awaited_once()
    hy.assert_not_awaited()
    assert out and out[0]["source"] == "portal-guide"
