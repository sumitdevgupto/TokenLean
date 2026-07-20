"""Unit tests for the G13 provider-native batch lane (P2): dispatcher, native
flush grouping/fallback, and the background poller. Redis and provider adapters
are mocked — no network, no live batch."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import g13_batch


@pytest.fixture(autouse=True)
def _clear_native_memo():
    """The native-unsupported memo is process-global — isolate it between tests."""
    g13_batch._NATIVE_BATCH_UNSUPPORTED.clear()
    yield
    g13_batch._NATIVE_BATCH_UNSUPPORTED.clear()


def _items(n=2, provider_model="gpt-4o-mini"):
    return [
        {
            "request_id": f"r{i}",
            "model": provider_model,
            "messages": [{"role": "user", "content": f"Q{i}"}],
            "params": {},
        }
        for i in range(n)
    ]


def _fake_adapter(name="openai", native=True, job_id="batch-1"):
    a = MagicMock()
    a.name = name
    a.supports_native_batch.return_value = native
    a.submit_batch = AsyncMock(return_value=job_id)
    return a


# ---------------------------------------------------------------------------
# Dispatcher: provider_native flag routes to native vs loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFlushDispatcher:
    async def test_disabled_uses_loop(self):
        cfg = {"groups": {"G13_batch": {"provider_native": False}}}
        with patch.object(g13_batch, "_flush_batch_native", new=AsyncMock()) as native, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch("t", _items(), cfg)
        loop.assert_awaited_once()
        native.assert_not_awaited()

    async def test_enabled_uses_native(self):
        cfg = {"groups": {"G13_batch": {"provider_native": True}}}
        with patch.object(g13_batch, "_flush_batch_native", new=AsyncMock()) as native, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch("t", _items(), cfg)
        native.assert_awaited_once()
        loop.assert_not_awaited()


# ---------------------------------------------------------------------------
# Native flush: grouping, submission, fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFlushNative:
    async def test_submits_native_and_records_job(self):
        adapter = _fake_adapter()
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_record_batch_job", new=AsyncMock()) as rec, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(2), cfg)

        adapter.submit_batch.assert_awaited_once()
        submitted_items = adapter.submit_batch.await_args.args[0]
        assert [it["request_id"] for it in submitted_items] == ["r0", "r1"]
        # _record_batch_job now receives the full items (not just ids) so it can carry
        # each item's baseline_tokens through to the poller for header attribution.
        rec.assert_awaited_once()
        job_id_arg, provider_arg, items_arg = rec.await_args.args
        assert job_id_arg == "batch-1" and provider_arg == "openai"
        assert [it["request_id"] for it in items_arg] == ["r0", "r1"]
        loop.assert_not_awaited()

    async def test_falls_back_when_provider_not_native(self):
        adapter = _fake_adapter(native=False)
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(2), cfg)
        adapter.submit_batch.assert_not_awaited()
        loop.assert_awaited_once()

    async def test_falls_back_when_key_missing(self):
        adapter = _fake_adapter()
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value=None), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(1), cfg)
        adapter.submit_batch.assert_not_awaited()
        loop.assert_awaited_once()

    async def test_falls_back_when_submit_raises(self):
        adapter = _fake_adapter()
        adapter.submit_batch = AsyncMock(side_effect=RuntimeError("boom"))
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(1), cfg)
        loop.assert_awaited_once()
        assert "openai" in g13_batch._NATIVE_BATCH_UNSUPPORTED  # memoised

    async def test_memo_skips_native_after_failure(self):
        adapter = _fake_adapter()
        adapter.submit_batch = AsyncMock(side_effect=RuntimeError("boom"))
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(1), cfg)  # 1st: fails → memo
            await g13_batch._flush_batch_native("t", _items(1), cfg)  # 2nd: skips native
        assert adapter.submit_batch.await_count == 1   # native attempted only once
        assert loop.await_count == 2                   # loop used both times


# ---------------------------------------------------------------------------
# Poller: status handling + result storage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPollBatchJobs:
    def _redis(self, jobs):
        r = MagicMock()
        r.hgetall = AsyncMock(return_value=jobs)
        r.hdel = AsyncMock()
        return r

    async def test_pending_job_is_left_in_place(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="pending")
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            finished = await g13_batch.poll_batch_jobs({})
        assert finished == 0
        redis.hdel.assert_not_awaited()
        store.assert_not_awaited()

    async def test_completed_job_stores_results_and_removes_job(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0", "r1"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0"}},
            {"request_id": "r1", "error": "bad"},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            finished = await g13_batch.poll_batch_jobs({})

        assert finished == 1
        redis.hdel.assert_awaited_once()
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r0"]["status"] == "completed"
        assert stored["r0"]["response"]["_batch_request_id"] == "r0"
        assert stored["r1"]["status"] == "failed"

    async def test_completed_missing_result_marked_failed(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0", "r1"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0"}},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch.poll_batch_jobs({})
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r1"]["status"] == "failed"

    async def test_failed_job_marks_all_failed_and_removes(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0", "r1"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="failed")
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            finished = await g13_batch.poll_batch_jobs({})
        assert finished == 1
        assert all(c.args[1]["status"] == "failed" for c in store.await_args_list)
        redis.hdel.assert_awaited_once()

    async def test_no_jobs_is_noop(self):
        redis = self._redis({})
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            assert await g13_batch.poll_batch_jobs({}) == 0


@pytest.mark.asyncio
class TestBaselineTokensAttribution:
    """Regression: /v1/batch/results needs baseline_tokens to build x-tokenlean-* headers
    on completion, but it has no RequestContext at poll time — baseline_tokens has to be
    threaded through from _accumulate → the flush lane → _store_batch_result."""

    async def test_record_batch_job_carries_baseline_tokens_map(self):
        redis = MagicMock()
        redis.hset = AsyncMock()
        items = [
            {"request_id": "r0", "baseline_tokens": 120},
            {"request_id": "r1", "baseline_tokens": 340},
        ]
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            await g13_batch._record_batch_job("job-1", "openai", items)
        stored = json.loads(redis.hset.await_args.args[2])
        assert stored["request_ids"] == ["r0", "r1"]
        assert stored["baseline_tokens"] == {"r0": 120, "r1": 340}

    async def test_poll_completed_native_job_stores_baseline_tokens(self):
        jobs = {"batch-1": json.dumps({
            "provider": "openai", "request_ids": ["r0"],
            "baseline_tokens": {"r0": 250},
        })}
        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value=jobs)
        redis.hdel = AsyncMock()
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0", "usage": {"prompt_tokens": 100}}},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch.poll_batch_jobs({})
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r0"]["baseline_tokens"] == 250

    async def test_poll_missing_baseline_tokens_map_defaults_to_zero(self):
        """Backward compatible: a job recorded before this fix (no baseline_tokens key)
        must not crash the poller."""
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0"]})}
        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value=jobs)
        redis.hdel = AsyncMock()
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0"}},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch.poll_batch_jobs({})
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r0"]["baseline_tokens"] == 0


@pytest.mark.asyncio
async def test_start_batch_poller_noop_when_native_disabled():
    # Returns immediately (no infinite loop) when provider_native is off.
    cfg = {"groups": {"G13_batch": {"enabled": True, "provider_native": False}}}
    await g13_batch.start_batch_poller(cfg)
