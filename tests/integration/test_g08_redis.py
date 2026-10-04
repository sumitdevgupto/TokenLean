"""G08's Redis records against a REAL Redis.

The unit tests count commands on a double; this runs the same pipelines against a real
server: one hash per registry tool, each with a TTL, no sorted set, nothing for a caller's
own tool names, and a pruned status read back and applied.

TEST_REDIS_URL must point at a THROWAWAY Redis: the test flushes its database. Skipped
otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
from unittest.mock import patch

import pytest

redis_asyncio = pytest.importorskip("redis.asyncio")

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"),
                                reason="set TEST_REDIS_URL to a throwaway Redis")

_REGISTRY = [{"name": f"tool_{i}", "intents": ["default"]} for i in range(3)]


def test_records_and_lookups_on_a_real_redis(make_ctx):
    from middleware.g08_tool_loading import G08ToolLoading, _tool_usage_ttl_days

    async def scenario():
        client = redis_asyncio.Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
        try:
            await client.flushdb()
            await client.hset("t:acme:tok_opt:tool:manifest:tool_1", "status", "pruned")
            tools = [{"function": {"name": n}} for n in ("tool_0", "tool_1", "tool_2", "caller_tool")]
            ctx = make_ctx([{"role": "user", "content": "do the task"}], params={"tools": tools})
            ctx.redis_prefix = "t:acme:"
            with patch("middleware.g08_tool_loading._load_registry", return_value=_REGISTRY), \
                    patch("middleware.g08_tool_loading._get_redis", return_value=client):
                ctx = await G08ToolLoading().process_request(ctx)
            keys = sorted(await client.keys("*"))
            types = {k: await client.type(k) for k in keys}
            ttls = {k: await client.ttl(k) for k in keys}
            meta = await client.hgetall("t:acme:tok_opt:tool:usage:tool_0:meta")
            return ctx, keys, types, ttls, meta
        finally:
            await client.aclose()

    ctx, keys, types, ttls, meta = asyncio.run(scenario())
    assert [t["function"]["name"] for t in ctx.params["tools"]] == ["tool_0", "tool_2", "caller_tool"]
    assert keys == ["t:acme:tok_opt:tool:manifest:tool_1",
                    "t:acme:tok_opt:tool:usage:tool_0:meta",
                    "t:acme:tok_opt:tool:usage:tool_2:meta"]
    assert set(types.values()) == {"hash"}
    for key in keys[1:]:
        assert 0 < ttls[key] <= _tool_usage_ttl_days() * 86400
    assert meta["total_calls"] == "1" and float(meta["last_used"]) > 0


def test_the_daily_pruning_on_a_real_redis(make_ctx):
    """The pass's scan, day lock, marks, mark expiry and history restart, the call record
    and a named tool's return, on a real server."""
    import time
    from middleware.g08_tool_loading import (
        G08ToolLoading, _inactivity_threshold_days, record_called_tools, run_pruning_pass)
    old = str(time.time() - 40 * 86400)
    config = {"groups": {"G8_tools": {"enabled": True, "pruning": {
        "enabled": True, "dry_run_first": False}}}}

    async def scenario():
        client = redis_asyncio.Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
        try:
            await client.flushdb()
            for key in ("t:acme:tok_opt:tool:usage:tool_0:meta", "tok_opt:tool:usage:tool_1:meta"):
                await client.hset(key, mapping={"first_seen": old, "last_used": str(time.time())})
            with patch("middleware.g08_tool_loading._load_registry", return_value=_REGISTRY):
                first = await run_pruning_pass(lambda: config, redis=client)
                again = await run_pruning_pass(lambda: config, redis=client)
            mark = "t:acme:tok_opt:tool:manifest:tool_0"
            seen = {"status": await client.hget(mark, "status"), "ttl": await client.ttl(mark),
                    "history": await client.hgetall("t:acme:tok_opt:tool:usage:tool_0:meta")}
            ctx = make_ctx([{"role": "user", "content": "do it"}], params={
                "tools": [{"function": {"name": "tool_0"}}],
                "tool_choice": {"type": "function", "function": {"name": "tool_0"}}})
            ctx.redis_prefix = "t:acme:"
            with patch("middleware.g08_tool_loading._load_registry", return_value=_REGISTRY), \
                    patch("middleware.g08_tool_loading._get_redis", return_value=client):
                ctx = await G08ToolLoading().process_request(ctx)
                await record_called_tools(ctx, {"tool_0"})
            seen["mark_after_naming"] = await client.exists(mark)
            seen["after_call"] = await client.hgetall("t:acme:tok_opt:tool:usage:tool_0:meta")
            return first, again, seen
        finally:
            await client.aclose()

    first, again, seen = asyncio.run(scenario())
    assert first["acme"]["pruned"] == ["tool_0"] and first["default"]["pruned"] == ["tool_1"]
    assert again == {}                                            # once a day
    assert seen["status"] == "pruned"
    assert 0 < seen["ttl"] <= _inactivity_threshold_days() * 86400
    assert "first_seen" not in seen["history"]                    # its history restarts
    assert seen["mark_after_naming"] == 0                         # named, so it is back
    assert "last_called" in seen["after_call"] and "first_seen" in seen["after_call"]
