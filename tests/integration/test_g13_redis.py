"""G13's batch stream against a REAL Redis: several consumers on one stream.

The unit tests drive the consumer against a double; this runs the claim, hold and sweep
commands against a real server, where idle times and delivery counts are Redis's own. The
defect being pinned: a flush can outlast `max_pending_ack_ms`, and another instance's sweep
used to take the entries still being worked on and mark them failed, so a polling client
resubmitted and paid twice.

TEST_REDIS_URL must point at a THROWAWAY Redis: the test flushes its database. Skipped
otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import json
from unittest.mock import patch

import pytest

redis_asyncio = pytest.importorskip("redis.asyncio")

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"),
                                reason="set TEST_REDIS_URL to a throwaway Redis")

STREAM, GROUP = "tok_opt:batch:bulk", "proxy-batch-consumers"
STALE_MS = 300


def _run(scenario):
    async def main():
        client = redis_asyncio.Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
        try:
            await client.flushdb()
            await client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
            with patch("middleware.g13_batch._get_redis", return_value=client):
                return await scenario(client)
        finally:
            await client.aclose()
    return asyncio.run(main())


async def _queue(client, request_id):
    await client.xadd(STREAM, {"payload": json.dumps({"request_id": request_id, "messages": []})})


async def _read(client, consumer):
    entries = await client.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=10)
    return [(msg_id, json.loads(fields["payload"])) for _, messages in entries
            for msg_id, fields in messages]


async def _sweep(g13, consumer, max_attempts=3):
    await g13._reclaim_stale_pel(
        g13._get_redis(), "bulk", GROUP, consumer,
        {"max_pending_ack_ms": STALE_MS, "max_attempts": max_attempts}, {})


def test_a_consumer_still_flushing_keeps_its_entries_from_another_sweep():
    from middleware import g13_batch as g13
    flushed = []

    async def flush(topic, items, cfg):
        flushed.extend(item["request_id"] for item in items)
        await asyncio.sleep(4 * STALE_MS / 1000)   # a flush that outlasts the stale time
        for item in items:
            await g13._store_batch_result(item["request_id"], {"status": "completed"})

    async def scenario(client):
        await _queue(client, "r1")
        entries = await _read(client, "instance-a")
        with patch.object(g13, "_flush_batch", flush):
            async def other_instance():
                for _ in range(8):
                    await asyncio.sleep(STALE_MS / 1000 / 2)
                    await _sweep(g13, "instance-b")
            await asyncio.gather(
                g13._flush_held(client, STREAM, GROUP, "instance-a", "bulk", entries, {},
                                STALE_MS),
                other_instance())
        stored = json.loads(await client.get("tok_opt:batch_result:r1"))
        return stored, await client.xpending(STREAM, GROUP)

    stored, pending = _run(scenario)
    assert flushed == ["r1"]                  # flushed once, by the consumer that read it
    assert stored["status"] == "completed"    # never "failed" on the way
    assert pending["pending"] == 0


def test_an_entry_a_stopped_consumer_left_is_retried_by_another():
    from middleware import g13_batch as g13
    flushed = []

    async def flush(topic, items, cfg):
        flushed.extend(item["request_id"] for item in items)
        for item in items:
            await g13._store_batch_result(item["request_id"], {"status": "completed"})

    async def scenario(client):
        await _queue(client, "r1")
        await _read(client, "instance-a")          # read, then the process stops
        await asyncio.sleep(1.5 * STALE_MS / 1000)
        with patch.object(g13, "_flush_batch", flush):
            await _sweep(g13, "instance-b")
        stored = json.loads(await client.get("tok_opt:batch_result:r1"))
        return stored, await client.xpending(STREAM, GROUP), await client.xlen(STREAM)

    stored, pending, length = _run(scenario)
    assert flushed == ["r1"] and stored["status"] == "completed"
    assert pending["pending"] == 0 and length == 0


def test_an_entry_that_keeps_failing_is_marked_failed_after_its_attempts():
    from middleware import g13_batch as g13
    attempts = []

    async def flush(topic, items, cfg):
        attempts.append(len(items))
        raise RuntimeError("the process dies mid-flush")

    async def scenario(client):
        await _queue(client, "r1")
        await _read(client, "instance-a")          # delivery 1
        with patch.object(g13, "_flush_batch", flush):
            for _ in range(4):
                await asyncio.sleep(1.5 * STALE_MS / 1000)
                await _sweep(g13, "instance-b", max_attempts=3)
        return (json.loads(await client.get("tok_opt:batch_result:r1")),
                await client.xpending(STREAM, GROUP))

    stored, pending = _run(scenario)
    assert attempts == [1, 1]                  # deliveries 2 and 3; the 4th gives up
    assert stored["status"] == "failed"
    assert pending["pending"] == 0


def test_a_failure_never_replaces_a_completed_answer():
    from middleware import g13_batch as g13

    async def scenario(client):
        await g13._store_batch_result("r1", {"status": "completed", "response": {"id": "c1"}})
        await g13._store_batch_result("r1", {"status": "failed", "error": "stale"})
        kept = json.loads(await client.get("tok_opt:batch_result:r1"))
        await g13._store_batch_result("r2", {"status": "failed", "error": "provider down"})
        await g13._store_batch_result("r2", {"status": "completed", "response": {"id": "c2"}})
        replaced = json.loads(await client.get("tok_opt:batch_result:r2"))
        await g13._store_batch_result("r3", {"status": "failed", "error": "provider down"})
        first_failure = json.loads(await client.get("tok_opt:batch_result:r3"))
        ttls = [await client.ttl(f"tok_opt:batch_result:{r}") for r in ("r1", "r2", "r3")]
        return kept, replaced, first_failure, ttls

    kept, replaced, first_failure, ttls = _run(scenario)
    assert kept == {"status": "completed", "response": {"id": "c1"}}
    assert replaced["status"] == "completed"   # a later answer still replaces a failure
    assert first_failure["status"] == "failed"
    assert all(0 < t <= g13._RESULT_TTL for t in ttls)


def test_only_consumers_of_stopped_processes_are_deleted():
    from middleware import g13_batch as g13

    async def scenario(client):
        await _queue(client, "r1")
        await _read(client, "stopped-with-work")         # holds r1
        await client.xreadgroup(GROUP, "stopped-idle", {STREAM: ">"}, count=1)
        await asyncio.sleep(0.3)
        await client.xreadgroup(GROUP, "self", {STREAM: ">"}, count=1)
        await g13._drop_stopped_consumers(client, STREAM, GROUP, "self", idle_ms=200)
        return sorted(c["name"] for c in await client.xinfo_consumers(STREAM, GROUP))

    assert _run(scenario) == ["self", "stopped-with-work"]


def test_each_tenant_queues_on_its_own_stream_and_one_consumer_answers_both(make_ctx):
    """Tenants no longer share a topic's stream: each has its own (its Redis prefix), listed
    in the topic's registry set, which the consumer reads."""
    from middleware import g13_batch as g13
    acme = "t:acme:tok_opt:batch:bulk"
    answered = []

    async def flush(topic, items, cfg):
        answered.extend((topic, item["request_id"], item["tenant_id"]) for item in items)

    async def scenario(client):
        for tenant, prefix in (("acme", "t:acme:"), ("default", "")):
            ctx = make_ctx([{"role": "user", "content": "hi"}])
            ctx.tenant_id, ctx.redis_prefix, ctx.request_id = tenant, prefix, f"r-{tenant}"
            assert await g13._accumulate(ctx, "bulk") is True
        registered = sorted(await client.smembers("tok_opt:batch_streams:bulk"))
        queued = {s: await client.xlen(s) for s in (acme, STREAM)}
        cfg = {"groups": {"G13_batch": {"enabled": True, "batch_topics": ["bulk"],
                                        "flush_interval_ms": 50, "max_pending_ack_ms": 10 ** 9}}}
        with patch.object(g13, "_flush_batch", flush):
            consumer = asyncio.create_task(g13.start_batch_consumer(cfg))
            for _ in range(50):
                await asyncio.sleep(0.1)
                if len(answered) == 2:
                    break
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        return registered, queued, {s: await client.xlen(s) for s in (acme, STREAM)}

    registered, queued, left = _run(scenario)
    assert registered == [acme, STREAM]
    assert queued == {acme: 1, STREAM: 1}
    assert sorted(answered) == [("bulk", "r-acme", "acme"), ("bulk", "r-default", "default")]
    assert left == {acme: 0, STREAM: 0}                  # acknowledged and deleted
