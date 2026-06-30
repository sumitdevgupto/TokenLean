"""Unit tests for the provider-native batch lane (G13 P2) — OpenAI + Anthropic.

All provider SDK calls are mocked via the adapter's client factory, so these tests
never hit the network. They verify JSONL/custom_id construction, status mapping,
and result parsing — not live batch behaviour (which needs live keys + hours).
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from providers.openai_adapter import OpenAIAdapter


def _items():
    return [
        {
            "request_id": "r1",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Q1"}],
            "params": {"temperature": 0.2, "_internal": "x", "x_team": "t", "model": "dup", "batch_topic": "b"},
        },
        {
            "request_id": "r2",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Q2"}],
            "params": {},
        },
    ]


# ---------------------------------------------------------------------------
# OpenAI Batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestOpenAIBatch:
    def _client(self):
        client = MagicMock()
        client.files.create = AsyncMock(return_value=MagicMock(id="file-123"))
        client.batches.create = AsyncMock(return_value=MagicMock(id="batch-abc"))
        return client

    async def test_submit_builds_jsonl_with_custom_id_and_returns_job_id(self):
        client = self._client()
        with patch.object(OpenAIAdapter, "_make_async_client", return_value=client):
            job_id = await OpenAIAdapter().submit_batch(_items(), "sk-test", {})

        assert job_id == "batch-abc"
        # JSONL payload captured from files.create(file=...)
        payload = client.files.create.call_args.kwargs["file"].decode("utf-8")
        lines = [json.loads(l) for l in payload.splitlines()]
        assert [l["custom_id"] for l in lines] == ["r1", "r2"]
        assert all(l["url"] == "/v1/chat/completions" for l in lines)
        body0 = lines[0]["body"]
        assert body0["model"] == "gpt-4o-mini"
        assert body0["temperature"] == 0.2
        # Internal/duplicate params must NOT leak into the batch body
        assert "_internal" not in body0
        assert "x_team" not in body0
        assert "batch_topic" not in body0
        # batch created against the uploaded file
        assert client.batches.create.call_args.kwargs["input_file_id"] == "file-123"

    async def test_completion_window_configurable(self):
        client = self._client()
        with patch.object(OpenAIAdapter, "_make_async_client", return_value=client):
            await OpenAIAdapter().submit_batch(_items(), "sk-test", {"completion_window": "1h"})
        assert client.batches.create.call_args.kwargs["completion_window"] == "1h"

    @pytest.mark.parametrize("status,expected", [
        ("completed", "completed"),
        ("in_progress", "pending"),
        ("validating", "pending"),
        ("failed", "failed"),
        ("expired", "failed"),
        ("cancelled", "failed"),
    ])
    async def test_poll_status_mapping(self, status, expected):
        client = MagicMock()
        client.batches.retrieve = AsyncMock(return_value=MagicMock(status=status))
        with patch.object(OpenAIAdapter, "_make_async_client", return_value=client):
            assert await OpenAIAdapter().poll_batch("batch-abc", "sk-test") == expected

    async def test_fetch_results_parses_success_and_error(self):
        out = "\n".join([
            json.dumps({"custom_id": "r1", "response": {"status_code": 200, "body": {"id": "c1"}}}),
            json.dumps({"custom_id": "r2", "response": {"status_code": 400, "body": {"error": "bad"}}}),
            "",  # blank line tolerated
        ])
        client = MagicMock()
        client.batches.retrieve = AsyncMock(return_value=MagicMock(output_file_id="out-1"))
        client.files.content = AsyncMock(return_value=MagicMock(text=out))
        with patch.object(OpenAIAdapter, "_make_async_client", return_value=client):
            results = await OpenAIAdapter().fetch_batch_results("batch-abc", "sk-test")

        by_id = {r["request_id"]: r for r in results}
        assert by_id["r1"]["response"] == {"id": "c1"}
        assert "error" in by_id["r2"]

    async def test_fetch_results_empty_when_no_output_file(self):
        client = MagicMock()
        client.batches.retrieve = AsyncMock(return_value=MagicMock(output_file_id=None))
        with patch.object(OpenAIAdapter, "_make_async_client", return_value=client):
            assert await OpenAIAdapter().fetch_batch_results("batch-abc", "sk-test") == []


# ---------------------------------------------------------------------------
# litellm unified batch lane (base impl — used by Anthropic / Gemini)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestLiteLLMBatchLane:
    async def test_submit_uses_litellm_with_custom_provider(self):
        from providers.gemini_adapter import GeminiAdapter
        with patch("litellm.acreate_file", new=AsyncMock(return_value=MagicMock(id="file-9"))) as cf, \
             patch("litellm.acreate_batch", new=AsyncMock(return_value=MagicMock(id="batch-9"))) as cb:
            job = await GeminiAdapter().submit_batch(_items(), "key", {"completion_window": "1h"})
        assert job == "batch-9"
        assert cf.await_args.kwargs["custom_llm_provider"] == "gemini"
        assert cb.await_args.kwargs["input_file_id"] == "file-9"
        assert cb.await_args.kwargs["custom_llm_provider"] == "gemini"
        assert cb.await_args.kwargs["completion_window"] == "1h"
        payload = cf.await_args.kwargs["file"].decode("utf-8")
        assert "r1" in payload and "r2" in payload

    @pytest.mark.parametrize("status,expected", [
        ("completed", "completed"), ("in_progress", "pending"), ("failed", "failed"),
    ])
    async def test_poll_maps_status(self, status, expected):
        from providers.anthropic_adapter import AnthropicAdapter
        with patch("litellm.aretrieve_batch", new=AsyncMock(return_value=MagicMock(status=status))):
            assert await AnthropicAdapter().poll_batch("b", "key") == expected

    async def test_fetch_parses_results_via_litellm(self):
        from providers.gemini_adapter import GeminiAdapter
        out = json.dumps({"custom_id": "r1", "response": {"status_code": 200, "body": {"id": "c1"}}})
        with patch("litellm.aretrieve_batch", new=AsyncMock(return_value=MagicMock(output_file_id="out-1"))), \
             patch("litellm.afile_content", new=AsyncMock(return_value=MagicMock(text=out))):
            res = await GeminiAdapter().fetch_batch_results("b", "key")
        assert res[0]["request_id"] == "r1"
        assert res[0]["response"] == {"id": "c1"}

    async def test_fetch_empty_without_output_file(self):
        from providers.anthropic_adapter import AnthropicAdapter
        with patch("litellm.aretrieve_batch", new=AsyncMock(return_value=MagicMock(output_file_id=None))):
            assert await AnthropicAdapter().fetch_batch_results("b", "key") == []
