"""Regression tests for nested Langfuse spans across middleware."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import copy
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_trace_ctx(make_ctx):
    """Return a RequestContext with a mocked langfuse_trace already attached."""
    ctx = make_ctx()
    mock_trace = MagicMock()
    ctx.langfuse_trace = mock_trace
    return ctx, mock_trace


def _span_kwargs(mock_trace):
    """Extract kwargs dict from the first .span() call on a mock trace."""
    # method_calls entries are call objects: (name, args, kwargs)
    span_calls = [c for c in mock_trace.method_calls if c[0] == "span"]
    assert span_calls, "No span() call found on mock_trace"
    return span_calls[0][2]  # index 2 = kwargs dict


@pytest.mark.asyncio
class TestG01CompressionSpan:
    async def test_span_emitted_when_compression_occurs(self, make_ctx):
        # Provide a long system message (>100 chars) to trigger G01 compression
        long_system = "You are a helpful AI assistant. " * 10  # ~320 chars
        ctx = make_ctx(messages=[
            {"role": "system", "content": long_system},
            {"role": "user", "content": "Hello"},
        ])
        mock_trace = MagicMock()
        ctx.langfuse_trace = mock_trace
        # System-prompt compression is opt-in (off by default for safety); enable it
        # so the system message is actually compressed and the span is emitted.
        ctx.config["groups"]["G1_compression"]["compress_system_prompt"] = True

        from middleware.g01_compression import G01Compression

        # Return something shorter to trigger the "len(compressed) < len(content)" guard.
        # _call_llmlingua is awaited, so the patch must be async.
        compressed_system = "You are a helpful AI assistant."
        with patch("middleware.g01_compression._call_llmlingua",
                   new=AsyncMock(return_value=compressed_system)):
            await G01Compression().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G01-compression"
        assert "tokens_before" in kwargs["input"]
        assert "tokens_after" in kwargs["output"]

    async def test_no_span_when_disabled(self, make_ctx):
        ctx = make_ctx()
        mock_trace = MagicMock()
        ctx.langfuse_trace = mock_trace
        ctx.config["groups"]["G1_compression"]["enabled"] = False

        from middleware.g01_compression import G01Compression
        await G01Compression().process_request(ctx)
        mock_trace.span.assert_not_called()


@pytest.mark.asyncio
class TestG05CacheSpan:
    async def test_span_emitted_on_l1_hit(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        from middleware.g05_cache import G05Cache

        with patch("middleware.g05_cache._get_redis") as mock_get_redis:
            mock_redis = MagicMock()
            mock_redis.get = AsyncMock(return_value='{"choices": [], "usage": {}}')
            mock_get_redis.return_value = mock_redis
            await G05Cache().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G05-cache"
        assert kwargs["metadata"]["level"] == "L1"


@pytest.mark.asyncio
class TestG06RoutingSpan:
    async def test_span_emitted_with_routing_details(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4o"],
        }
        from middleware.g06_routing import G06Routing
        await G06Routing().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G06-routing"
        assert "routing_mode" in kwargs["metadata"]


@pytest.mark.asyncio
class TestG07RetrievalSpan:
    async def test_span_emitted_when_chunks_found(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.params["rag_query"] = "test query"  # required to bypass early return
        from middleware.g07_retrieval import G07Retrieval

        with patch("middleware.g07_retrieval._hybrid_search", return_value=[{"text": "chunk1", "score": 0.9}]):
            with patch("middleware.g07_retrieval._rerank", return_value=[{"text": "chunk1", "score": 0.95}]):
                await G07Retrieval().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G07-retrieval"
        assert kwargs["output"]["chunks_retrieved"] == 1


@pytest.mark.asyncio
class TestG10MemorySpan:
    async def test_span_emitted_on_sliding_window(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        # window=2, so need >4 non-system turns to trigger sliding window
        ctx.messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"},
        ]
        from middleware.g10_memory import G10Memory

        with patch("middleware.g10_memory._summarise", return_value="summary"):
            await G10Memory().process_request(ctx)

        span_calls = [c for c in mock_trace.method_calls if c[0] == "span"]
        assert len(span_calls) >= 1
        names = {c[2]["name"] for c in span_calls}  # kwargs are at index 2
        assert "G10-memory" in names


@pytest.mark.asyncio
class TestG11OutputFormatSpan:
    async def test_span_emitted_when_max_tokens_set(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        from middleware.g11_output_format import G11OutputFormat
        await G11OutputFormat().process_request(ctx)

        span_calls = [c for c in mock_trace.method_calls if c[0] == "span"]
        # G11 emits a span whenever it changes params
        assert len(span_calls) == 1
        kwargs = span_calls[0][2]  # kwargs at index 2
        assert kwargs["name"] == "G11-output-format"
        assert "notes" in kwargs["metadata"]


@pytest.mark.asyncio
class TestG12ReasoningBudgetSpan:
    async def test_span_emitted_for_o1_model(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.routed_model = "o1-preview"
        ctx.model = "o1-preview"
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        await G12ReasoningBudget().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G12-reasoning-budget"
        assert kwargs["metadata"]["provider"] in ("openai", "anthropic", "gemini")


@pytest.mark.asyncio
class TestStartTraceIdentityMetadata:
    """Grafana's User/Tenant dropdowns query metadata->>'user_id' and rely on
    the tenant_id label — both must be present on the trace from creation."""

    async def test_start_trace_includes_user_id_in_metadata(self, make_ctx):
        ctx = make_ctx()
        ctx.user_id = "acme-pitch"
        ctx.config["groups"]["G18_observability"] = {"enabled": True, "langfuse_enabled": True}

        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_client.trace.return_value = mock_trace

        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            langfuse_tracing.start_trace(ctx)

        _, kwargs = mock_client.trace.call_args
        assert kwargs["metadata"]["user_id"] == "acme-pitch"

    async def test_start_trace_includes_tenant_id_in_metadata(self, make_ctx):
        ctx = make_ctx()
        ctx.tenant_id = "nova-med"
        ctx.config["groups"]["G18_observability"] = {"enabled": True, "langfuse_enabled": True}

        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_client.trace.return_value = mock_trace

        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            langfuse_tracing.start_trace(ctx)

        _, kwargs = mock_client.trace.call_args
        assert kwargs["metadata"]["tenant_id"] == "nova-med"

    async def test_finish_trace_calls_trace_update_with_tenant(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.tenant_id = "nova-med"
        ctx.user_id = "nova-med-pitch"

        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=None):
            langfuse_tracing.finish_trace(ctx, response=None)

        update_calls = [c for c in mock_trace.method_calls if c[0] == "update"]
        assert update_calls, "trace.update() was not called"
        _, _, kwargs = update_calls[0]
        assert kwargs["metadata"]["tenant_id"] == "nova-med"
        assert kwargs["metadata"]["user_id"] == "nova-med-pitch"


@pytest.mark.asyncio
class TestLangfuseEnabledGate:
    """`langfuse_enabled` gates trace emission. OSS default OFF (absent → off);
    the commercial deploy sets it true. Keys are still required to actually emit."""

    def _cfg(self, ctx, **g18):
        ctx.config["groups"]["G18_observability"] = g18
        return ctx

    async def test_no_trace_when_langfuse_disabled(self, make_ctx):
        ctx = self._cfg(make_ctx(), enabled=True)  # langfuse_enabled absent → OFF
        mock_client = MagicMock()
        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            trace = langfuse_tracing.start_trace(ctx)
        assert trace is None
        mock_client.trace.assert_not_called()

    async def test_trace_created_when_enabled(self, make_ctx):
        ctx = self._cfg(make_ctx(), enabled=True, langfuse_enabled=True)
        mock_client = MagicMock()
        mock_client.trace.return_value = MagicMock()
        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            trace = langfuse_tracing.start_trace(ctx)
        assert trace is not None
        mock_client.trace.assert_called_once()

    async def test_no_trace_when_flag_true_but_no_keys(self, make_ctx):
        ctx = self._cfg(make_ctx(), enabled=True, langfuse_enabled=True)
        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=None):  # keys absent
            trace = langfuse_tracing.start_trace(ctx)
        assert trace is None
