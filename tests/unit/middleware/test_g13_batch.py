"""Unit tests for G13 — Batch Processing & Compact Notation."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG13Batch:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G13_batch"]["enabled"] = False
        original = [m.copy() for m in ctx.messages]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages == original

    async def test_no_structured_data_unchanged(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Plain text question here."}])
        original_content = ctx.messages[0]["content"]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages[0]["content"] == original_content

    async def test_batch_topic_defers_request(self, make_ctx, monkeypatch):
        # Deferred only onto a topic a consumer reads, and only once the write succeeded
        # (this test used to pass with no Redis at all: the failed write was swallowed).
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", {"classification"})
        redis = AsyncMock()
        redis.xlen = AsyncMock(return_value=0)
        ctx = make_ctx(
            [{"role": "user", "content": "Classify this text."}],
            params={"batch_topic": "classification"},
        )
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            ctx = await g13_batch.G13Batch().process_request(ctx)
        assert ctx.batch_deferred is True
        redis.xadd.assert_awaited_once()

    async def test_toon_notation_applied_to_json_in_message(self, make_ctx):
        # TOON only fires when a system message contains 'schema' AND '|'
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        ctx = make_ctx([
            {"role": "system", "content": "schema:name|age"},
            {"role": "user", "content": f"Analyse this data: {json_data}"},
        ])
        tokens_before = ctx.current_token_count
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        # If TOON notation applied, token count should be ≤ before
        assert ctx.current_token_count <= tokens_before

    async def test_step_saving_recorded_when_toon_applied(self, make_ctx):
        big_array = str([{"id": i, "value": f"item-{i}", "status": "active"} for i in range(10)])
        # Need system message with 'schema' and '|' to trigger TOON
        ctx = make_ctx([
            {"role": "system", "content": "schema:id|value|status"},
            {"role": "user", "content": big_array},
        ])
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        for s in ctx.savings.step_savings:
            if s.group == "G13":
                assert s.tokens_after <= s.tokens_before

    async def test_no_system_schema_no_toon(self, make_ctx):
        # Without system message containing schema|, TOON should NOT fire
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        ctx = make_ctx([{"role": "user", "content": json_data}])
        original_content = ctx.messages[0]["content"]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages[-1]["content"] == original_content


@pytest.mark.asyncio
@pytest.mark.asyncio
class TestBatchIsPromisedOnlyWhenItWillRun:
    """A deferred request is answered 202 and billed at once, so it may be
    deferred only when something will produce its result: a consumer reads the topic and
    the request actually reached the stream. Otherwise it is answered now."""

    @pytest.fixture
    def ctx(self, make_ctx):
        return make_ctx([{"role": "user", "content": "Classify this."}],
                        params={"batch_topic": "bulk"})

    @staticmethod
    def _redis(queued=0, xadd_error=None):
        redis = AsyncMock()
        redis.xlen = AsyncMock(return_value=queued)
        redis.xadd = AsyncMock(side_effect=xadd_error)
        return redis

    async def test_a_topic_no_consumer_reads_is_answered_now(self, ctx, monkeypatch):
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", {"other"})
        redis = self._redis()
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            out = await g13_batch.G13Batch().process_request(ctx)
        assert out.batch_deferred is False
        redis.xadd.assert_not_awaited()

    async def test_a_failed_queue_write_is_answered_now(self, ctx, monkeypatch):
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", {"bulk"})
        redis = self._redis(xadd_error=ConnectionError("redis down"))
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            out = await g13_batch.G13Batch().process_request(ctx)
        assert out.batch_deferred is False
        # No owner marker is left for a request never queued (its id would poll as pending
        # forever): one written ahead of the queue write is taken back.
        written = {c.args[0] for c in redis.set.await_args_list}
        deleted = {key for c in redis.delete.await_args_list for key in c.args}
        assert written <= deleted

    async def test_a_stream_at_its_backlog_takes_no_more(self, ctx, monkeypatch):
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", {"bulk"})
        ctx.config["groups"]["G13_batch"]["max_backlog"] = 5
        full, room = self._redis(queued=5), self._redis(queued=4)
        with patch("middleware.g13_batch._get_redis", return_value=full):
            assert (await g13_batch.G13Batch().process_request(ctx)).batch_deferred is False
        full.xadd.assert_not_awaited()
        ctx.batch_deferred = False
        with patch("middleware.g13_batch._get_redis", return_value=room):
            assert (await g13_batch.G13Batch().process_request(ctx)).batch_deferred is True
        room.xadd.assert_awaited_once()

    async def test_the_consumer_registers_its_topics_and_deletes_what_it_ran(self, monkeypatch):
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", set())
        monkeypatch.setattr(g13_batch, "_flush_batch", AsyncMock())
        reads = iter([[("tok_opt:batch:bulk", [("1-0", {"payload": json.dumps({"request_id": "r1"})})])]])

        async def xreadgroup(*args, **kwargs):
            try:
                return next(reads)
            except StopIteration:
                raise asyncio.CancelledError from None   # stop the endless loop after one pass

        redis = AsyncMock()
        redis.xreadgroup = xreadgroup
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"],
                                        "max_pending_ack_ms": 10 ** 9}}}
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(asyncio.CancelledError):
                await g13_batch.start_batch_consumer(cfg)
        assert g13_batch._CONSUMED_TOPICS == {"bulk"}
        redis.xack.assert_awaited_once_with("tok_opt:batch:bulk", "proxy-batch-consumers", "1-0")
        redis.xdel.assert_awaited_once_with("tok_opt:batch:bulk", "1-0")

    async def _consumer_name(self, monkeypatch, batch_cfg):
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", set())
        names = []

        async def xreadgroup(group, consumer, *args, **kwargs):
            names.append(consumer)
            raise asyncio.CancelledError   # stop the endless loop at the first read

        redis = AsyncMock()
        redis.xreadgroup = xreadgroup
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"], **batch_cfg}}}
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(asyncio.CancelledError):
                await g13_batch.start_batch_consumer(cfg)
        return names[0]

    async def test_each_process_reads_under_its_own_consumer_name(self, monkeypatch):
        """Instances sharing one name are one consumer to Redis, so nothing can tell which
        of them holds an unacknowledged entry."""
        first = await self._consumer_name(monkeypatch, {})
        second = await self._consumer_name(monkeypatch, {})
        assert first != second
        assert str(os.getpid()) in first

    async def test_an_operator_set_consumer_name_is_used(self, monkeypatch):
        assert await self._consumer_name(monkeypatch, {"consumer_name": "worker-a"}) == "worker-a"


@pytest.mark.asyncio
class TestBatchEntriesStayWithTheConsumerFlushingThem:
    """A flush makes up to `max_batch_size` provider calls in turn, so it can outlast
    `max_pending_ack_ms`, the idle time after which any consumer's sweep takes an entry
    over. The sweep used to mark such entries failed while their consumer was still on
    them: a client polling then saw "failed", resubmitted, and paid for a second call.
    Now a consumer holds what it is flushing, and the sweep retries only what a stopped
    consumer left, up to `max_attempts` deliveries, then marks it failed."""

    STREAM = "tok_opt:batch:bulk"
    ENTRY = ("7-0", {"payload": json.dumps({"request_id": "r7", "messages": []})})

    async def test_a_flush_holds_its_entries_until_it_ends(self, monkeypatch):
        import asyncio
        from middleware import g13_batch

        async def slow_flush(topic, items, cfg):
            await asyncio.sleep(1.0)

        monkeypatch.setattr(g13_batch, "_flush_batch", slow_flush)
        redis = AsyncMock()
        await g13_batch._flush_held(redis, self.STREAM, "grp", "c1", "bulk",
                                    [("7-0", {"request_id": "r7"})], {}, stale_ms=300)
        holds = redis.xclaim.await_count
        assert holds >= 6      # every 100 ms (a third of the stale time) through a 1 s flush
        assert redis.xclaim.await_args.args == (self.STREAM, "grp", "c1", 0, ["7-0"])
        assert redis.xclaim.await_args.kwargs == {"justid": True}   # not a new delivery
        await asyncio.sleep(0.3)
        assert redis.xclaim.await_count == holds                  # released once done
        redis.xack.assert_awaited_once_with(self.STREAM, "grp", "7-0")
        redis.xdel.assert_awaited_once_with(self.STREAM, "7-0")

    async def test_a_failed_flush_leaves_its_entries_pending_for_a_retry(self, monkeypatch):
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_flush_batch", AsyncMock(side_effect=RuntimeError("x")))
        redis = AsyncMock()
        with pytest.raises(RuntimeError):
            await g13_batch._flush_held(redis, self.STREAM, "grp", "c1", "bulk",
                                        [("7-0", {"request_id": "r7"})], {}, stale_ms=60)
        await asyncio.sleep(0.05)
        redis.xclaim.assert_not_awaited()
        redis.xack.assert_not_awaited()

    def _sweep_redis(self, delivered, stored=None, fields=ENTRY[1]):
        redis = AsyncMock()
        redis.xautoclaim = AsyncMock(return_value=("0-0", [(self.ENTRY[0], fields)], []))
        redis.xpending_range = AsyncMock(return_value=[
            {"message_id": "7-0", "consumer": "c1", "time_since_delivered": 0,
             "times_delivered": delivered}])
        redis.get = AsyncMock(return_value=json.dumps(stored) if stored else None)
        redis.xinfo_consumers = AsyncMock(return_value=[])
        return redis

    async def _sweep(self, monkeypatch, redis, max_attempts=3):
        from middleware import g13_batch
        flushed, results = [], {}

        async def flush(topic, items, cfg):
            flushed.extend(items)

        async def store(request_id, result):
            results[request_id] = result

        monkeypatch.setattr(g13_batch, "_flush_batch", flush)
        monkeypatch.setattr(g13_batch, "_store_batch_result", store)
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            await g13_batch._reclaim_stale_pel(
                redis, "bulk", "grp", "c2",
                {"max_pending_ack_ms": 30000, "max_attempts": max_attempts}, {})
        return flushed, results

    async def test_an_entry_a_stopped_consumer_left_is_retried_not_failed(self, monkeypatch):
        redis = self._sweep_redis(delivered=2)
        flushed, results = await self._sweep(monkeypatch, redis)
        assert [item["request_id"] for item in flushed] == ["r7"]
        assert results == {}
        redis.xack.assert_awaited_once_with(self.STREAM, "grp", "7-0")

    async def test_an_entry_already_answered_is_only_acknowledged(self, monkeypatch):
        redis = self._sweep_redis(delivered=2, stored={"status": "completed", "response": {}})
        flushed, results = await self._sweep(monkeypatch, redis)
        assert flushed == [] and results == {}
        redis.xack.assert_awaited_once_with(self.STREAM, "grp", "7-0")
        redis.xdel.assert_awaited_once_with(self.STREAM, "7-0")

    async def test_an_entry_past_its_attempts_is_marked_failed(self, monkeypatch):
        redis = self._sweep_redis(delivered=4)
        flushed, results = await self._sweep(monkeypatch, redis, max_attempts=3)
        assert flushed == []
        assert results["r7"]["status"] == "failed"   # its poller gets an answer
        redis.xack.assert_awaited_once_with(self.STREAM, "grp", "7-0")
        redis.xdel.assert_awaited_once_with(self.STREAM, "7-0")

    async def test_the_last_attempt_is_still_made(self, monkeypatch):
        redis = self._sweep_redis(delivered=3)
        flushed, results = await self._sweep(monkeypatch, redis, max_attempts=3)
        assert [item["request_id"] for item in flushed] == ["r7"] and results == {}

    async def test_an_entry_its_consumer_acknowledged_since_the_claim_is_left_alone(
            self, monkeypatch):
        redis = self._sweep_redis(delivered=2)
        redis.xpending_range = AsyncMock(return_value=[])     # no longer pending
        flushed, results = await self._sweep(monkeypatch, redis)
        assert flushed == [] and results == {}
        redis.xack.assert_not_awaited()

    async def test_the_consumer_hands_the_sweep_the_full_config(self, monkeypatch):
        """A retried entry is flushed with the config a first attempt gets: without it the
        retry would price nothing, so its cost would never reach the spend counter."""
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", set())
        swept = []

        async def sweep(*args, **kwargs):
            swept.append(args)
            raise asyncio.CancelledError   # stop the endless loop at the first sweep

        async def xreadgroup(*args, **kwargs):
            await asyncio.sleep(0.01)
            return []

        monkeypatch.setattr(g13_batch, "_reclaim_stale_pel", sweep)
        redis = AsyncMock()
        redis.xreadgroup = xreadgroup
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"],
                                        "max_pending_ack_ms": 1}}}
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(asyncio.CancelledError):
                await g13_batch.start_batch_consumer(cfg)
        assert swept[0][-1] is cfg

    async def test_an_entry_whose_content_is_gone_is_only_acknowledged(self, monkeypatch):
        redis = self._sweep_redis(delivered=2, fields=None)
        flushed, results = await self._sweep(monkeypatch, redis)
        assert flushed == [] and results == {}
        redis.xack.assert_awaited_once_with(self.STREAM, "grp", "7-0")

    async def test_consumers_of_stopped_processes_are_deleted(self, monkeypatch):
        from middleware import g13_batch
        day = g13_batch._STOPPED_CONSUMER_IDLE_MS
        redis = self._sweep_redis(delivered=2)
        redis.xinfo_consumers = AsyncMock(return_value=[
            {"name": "c2", "pending": 0, "idle": day + 1},        # this process
            {"name": "gone", "pending": 0, "idle": day + 1},      # a stopped process
            {"name": "gone-with-work", "pending": 1, "idle": day + 1},
            {"name": "alive", "pending": 0, "idle": 5},
        ])
        await self._sweep(monkeypatch, redis)
        redis.xgroup_delconsumer.assert_awaited_once_with(self.STREAM, "grp", "gone")


@pytest.mark.asyncio
class TestAStoredAnswerIsNeverReplacedByAFailure:
    async def test_a_completed_result_is_written_outright(self):
        from middleware import g13_batch
        redis = AsyncMock()
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            await g13_batch._store_batch_result("r1", {"status": "completed", "response": {}})
        redis.set.assert_awaited_once()
        redis.eval.assert_not_awaited()

    async def test_a_failure_is_written_only_where_no_answer_is_stored(self):
        """One atomic step on the server: read the stored result and write the failure
        only if that result is not a completed answer."""
        from middleware import g13_batch
        redis = AsyncMock()
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            await g13_batch._store_batch_result("r1", {"status": "failed", "error": "x"})
        redis.set.assert_not_awaited()
        script, numkeys, key, value, ttl = redis.eval.await_args.args
        assert numkeys == 1 and key == "tok_opt:batch_result:r1"
        assert json.loads(value)["status"] == "failed" and ttl == g13_batch._RESULT_TTL
        assert "completed" in script


class TestBatchBaselineTokensAttribution:
    """Regression: /v1/batch/results has no RequestContext at poll time, so
    baseline_tokens must be captured at accumulate-time and threaded through the flush
    lane to _store_batch_result for the poller's x-tokenlean-* headers to be accurate."""

    async def test_accumulate_includes_baseline_tokens(self, make_ctx):
        from middleware.g13_batch import _accumulate
        ctx = make_ctx([{"role": "user", "content": "hi"}])
        ctx.savings.baseline_tokens = 77
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
        mock_redis.xlen = AsyncMock(return_value=0)      # room on the stream
        with patch("middleware.g13_batch._get_redis", return_value=mock_redis):
            assert await _accumulate(ctx, "topic1") is True
        _stream, fields = mock_redis.xadd.await_args.args
        payload = json.loads(fields["payload"])
        assert payload["baseline_tokens"] == 77

    async def test_flush_loop_threads_baseline_tokens_into_stored_result(self):
        from middleware import g13_batch
        items = [{"request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
                  "params": {}, "model": "gpt-4o-mini", "baseline_tokens": 500,
                  "tenant_id": "acme"}]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0", "usage": {"prompt_tokens": 120}}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch._flush_batch_loop("topic1", items, {})
        store.assert_awaited_once()
        request_id_arg, stored = store.await_args.args
        assert request_id_arg == "r0"
        assert stored["status"] == "completed"
        assert stored["baseline_tokens"] == 500

    async def test_flush_loop_missing_baseline_tokens_defaults_to_zero(self):
        from middleware import g13_batch
        items = [{"request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
                  "params": {}, "model": "gpt-4o-mini", "tenant_id": "acme"}]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0"}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch._flush_batch_loop("topic1", items, {})
        stored = store.await_args.args[1]
        assert stored["baseline_tokens"] == 0


@pytest.mark.asyncio
class TestBatchCacheMarkers:
    """A batched request's prompt-cache markers go to its provider only when that
    provider caches by marker: the flush sends the stored messages and params straight to
    litellm, which would turn the markers into a separately billed Gemini cache or hand
    them to an endpoint that may reject them."""

    MARK = {"type": "ephemeral"}

    async def _flush(self, model):
        from middleware import g13_batch
        mark = self.MARK
        items = [{"request_id": "r0", "model": model, "tenant_id": "acme", "messages": [
            {"role": "system", "content": [{"type": "text", "text": "Rules.", "cache_control": mark}]},
            {"role": "user", "content": "hi", "cache_control": mark}],
            "params": {"tools": [{"type": "function", "function": {"name": "f", "parameters": {}},
                                  "cache_control": mark}]}}]
        providers = [{"name": "openai", "model_prefixes": ["gpt-"]},
                     {"name": "anthropic", "model_prefixes": ["claude"]}]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0", "usage": {"prompt_tokens": 10}}
        with patch("config_loader.get_provider_model_prefixes",
                   return_value={"gpt-": "openai", "claude": "anthropic"}), \
             patch("config_loader.get_providers", return_value=providers), \
             patch("providers.build_litellm_call", return_value=(model, {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)) as call, \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()):
            await g13_batch._flush_batch_loop("topic1", items, {})
        return call.await_args.kwargs

    async def test_a_provider_that_caches_by_marker_gets_them(self):
        sent = await self._flush("claude-3-5-haiku")
        assert sent["messages"][0]["content"][0]["cache_control"] == self.MARK
        assert sent["messages"][1]["cache_control"] == self.MARK
        assert sent["tools"][0]["cache_control"] == self.MARK

    async def test_any_other_provider_gets_none(self):
        sent = await self._flush("gpt-4o-mini")
        assert sent["messages"] == [
            {"role": "system", "content": [{"type": "text", "text": "Rules."}]},
            {"role": "user", "content": "hi"}]
        assert sent["tools"] == [{"type": "function", "function": {"name": "f", "parameters": {}}}]


@pytest.mark.asyncio
class TestBatchRoutesThroughReservationSeam:
    """Tracker 23.44 — the batch flush must go through the SAME provider param-hygiene
    + reasoning-headroom seam every other call site uses (providers.outgoing_params_for).
    Before this fix the loop called litellm.acompletion with the raw item params, so a
    batched request to a reasoning model reached the provider with a budget sized for
    the answer alone — and the result-serve endpoint has no RequestContext to detect the
    resulting empty answer either."""

    async def test_flush_loop_reserves_reasoning_headroom_for_a_reasoning_model(self):
        from middleware import g13_batch
        items = [{
            "request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
            "params": {"max_completion_tokens": 1024, "reasoning_effort": "medium"},
            "model": "o4-mini", "tenant_id": "acme",
        }]
        cfg = {
            "groups": {"G12_reasoning": {"reasoning_headroom": {
                "enabled": True,
                "answer_floor_tokens": 512,
                "allowance_tokens": {"low": 1024, "medium": 4096, "high": 16384},
            }}},
            "providers": [],
        }
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0", "usage": {"prompt_tokens": 10}}
        with patch("config_loader.get_provider_model_prefixes", return_value={"o4-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("config_loader.get_default_provider", return_value="openai"), \
             patch("providers.build_litellm_call", return_value=("o4-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)) as mock_call, \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()):
            await g13_batch._flush_batch_loop("topic1", items, cfg)
        sent = mock_call.await_args.kwargs
        # 4096 (medium allowance) + 512 (answer floor) — the reservation the seam
        # applies for a reasoning model whose caller-sized budget can't hold thinking
        # plus an answer. Unrouted, this stays 1024 and the provider gets billed for
        # thinking alone.
        assert sent["max_completion_tokens"] == 4096 + 512

    async def test_flush_loop_strips_the_off_reasoning_sentinel(self):
        """`off` is our internal tier vocabulary, never a value any provider accepts —
        the seam strips it (providers.outgoing_params_for). A side effect of routing
        through the seam for tracker 23.44: previously the raw sentinel reached
        litellm.acompletion unfiltered on this path."""
        from middleware import g13_batch
        items = [{
            "request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
            "params": {"reasoning_effort": "off", "max_tokens": 64},
            "model": "gpt-4o-mini", "tenant_id": "acme",
        }]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0"}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("config_loader.get_default_provider", return_value="openai"), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)) as mock_call, \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()):
            await g13_batch._flush_batch_loop("topic1", items, {})
        sent = mock_call.await_args.kwargs
        assert "reasoning_effort" not in sent
        assert sent["max_tokens"] == 64

    async def test_flush_loop_strips_internal_only_param_keys(self):
        """INTERNAL_PARAM_KEYS (template_id/workflow_id/rag_query/batch_id/burst_block)
        are middleware-internal and must never reach litellm — the seam strips them.
        The pre-fix loop only stripped `_`/`x_`-prefixed keys and `model`, so any of
        these leaking into a batched item's params would have reached the provider."""
        from middleware import g13_batch
        items = [{
            "request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
            "params": {"template_id": "tpl-1", "max_tokens": 64},
            "model": "gpt-4o-mini", "tenant_id": "acme",
        }]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0"}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("config_loader.get_default_provider", return_value="openai"), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)) as mock_call, \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()):
            await g13_batch._flush_batch_loop("topic1", items, {})
        sent = mock_call.await_args.kwargs
        assert "template_id" not in sent
        assert sent["max_tokens"] == 64


@pytest.mark.asyncio
class TestABatchedRequestsCostReachesTheSpendCap:
    """A batched request is billed, and counts against the quota and trial, when it is
    queued; its cost is known only when the provider answers. The consumer then adds it,
    priced as G18 prices a served call, to the tenant's spend counter, which the spend cap
    reads."""

    _USAGE = {"id": "c0", "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}

    @staticmethod
    def _item(**changes):
        item = {"request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
                "params": {}, "model": "gpt-4o-mini", "tenant_id": "acme",
                "spend_prefix": "t:acme:", **changes}
        return {k: v for k, v in item.items() if v is not None}

    @staticmethod
    def _spend_counter(monkeypatch):
        from middleware import g00_rate_limit
        added = []

        class _Redis:
            async def incrbyfloat(self, key, amount):
                added.append((key, amount))
                return amount

            async def expire(self, key, ttl):
                pass

        monkeypatch.setattr(g00_rate_limit, "_get_redis", lambda: _Redis())
        return added

    @staticmethod
    async def _flush(item, response, *, error=None, g18=True):
        """Run one item through the per-item flush; returns the status stored for it. The
        native-batch discount is configured, as shipped: the loop pays the sync price."""
        from middleware import g13_batch
        answer = MagicMock()
        answer.model_dump.return_value = response
        call = AsyncMock(side_effect=error) if error else AsyncMock(return_value=answer)
        cfg = {"groups": {"G18_observability": {"enabled": g18, "batch_discount_multiplier": 0.5}}}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("config_loader.get_default_provider", return_value="openai"), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=call), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch._flush_batch_loop("topic1", [item], cfg)
        return store.await_args.args[1]["status"]

    async def test_the_queued_request_names_the_tenant_s_spend_counter(self, make_ctx):
        from middleware.g13_batch import _accumulate
        ctx = make_ctx([{"role": "user", "content": "hi"}])
        ctx.tenant_id, ctx.redis_prefix = "acme", "t:acme:"
        queued = []
        for impersonator in (None, "ops"):
            ctx.impersonator_tenant_id = impersonator
            redis = AsyncMock()
            redis.xlen = AsyncMock(return_value=0)
            with patch("middleware.g13_batch._get_redis", return_value=redis):
                assert await _accumulate(ctx, "topic1") is True
            queued.append(json.loads(redis.xadd.await_args.args[1]["payload"])["spend_prefix"])
        # An admin key acting as the tenant spends nothing of the tenant's.
        assert queued == ["t:acme:", ""]

    async def test_the_answer_s_cost_is_added_to_the_tenant_s_spend_counter(self, monkeypatch):
        from middleware.g00_rate_limit import G00RateLimit
        from savings.calculator import estimate_cost
        added = self._spend_counter(monkeypatch)
        assert await self._flush(self._item(), self._USAGE) == "completed"
        cost = estimate_cost(1200, 300, "gpt-4o-mini")
        assert cost > 0
        assert added == [(G00RateLimit.spend_key("t:acme:"), pytest.approx(cost))]

    @pytest.mark.parametrize("case", [
        "an admin key sent it as the tenant", "it was queued without a counter",
        "the provider reported no usage"])
    async def test_nothing_is_added_when(self, monkeypatch, case):
        added = self._spend_counter(monkeypatch)
        item, response = self._item(), self._USAGE
        if case == "an admin key sent it as the tenant":
            item = self._item(spend_prefix="")
        elif case == "it was queued without a counter":
            item = self._item(spend_prefix=None)
        else:
            response = {"id": "c0"}
        assert await self._flush(item, response) == "completed"
        assert added == []

    async def test_the_cost_is_added_with_g18_switched_off(self, monkeypatch):
        # G18's switch is its metrics, export and tracing; a tenant turning it off must not
        # turn off its own spend cap.
        from middleware.g00_rate_limit import G00RateLimit
        from savings.calculator import estimate_cost
        added = self._spend_counter(monkeypatch)
        assert await self._flush(self._item(), self._USAGE, g18=False) == "completed"
        assert added == [(G00RateLimit.spend_key("t:acme:"),
                          pytest.approx(estimate_cost(1200, 300, "gpt-4o-mini")))]

    async def test_a_failed_call_adds_nothing(self, monkeypatch):
        added = self._spend_counter(monkeypatch)
        assert await self._flush(self._item(), None, error=RuntimeError("provider down")) == "failed"
        assert added == []

    async def test_a_pricing_failure_still_stores_the_answer(self, monkeypatch):
        added = self._spend_counter(monkeypatch)
        with patch("middleware.g18_observability.price_billed_call",
                   side_effect=RuntimeError("bad price book")):
            assert await self._flush(self._item(), self._USAGE) == "completed"
        assert added == []


class TestCompactJsonToToon:
    """Boundary tests for the lowered TOON array-length trigger threshold
    (now >= 2 identical-key items, previously >= 3)."""

    def test_two_item_array_triggers_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert "schema:name|age" in result
        assert "Alice|30" in result
        assert "Bob|25" in result

    def test_single_item_array_does_not_trigger_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30, "city": "Paris", "role": "engineer"}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert result == content

    def test_mixed_key_two_item_array_does_not_trigger_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "city": "Paris"}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert result == content

    def test_three_item_array_still_triggers_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert "schema:name|age" in result


class TestToonGating:
    """Eligibility, net-savings and coverage gates added by the tabular-gating work."""

    def test_nested_array_not_compressed_by_default(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = 'rows [{"id": 1, "meta": {"k": "v"}}, {"id": 2, "meta": {"k": "w"}}]'
        # Nested object values → scalar-only gate leaves the block as JSON.
        assert _compact_json_to_toon(content) == content

    def test_nested_array_compressed_when_allowed(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = 'rows [{"id": 1, "meta": {"k": "v"}}, {"id": 2, "meta": {"k": "w"}}]'
        out = _compact_json_to_toon(
            content, {"toon_allow_nested": True, "toon_require_net_savings": False}
        )
        assert "schema:id|meta" in out

    def test_large_array_over_2000_chars_now_compressed(self):
        import json as _json
        from middleware.g13_batch import _compact_json_to_toon
        data = [{"id": i, "value": f"item-{i}", "status": "active"} for i in range(80)]
        json_str = _json.dumps(data)
        assert len(json_str) > 2000  # the legacy 2000-char ceiling would have skipped this
        content = f"Records: {json_str}"
        result = _compact_json_to_toon(content)
        assert "schema:id|value|status" in result
        assert len(result) < len(content)

    def test_multiple_blocks_all_compressed(self):
        from middleware.g13_batch import _compact_json_to_toon
        a = '[{"x": 1}, {"x": 2}]'
        b = '[{"y": 3}, {"y": 4}]'
        content = f"First {a} then {b}"
        result = _compact_json_to_toon(content)
        assert "schema:x" in result
        assert "schema:y" in result

    def test_min_rows_boundary(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = 'data [{"a": 1}, {"a": 2}]'  # 2-row array
        assert _compact_json_to_toon(content, {"toon_min_rows": 3}) == content
        assert "schema:a" in _compact_json_to_toon(content, {"toon_min_rows": 2})

    def test_uniform_threshold_outlier(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = (
            'rows [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, '
            '{"name": "Carol", "age": 35, "city": "Paris"}]'
        )
        # Strict (1.0): the superset outlier breaks uniformity → left as JSON.
        assert _compact_json_to_toon(content, {"toon_uniform_threshold": 1.0}) == content
        # 0.6: 2/3 rows share the modal key-set → compress with a union header.
        out = _compact_json_to_toon(content, {"toon_uniform_threshold": 0.6})
        assert "schema:name|age|city" in out
        assert "Carol|35|Paris" in out

    def test_net_savings_guard_reverts_when_not_smaller(self, monkeypatch):
        from middleware import g13_batch
        # Force the estimator to report TOON as not strictly smaller → guard reverts.
        monkeypatch.setattr(g13_batch, "estimate_tokens", lambda text, model="": 100)
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        out = g13_batch._compact_json_to_toon(json_data, {"toon_require_net_savings": True})
        assert out == json_data  # reverted

    def test_net_savings_guard_disabled_applies_even_if_not_smaller(self, monkeypatch):
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "estimate_tokens", lambda text, model="": 100)
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        out = g13_batch._compact_json_to_toon(json_data, {"toon_require_net_savings": False})
        assert "schema:name|age" in out  # applied despite equal size


@pytest.mark.asyncio
class TestToonAutoDetect:
    """Auto-detect mode (no manual `schema:` marker) + per-tenant override."""

    async def test_auto_detect_compresses_without_schema_marker(self, make_ctx):
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        ctx = make_ctx([{"role": "user", "content": f"Analyse: {json_data}"}])
        ctx.config["groups"]["G13_batch"]["toon_auto_detect"] = True
        before = ctx.current_token_count
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert "schema:name|age" in ctx.messages[0]["content"]
        assert ctx.current_token_count <= before

    async def test_auto_detect_off_no_marker_unchanged(self, make_ctx):
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        ctx = make_ctx([{"role": "user", "content": f"Analyse: {json_data}"}])
        ctx.config["groups"]["G13_batch"]["toon_auto_detect"] = False
        original = ctx.messages[0]["content"]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages[0]["content"] == original

    async def test_per_tenant_auto_detect_override(self, make_ctx):
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        ctx = make_ctx([{"role": "user", "content": f"Analyse: {json_data}"}])
        # Global default off; the tenant turns auto-detect on.
        ctx.tenant_id = "acme"
        ctx.config.setdefault("tenants", {})["acme"] = {
            "groups": {"G13_batch": {"toon_auto_detect": True}}
        }
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert "schema:name|age" in ctx.messages[0]["content"]


class TestToonCellsAreUnambiguous:
    """A TOON row is the cells joined by '|' and rows by newlines, so a cell holding either
    shifted the columns or split the row, and null, "" and a missing key all read as an
    empty cell. Such a block now stays JSON, and the three are told apart."""

    _CFG = {"toon_require_net_savings": False}

    @pytest.mark.parametrize("rows", [
        [{"name": "a|b", "n": 1}, {"name": "c", "n": 2}],
        [{"name": "line1\nline2", "n": 1}, {"name": "c", "n": 2}],
        [{"name": "carriage\rreturn", "n": 1}, {"name": "c", "n": 2}],
        [{"a|b": 1, "n": 1}, {"a|b": 2, "n": 2}],
        [{"id": 1, "meta": {"note": "x|y"}}, {"id": 2, "meta": {"note": "z"}}],
    ], ids=["pipe", "newline", "carriage-return", "pipe-in-key", "pipe-in-nested"])
    def test_a_block_a_cell_would_corrupt_stays_json(self, rows):
        from middleware.g13_batch import _compact_json_to_toon
        content = "Data: " + json.dumps(rows)
        assert _compact_json_to_toon(content, {**self._CFG, "toon_allow_nested": True}) == content

    def test_null_empty_and_missing_are_told_apart(self):
        from middleware.g13_batch import _compact_json_to_toon
        rows = [{"a": 1, "b": None}, {"a": 2, "b": ""}, {"a": 3, "b": "x"}, {"a": 4}]
        out = _compact_json_to_toon(json.dumps(rows), {**self._CFG, "toon_uniform_threshold": 0.7})
        assert out.splitlines() == ["schema:a|b", "1|null", '2|""', "3|x", "4|"]

    def test_a_string_that_reads_as_null_or_quoted_is_written_as_json(self):
        from middleware.g13_batch import _compact_json_to_toon
        rows = [{"a": "null"}, {"a": '"quoted"'}, {"a": "plain"}]
        out = _compact_json_to_toon(json.dumps(rows), self._CFG)
        assert out.splitlines() == ["schema:a", '"null"', '"\\"quoted\\""', "plain"]

    def test_a_nested_value_is_written_as_json(self):
        from middleware.g13_batch import _compact_json_to_toon
        rows = [{"id": 1, "meta": {"k": None, "on": True}}, {"id": 2, "meta": [1, "x"]}]
        out = _compact_json_to_toon(json.dumps(rows), {**self._CFG, "toon_allow_nested": True})
        assert out.splitlines() == ["schema:id|meta", '1|{"k":null,"on":true}', '2|[1,"x"]']


@pytest.mark.asyncio
class TestToonMarker:
    """Without toon_auto_detect, TOON runs only when a system message carries the notation
    itself ("schema:name|age"). It used to run whenever a system prompt merely mentioned
    "schema" and held a "|" anywhere, as any markdown table does."""

    _ROWS = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'

    async def _run(self, make_ctx, system):
        ctx = make_ctx([{"role": "system", "content": system},
                        {"role": "user", "content": f"Analyse this data: {self._ROWS}"}])
        from middleware.g13_batch import G13Batch
        return (await G13Batch().process_request(ctx)).messages[1]["content"]

    @pytest.mark.parametrize("system", [
        "Answer using this schema.\n| field | type |\n|---|---|\n| name | str |",
        "The output schema is JSON; separate alternatives with |.",
        "Schema:name|age",
        "schema: reply in JSON\n| a | b |",
        "Reply in the schema:name|age format.",
    ])
    async def test_a_prompt_without_the_marker_leaves_the_data_alone(self, make_ctx, system):
        assert await self._run(make_ctx, system) == f"Analyse this data: {self._ROWS}"

    @pytest.mark.parametrize("system", [
        "schema:name|age", "Rows below.\n  schema:name|age\nThanks.",
        [{"type": "text", "text": "Rows below."}, {"type": "text", "text": "schema:name|age"}],
    ], ids=["alone", "a-line-of-a-longer-prompt", "a-text-part"])
    async def test_the_marker_turns_it_on(self, make_ctx, system):
        assert "schema:name|age" in await self._run(make_ctx, system)


@pytest.mark.asyncio
class TestBatchOwnerRecord:
    """/v1/batch/results serves a result only to the tenant recorded as its owner. The record
    was written after the request was queued, for one hour, and a failed write was ignored,
    so a result stored later than that (a provider batch has a 24h window) had no owner."""

    @staticmethod
    def _redis(calls, set_error=None):
        redis = AsyncMock()
        redis.xlen = AsyncMock(return_value=0)

        async def _set(key, value, ex=None):
            if set_error:
                raise set_error
            calls.append(("set", key, value, ex))

        async def _xadd(*a, **k):
            calls.append(("xadd",))
        redis.set = AsyncMock(side_effect=_set)
        redis.xadd = AsyncMock(side_effect=_xadd)
        return redis

    async def _defer(self, make_ctx, monkeypatch, redis):
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", {"classification"})
        ctx = make_ctx([{"role": "user", "content": "Classify this text."}],
                       params={"batch_topic": "classification"})
        ctx.tenant_id = "acme"
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            return await g13_batch.G13Batch().process_request(ctx)

    async def test_the_owner_is_recorded_before_the_request_is_queued(self, make_ctx, monkeypatch):
        calls = []
        ctx = await self._defer(make_ctx, monkeypatch, self._redis(calls))
        assert ctx.batch_deferred is True
        assert [c[0] for c in calls] == ["set", "xadd"]
        assert calls[0][1:3] == (f"tok_opt:batch_owner:{ctx.request_id}", "acme")

    async def test_the_owner_outlives_a_full_provider_batch_window(self, make_ctx, monkeypatch):
        from middleware import g13_batch
        calls = []
        await self._defer(make_ctx, monkeypatch, self._redis(calls))
        owner_writes = [c for c in calls if c[0] == "set"]
        assert owner_writes and owner_writes[0][3] >= 24 * 3600 + g13_batch._RESULT_TTL

    async def test_a_request_whose_owner_cannot_be_recorded_is_answered_now(
            self, make_ctx, monkeypatch):
        calls = []
        redis = self._redis(calls, set_error=ConnectionError("redis blip"))
        ctx = await self._defer(make_ctx, monkeypatch, redis)
        assert not ctx.batch_deferred
        assert calls == []  # never queued


class _TtlRedis:
    """Keys and their TTLs only: -2 for a missing key, -1 for one with no expiry."""

    def __init__(self, ttls=None):
        self.ttls = dict(ttls or {})

    async def set(self, key, value, ex=None):
        self.ttls[key] = ex if ex else -1

    async def eval(self, script, numkeys, key, value, ttl):
        self.ttls[key] = int(ttl)
        return 1

    async def ttl(self, key):
        return self.ttls.get(key, -2)

    async def expire(self, key, seconds):
        if key not in self.ttls:
            return False
        self.ttls[key] = int(seconds)
        return True


@pytest.mark.asyncio
class TestTheOwnerLastsAsLongAsTheResult:
    OWNER = "tok_opt:batch_owner:r1"

    async def _store(self, owner_ttl, status="completed"):
        from middleware import g13_batch
        redis = _TtlRedis({} if owner_ttl is None else {self.OWNER: owner_ttl})
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            await g13_batch._store_batch_result("r1", {"status": status})
        return redis.ttls.get(self.OWNER), g13_batch._RESULT_TTL

    @pytest.mark.parametrize("status", ["completed", "failed"])
    async def test_an_owner_about_to_expire_is_kept_for_the_result(self, status):
        owner_ttl, result_ttl = await self._store(120, status)
        assert owner_ttl == result_ttl

    @pytest.mark.parametrize("ttl", [3 * 86400, -1], ids=["longer", "no-expiry"])
    async def test_a_longer_lived_owner_is_not_shortened(self, ttl):
        owner_ttl, _ = await self._store(ttl)
        assert owner_ttl == ttl

    async def test_a_missing_owner_is_not_invented(self):
        owner_ttl, _ = await self._store(None)
        assert owner_ttl is None


@pytest.mark.asyncio
class TestBatchResultsFailClosed:
    """GET /v1/batch/results/{id} returns a result only to its owner's tenant or an admin
    key. With no owner on record it skipped the check, so any key that knew the id (it is in
    202 bodies, headers and logs) could read another tenant's answer."""

    STORED = {"status": "completed", "response": {"choices": []}}

    async def _poll(self, owner, caller_meta, stored=STORED, owner_error=None):
        import main
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None if stored is None else json.dumps(stored))
        owner_lookup = AsyncMock(side_effect=owner_error, return_value=owner)
        with patch.object(main, "_authenticate", AsyncMock(return_value=("u", "k", caller_meta))), \
                patch("cache.redis_pool.get_redis", return_value=redis), \
                patch("middleware.g13_batch.get_batch_result_owner", owner_lookup):
            return await main.batch_results("r1", MagicMock())

    @pytest.mark.parametrize("stored", [STORED, None], ids=["result", "no-result"])
    async def test_no_owner_on_record_is_not_found(self, stored):
        resp = await self._poll(None, {"tenant_id": "beta"}, stored)
        assert resp.status_code == 404

    async def test_another_tenant_is_not_found(self):
        resp = await self._poll("acme", {"tenant_id": "beta"})
        assert resp.status_code == 404

    async def test_the_owner_reads_its_result(self):
        resp = await self._poll("acme", {"tenant_id": "acme"})
        assert resp.status_code == 200 and json.loads(resp.body)["status"] == "completed"

    async def test_an_admin_key_reads_any_result(self):
        resp = await self._poll(None, {"tenant_id": "ops", "admin": True})
        assert resp.status_code == 200

    async def test_an_owner_lookup_that_fails_is_an_error_not_a_pass(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await self._poll(None, {"tenant_id": "beta"}, owner_error=ConnectionError("blip"))
        assert exc.value.status_code == 500

    async def test_an_unreadable_owner_record_raises_rather_than_reading_as_none(self):
        from middleware import g13_batch
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=ConnectionError("blip"))
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(ConnectionError):
                await g13_batch.get_batch_result_owner("r1")


@pytest.mark.asyncio
class TestBatchStreamsAreTenantScoped:
    """Each tenant's queued requests go on its own stream (its Redis prefix + the topic), so
    one tenant's backlog cannot fill a topic for every other tenant, and tenants' prompts do
    not sit in one shared stream. The default tenant's prefix is empty: its stream is the old
    shared key, so what was queued there before still drains. The consumer finds the tenant
    streams through a registry set per topic, written before the entry."""

    @staticmethod
    def _ctx(make_ctx, prefix):
        ctx = make_ctx([{"role": "user", "content": "hi"}])
        ctx.redis_prefix = prefix
        return ctx

    @staticmethod
    def _redis(queued=0):
        calls = MagicMock()
        redis = AsyncMock()
        redis.xlen = AsyncMock(return_value=queued)
        redis.sadd = AsyncMock(side_effect=lambda *a: calls.sadd(*a))
        redis.xadd = AsyncMock(side_effect=lambda *a, **k: calls.xadd(*a))
        return redis, calls

    async def test_a_request_is_queued_on_its_tenants_own_stream_registered_first(self, make_ctx):
        from middleware.g13_batch import _accumulate
        redis, calls = self._redis()
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            assert await _accumulate(self._ctx(make_ctx, "t:acme:"), "bulk") is True
        assert [c[0] for c in calls.mock_calls] == ["sadd", "xadd"]       # registered first
        assert calls.sadd.call_args.args == ("tok_opt:batch_streams:bulk",
                                             "t:acme:tok_opt:batch:bulk")
        assert calls.xadd.call_args.args[0] == "t:acme:tok_opt:batch:bulk"
        redis.xlen.assert_awaited_once_with("t:acme:tok_opt:batch:bulk")  # backlog per tenant

    async def test_the_default_tenant_keeps_the_shared_key(self, make_ctx):
        from middleware.g13_batch import _accumulate
        redis, calls = self._redis()
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            assert await _accumulate(self._ctx(make_ctx, ""), "bulk") is True
        assert calls.xadd.call_args.args[0] == "tok_opt:batch:bulk"

    async def test_an_unregistered_stream_is_never_written(self, make_ctx):
        from middleware.g13_batch import _accumulate
        redis, calls = self._redis()
        redis.sadd = AsyncMock(side_effect=ConnectionError("redis down"))
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            assert await _accumulate(self._ctx(make_ctx, "t:acme:"), "bulk") is False
        redis.xadd.assert_not_awaited()

    async def test_the_consumer_reads_every_registered_tenant_stream(self, monkeypatch):
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", set())
        flushed = []

        async def flush(topic, items, cfg):
            flushed.append((topic, [i["request_id"] for i in items]))

        monkeypatch.setattr(g13_batch, "_flush_batch", flush)
        registered = [set(), {"t:acme:tok_opt:batch:bulk"}, {"t:acme:tok_opt:batch:bulk"}]
        reads, read_streams = iter([[], [("t:acme:tok_opt:batch:bulk", [
            ("1-0", {"payload": json.dumps({"request_id": "r1"})})])]]), []

        async def smembers(key):
            assert key == "tok_opt:batch_streams:bulk"
            return registered.pop(0) if registered else {"t:acme:tok_opt:batch:bulk"}

        async def xreadgroup(group, consumer, streams, **kwargs):
            read_streams.append(sorted(streams))
            try:
                return next(reads)
            except StopIteration:
                raise asyncio.CancelledError from None

        redis = AsyncMock()
        redis.smembers = smembers
        redis.xreadgroup = xreadgroup
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"],
                                        "max_pending_ack_ms": 10 ** 9}}}
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(asyncio.CancelledError):
                await g13_batch.start_batch_consumer(cfg)
        assert read_streams[0] == ["tok_opt:batch:bulk"]                  # before it registered
        assert read_streams[1] == ["t:acme:tok_opt:batch:bulk", "tok_opt:batch:bulk"]
        groups = {c.args[0] for c in redis.xgroup_create.await_args_list}
        assert groups == {"tok_opt:batch:bulk", "t:acme:tok_opt:batch:bulk"}
        assert flushed == [("bulk", ["r1"])]
        redis.xack.assert_awaited_once_with("t:acme:tok_opt:batch:bulk",
                                            "proxy-batch-consumers", "1-0")

    async def test_the_sweep_runs_on_each_tenant_stream(self, monkeypatch):
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", set())
        swept = []

        async def sweep(redis, topic, group, consumer, batch_cfg, cfg, stream=None):
            swept.append(stream)
            if len(swept) == 2:
                raise asyncio.CancelledError

        async def xreadgroup(*args, **kwargs):
            await asyncio.sleep(0.01)
            return []

        monkeypatch.setattr(g13_batch, "_reclaim_stale_pel", sweep)
        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value={"t:acme:tok_opt:batch:bulk"})
        redis.xreadgroup = xreadgroup
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"],
                                        "max_pending_ack_ms": 1}}}
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(asyncio.CancelledError):
                await g13_batch.start_batch_consumer(cfg)
        assert set(swept) == {"t:acme:tok_opt:batch:bulk", "tok_opt:batch:bulk"}

    async def test_a_failed_registry_read_still_reads_the_streams_already_known(self, monkeypatch):
        import asyncio
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "_CONSUMED_TOPICS", set())
        answers = [{"t:acme:tok_opt:batch:bulk"}, ConnectionError("blip")]
        read_streams = []

        async def smembers(key):
            answer = answers.pop(0) if answers else ConnectionError("blip")
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def xreadgroup(group, consumer, streams, **kwargs):
            read_streams.append(sorted(streams))
            if len(read_streams) == 2:
                raise asyncio.CancelledError
            return []

        redis = AsyncMock()
        redis.smembers = smembers
        redis.xreadgroup = xreadgroup
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"],
                                        "max_pending_ack_ms": 10 ** 9}}}
        with patch("middleware.g13_batch._get_redis", return_value=redis):
            with pytest.raises(asyncio.CancelledError):
                await g13_batch.start_batch_consumer(cfg)
        assert read_streams[1] == ["t:acme:tok_opt:batch:bulk", "tok_opt:batch:bulk"]

    async def test_the_sweep_uses_the_stream_it_is_given(self, monkeypatch):
        from middleware import g13_batch
        redis = AsyncMock()
        redis.xautoclaim = AsyncMock(return_value=["0-0", [], []])
        await g13_batch._reclaim_stale_pel(redis, "bulk", "grp", "c1", {}, {},
                                           stream="t:acme:tok_opt:batch:bulk")
        assert redis.xautoclaim.await_args.args[0] == "t:acme:tok_opt:batch:bulk"
