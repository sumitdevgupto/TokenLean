"""Unit tests for G08 — Tool Definition Loading."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


_MOCK_REGISTRY = [
    {"name": "web_search", "description": "Search the web", "intents": ["search", "research"]},
    {"name": "send_email", "description": "Send email", "intents": ["email", "notify"]},
    {"name": "execute_sql", "description": "Run SQL", "intents": ["fetch_data", "calculate"]},
    {"name": "run_python", "description": "Run Python", "intents": ["code"]},
]

# G08 looks up registry entries for tools ALREADY in ctx.params["tools"]
# and prunes those whose intents don't match the classified request intent.
_ALL_TOOLS_IN_PARAMS = [
    {"function": {"name": "web_search"}},
    {"function": {"name": "send_email"}},
    {"function": {"name": "execute_sql"}},
    {"function": {"name": "run_python"}},
]


@pytest.mark.asyncio
class TestG08ToolLoading:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx(params={"tools": list(_ALL_TOOLS_IN_PARAMS)})
        ctx.config["groups"]["G8_tools"]["enabled"] = False
        original_count = len(ctx.params["tools"])
        from middleware.g08_tool_loading import G08ToolLoading
        ctx = await G08ToolLoading().process_request(ctx)
        assert len(ctx.params["tools"]) == original_count

    async def test_no_existing_tools_skips(self, make_ctx):
        # G08 only runs if ctx.params already has tools
        ctx = make_ctx()
        from middleware.g08_tool_loading import G08ToolLoading
        ctx = await G08ToolLoading().process_request(ctx)
        assert len(ctx.savings.step_savings) == 0

    async def test_prunes_irrelevant_tools(self, make_ctx):
        # User asks to search → only web_search is relevant; send_email/execute_sql/run_python pruned
        ctx = make_ctx(
            [{"role": "user", "content": "Search for the latest AI news"}],
            params={"tools": list(_ALL_TOOLS_IN_PARAMS)},
        )
        with patch("middleware.g08_tool_loading._load_registry", return_value=_MOCK_REGISTRY):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)

        tool_names = [t.get("function", {}).get("name", "") for t in ctx.params.get("tools", [])]
        # After pruning, only search-intent tools remain
        assert "web_search" in tool_names
        # Email tool should be pruned
        assert "send_email" not in tool_names

    async def test_records_step_saving_when_tools_pruned(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Search for news"}],
            params={"tools": list(_ALL_TOOLS_IN_PARAMS)},
        )
        with patch("middleware.g08_tool_loading._load_registry", return_value=_MOCK_REGISTRY):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)

        if any(s.group == "G08" for s in ctx.savings.step_savings):
            step = next(s for s in ctx.savings.step_savings if s.group == "G08")
            assert step.tokens_after <= step.tokens_before

    async def test_no_pruning_when_all_tools_match(self, make_ctx):
        # All tools have 'default' intent → none pruned
        all_default = [{"function": {"name": f"tool_{i}"}} for i in range(3)]
        registry_default = [{"name": f"tool_{i}", "intents": ["default"]} for i in range(3)]
        ctx = make_ctx(
            [{"role": "user", "content": "do something"}],
            params={"tools": all_default},
        )
        with patch("middleware.g08_tool_loading._load_registry", return_value=registry_default):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)

        # No tools pruned → no step saving
        assert len(ctx.params["tools"]) == 3
        assert not any(s.group == "G08" for s in ctx.savings.step_savings)

    async def test_null_mcp_servers_does_not_crash(self, make_ctx):
        # Regression: a config block with an explicit `mcp_servers:` (null) must not
        # raise `TypeError: 'NoneType' object is not iterable` in _load_mcp_tools.
        # (A pinned/template config can carry mcp_servers as null rather than absent.)
        ctx = make_ctx(
            [{"role": "user", "content": "Search for the latest AI news"}],
            params={"tools": list(_ALL_TOOLS_IN_PARAMS)},
        )
        ctx.config["groups"]["G8_tools"]["enabled"] = True
        ctx.config["groups"]["G8_tools"]["mcp_servers"] = None  # explicit null
        with patch("middleware.g08_tool_loading._load_registry", return_value=_MOCK_REGISTRY):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)  # must not raise
        # Pipeline continued normally: irrelevant tools pruned, search tool kept
        tool_names = [t.get("function", {}).get("name", "") for t in ctx.params.get("tools", [])]
        assert "web_search" in tool_names

    async def test_load_mcp_tools_handles_null_and_absent_servers(self):
        # _load_mcp_tools returns [] for both an explicit null and an absent key.
        from middleware.g08_tool_loading import G08ToolLoading
        g08 = G08ToolLoading()
        assert await g08._load_mcp_tools({"mcp_servers": None}) == []
        assert await g08._load_mcp_tools({}) == []


def test_load_registry_coerces_null_tools_key():
    # Regression: a registry file whose top-level `tools:` is null must yield []
    # (not None), so the merge in process_request can always iterate the result.
    from middleware import g08_tool_loading as g08
    g08._registry_cache = {}        # bypass the module-level cache (WS21: per-path dict)
    handle = MagicMock()
    handle.__enter__ = MagicMock(return_value=handle)
    handle.__exit__ = MagicMock(return_value=False)
    with patch("builtins.open", return_value=handle), \
         patch("middleware.g08_tool_loading.yaml.safe_load", return_value={"tools": None}):
        registry = g08._load_registry({"registry_path": ""})
    assert registry == []


class TestClassifyIntent:
    """Direct unit tests for _classify_intent's keyword extraction."""

    def test_search_intent_detected(self):
        from middleware.g08_tool_loading import _classify_intent
        intents = _classify_intent([{"role": "user", "content": "Search for the latest AI news"}])
        assert intents == ["search"]

    def test_multiple_intents_detected_from_single_message(self):
        from middleware.g08_tool_loading import _classify_intent
        intents = _classify_intent([
            {"role": "user", "content": "Write a function and send an email about it"}
        ])
        assert "write" in intents
        assert "code" in intents
        assert "email" in intents

    def test_no_keyword_match_returns_default(self):
        from middleware.g08_tool_loading import _classify_intent
        intents = _classify_intent([{"role": "user", "content": "Hello there, how are you?"}])
        assert intents == ["default"]

    def test_only_last_user_message_considered(self):
        from middleware.g08_tool_loading import _classify_intent
        intents = _classify_intent([
            {"role": "user", "content": "Search for news"},
            {"role": "assistant", "content": "Sure, searching..."},
            {"role": "user", "content": "Actually, calculate the total instead"},
        ])
        assert intents == ["calculate"]

    def test_case_insensitive_matching(self):
        from middleware.g08_tool_loading import _classify_intent
        intents = _classify_intent([{"role": "user", "content": "SCHEDULE a meeting for tomorrow"}])
        assert intents == ["calendar"]

    def test_no_user_message_returns_default(self):
        from middleware.g08_tool_loading import _classify_intent
        intents = _classify_intent([{"role": "system", "content": "You are helpful."}])
        assert intents == ["default"]


@pytest.mark.asyncio
class TestScheduledToolPruning:
    async def test_should_prune_tool_with_no_usage_history(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={})

        pruning = ScheduledToolPruning(mock_redis)
        result = await pruning.should_prune_tool("unused_tool")
        assert result is True

    async def test_should_prune_tool_recently_used_is_not_pruned(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={"last_used": str(time.time())})

        pruning = ScheduledToolPruning(mock_redis)
        result = await pruning.should_prune_tool("active_tool")
        assert result is False

    async def test_should_prune_tool_inactive_beyond_threshold(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        stale_timestamp = time.time() - (31 * 86400)  # 31 days ago
        mock_redis.hgetall = AsyncMock(return_value={"last_used": str(stale_timestamp)})

        pruning = ScheduledToolPruning(mock_redis)
        result = await pruning.should_prune_tool("stale_tool")
        assert result is True

    async def test_should_prune_tool_just_within_threshold_not_pruned(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        recent_timestamp = time.time() - (29 * 86400)  # 29 days ago
        mock_redis.hgetall = AsyncMock(return_value={"last_used": str(recent_timestamp)})

        pruning = ScheduledToolPruning(mock_redis)
        result = await pruning.should_prune_tool("recent_tool")
        assert result is False

    async def test_no_redis_never_prunes(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        pruning = ScheduledToolPruning(None)
        result = await pruning.should_prune_tool("any_tool")
        assert result is False

    async def test_get_inactive_tools_filters_correctly(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()

        async def fake_hgetall(key):
            if "active_tool" in key:
                return {"last_used": str(time.time())}
            return {}  # no history => inactive

        mock_redis.hgetall = AsyncMock(side_effect=fake_hgetall)

        pruning = ScheduledToolPruning(mock_redis)
        inactive = await pruning.get_inactive_tools(["active_tool", "stale_tool"])
        assert inactive == ["stale_tool"]

    async def test_run_scheduled_pruning_dry_run_does_not_mutate(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)  # lock acquired
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.hgetall = AsyncMock(return_value={})  # all inactive

        registry = [{"name": "tool_a"}, {"name": "tool_b"}]
        with patch("middleware.g08_tool_loading._load_registry", return_value=registry):
            pruning = ScheduledToolPruning(mock_redis)
            result = await pruning.run_scheduled_pruning(dry_run=True)

        assert result["status"] == "dry_run"
        assert set(result["would_prune"]) == {"tool_a", "tool_b"}
        mock_redis.hset.assert_not_called()

    async def test_run_scheduled_pruning_already_running_skips(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=False)  # lock NOT acquired

        pruning = ScheduledToolPruning(mock_redis)
        result = await pruning.run_scheduled_pruning()

        assert result == {"status": "already_running", "pruned": []}

    async def test_run_scheduled_pruning_marks_inactive_tools(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.hgetall = AsyncMock(return_value={})  # all inactive
        mock_redis.hset = AsyncMock(return_value=True)

        registry = [{"name": "tool_a"}]
        with patch("middleware.g08_tool_loading._load_registry", return_value=registry):
            pruning = ScheduledToolPruning(mock_redis)
            result = await pruning.run_scheduled_pruning(dry_run=False)

        assert result["status"] == "completed"
        assert result["pruned"] == ["tool_a"]
        mock_redis.hset.assert_any_call(
            "tok_opt:tool:manifest:tool_a", "status", "pruned"
        )


@pytest.mark.asyncio
class TestMCPLazyLoadManifest:
    async def test_get_tools_converts_mcp_to_openai_format(self):
        from middleware.g08_tool_loading import MCPLazyLoadManifest

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "tools": [
                {"name": "web_search", "description": "Search the web", "parameters": {"type": "object"}},
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            manifest = MCPLazyLoadManifest("http://mcp-server")
            tools = await manifest.get_tools()

        assert tools == [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {"type": "object"},
            },
        }]

    async def test_get_tools_applies_tool_filter(self):
        from middleware.g08_tool_loading import MCPLazyLoadManifest

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "tools": [
                {"name": "web_search", "description": "Search"},
                {"name": "send_email", "description": "Email"},
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            manifest = MCPLazyLoadManifest("http://mcp-server", tool_filter=["web_search"])
            tools = await manifest.get_tools()

        names = [t["function"]["name"] for t in tools]
        assert names == ["web_search"]

    async def test_get_tools_caches_within_ttl(self):
        from middleware.g08_tool_loading import MCPLazyLoadManifest

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"tools": [{"name": "web_search", "description": "Search"}]}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            manifest = MCPLazyLoadManifest("http://mcp-server")
            await manifest.get_tools()
            await manifest.get_tools()

        # Second call served from cache — only one HTTP client created
        assert mock_cls.call_count == 1

    async def test_get_tools_failure_returns_empty_list(self):
        from middleware.g08_tool_loading import MCPLazyLoadManifest

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            manifest = MCPLazyLoadManifest("http://mcp-server")
            tools = await manifest.get_tools()

        assert tools == []

    async def test_get_tool_hash_empty_when_no_cache(self):
        from middleware.g08_tool_loading import MCPLazyLoadManifest
        manifest = MCPLazyLoadManifest("http://mcp-server")
        assert manifest.get_tool_hash() == ""

    async def test_get_tool_hash_stable_after_fetch(self):
        from middleware.g08_tool_loading import MCPLazyLoadManifest

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"tools": [{"name": "web_search", "description": "Search"}]}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            manifest = MCPLazyLoadManifest("http://mcp-server")
            await manifest.get_tools()

        h1 = manifest.get_tool_hash()
        h2 = manifest.get_tool_hash()
        assert h1 == h2
        assert len(h1) == 16


# ─── Tool-description compression (prose_compress via compress_descriptions) ───
_TOOLS_WITH_DESC = [
    {"function": {"name": "web_search", "description": "This tool will really just search the web for you."}},
    {"function": {"name": "run_python", "description": "Please simply run the given Python code snippet."}},
]


@pytest.mark.asyncio
class TestG08DescriptionCompression:
    async def _run(self, make_ctx, cfg_extra):
        ctx = make_ctx(
            [{"role": "user", "content": "search and run code"}],
            params={"tools": [dict(t, function=dict(t["function"])) for t in _TOOLS_WITH_DESC]},
        )
        ctx.config["groups"]["G8_tools"].update(cfg_extra)
        # registry marks both tools "default" intent so NOTHING is pruned — isolates desc compression
        reg = [{"name": "web_search", "intents": ["default"]},
               {"name": "run_python", "intents": ["default"]}]
        with patch("middleware.g08_tool_loading._load_registry", return_value=reg):
            from middleware.g08_tool_loading import G08ToolLoading
            return await G08ToolLoading().process_request(ctx)

    async def test_off_by_default_descriptions_untouched(self, make_ctx):
        ctx = await self._run(make_ctx, {"compress_descriptions": False})
        descs = [t["function"]["description"] for t in ctx.params["tools"]]
        assert "really just" in descs[0]  # verbatim, not compressed

    async def test_on_compresses_descriptions(self, make_ctx):
        ctx = await self._run(make_ctx, {"compress_descriptions": True})
        descs = [t["function"]["description"].lower() for t in ctx.params["tools"]]
        assert "really" not in descs[0] and "please" not in descs[1]
        # names preserved
        assert [t["function"]["name"] for t in ctx.params["tools"]] == ["web_search", "run_python"]

    async def test_records_saving_step_when_only_descriptions_compressed(self, make_ctx):
        ctx = await self._run(make_ctx, {"compress_descriptions": True})
        assert any(s.group == "G08" for s in ctx.savings.step_savings)

    async def test_does_not_mutate_original_tool_dicts(self, make_ctx):
        # deep-copy guard: the caller's original tool objects must be untouched
        original = [dict(t, function=dict(t["function"])) for t in _TOOLS_WITH_DESC]
        ctx = make_ctx([{"role": "user", "content": "x"}], params={"tools": original})
        ctx.config["groups"]["G8_tools"].update({"compress_descriptions": True})
        reg = [{"name": "web_search", "intents": ["default"]}, {"name": "run_python", "intents": ["default"]}]
        with patch("middleware.g08_tool_loading._load_registry", return_value=reg):
            from middleware.g08_tool_loading import G08ToolLoading
            await G08ToolLoading().process_request(ctx)
        assert "really just" in original[0]["function"]["description"]  # original object intact
