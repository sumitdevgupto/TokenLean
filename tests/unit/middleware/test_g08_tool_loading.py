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

    @pytest.mark.parametrize("choice", [
        {"type": "function", "function": {"name": "send_email"}},
        {"type": "function", "name": "send_email"},
    ])
    async def test_the_tool_in_tool_choice_is_never_pruned(self, make_ctx, choice):
        """Pruning it turns a valid request into a provider 400: tool_choice names a tool
        that is not in tools."""
        ctx = make_ctx([{"role": "user", "content": "Search for the latest AI news"}],
                       params={"tools": list(_ALL_TOOLS_IN_PARAMS), "tool_choice": choice})
        with patch("middleware.g08_tool_loading._load_registry", return_value=_MOCK_REGISTRY):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)
        names = [t["function"]["name"] for t in ctx.params["tools"]]
        assert "send_email" in names and "run_python" not in names

    async def test_a_tool_the_conversation_already_called_is_never_pruned(self, make_ctx):
        messages = [
            {"role": "user", "content": "Email Dana the report"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "send_email", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "sent"},
            {"role": "user", "content": "Now search for the latest AI news"},
        ]
        ctx = make_ctx(messages, params={"tools": list(_ALL_TOOLS_IN_PARAMS)})
        with patch("middleware.g08_tool_loading._load_registry", return_value=_MOCK_REGISTRY):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)
        names = [t["function"]["name"] for t in ctx.params["tools"]]
        assert "send_email" in names and "run_python" not in names

    async def test_the_legacy_function_call_name_is_never_pruned(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Search for the latest AI news"}],
                       params={"tools": list(_ALL_TOOLS_IN_PARAMS),
                               "function_call": {"name": "run_python"}})
        with patch("middleware.g08_tool_loading._load_registry", return_value=_MOCK_REGISTRY):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)
        names = [t["function"]["name"] for t in ctx.params["tools"]]
        assert "run_python" in names and "send_email" not in names

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


class TestRegistryPathWithNoBucket:
    """2026-09-18. The template ships `gs://${CONFIG_GCS_BUCKET}/config/tool-registry.yaml`,
    so every deploy without that env var loads `gs:///config/tool-registry.yaml`. Building a
    GCS client for an empty bucket ran Google's credential discovery - a ~3 s metadata-server
    wait, synchronous on the request path, once per worker per cache TTL - before falling back
    to the local file. It was the 3.3-3.5 s G08 stage in the local latency dashboards."""

    @staticmethod
    def _local_registry_file():
        handle = MagicMock()
        handle.__enter__ = MagicMock(return_value=handle)
        handle.__exit__ = MagicMock(return_value=False)
        return handle

    def test_an_empty_bucket_never_constructs_a_gcs_client(self):
        from middleware import g08_tool_loading as g08
        g08._registry_cache = {}
        with patch("google.cloud.storage.Client") as client, \
             patch("builtins.open", return_value=self._local_registry_file()), \
             patch("middleware.g08_tool_loading.yaml.safe_load", return_value={"tools": _MOCK_REGISTRY}), \
             patch.object(g08.logger, "warning") as warning:
            registry = g08._load_registry({"registry_path": "gs:///config/tool-registry.yaml"})
        assert registry == _MOCK_REGISTRY, "the local registry must be served"
        client.assert_not_called()
        # Not a failure, so not a WARNING: the old path logged "could not load" every TTL.
        warning.assert_not_called()

    def test_a_real_bucket_still_reads_gcs(self):
        """The GCP path must be untouched - only the bucket-less path changed."""
        from middleware import g08_tool_loading as g08
        g08._registry_cache = {}
        with patch("google.cloud.storage.Client") as client, \
             patch("middleware.g08_tool_loading.yaml.safe_load", return_value={"tools": _MOCK_REGISTRY}):
            client.return_value.bucket.return_value.blob.return_value.download_as_text.return_value = "x"
            registry = g08._load_registry({"registry_path": "gs://acme-config/config/tool-registry.yaml"})
        client.assert_called_once()
        client.return_value.bucket.assert_called_once_with("acme-config")
        client.return_value.bucket.return_value.blob.assert_called_once_with("config/tool-registry.yaml")
        assert registry == _MOCK_REGISTRY


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


_DAY = 86400


def _meta(now, first=None, offered=None, called=None):
    """A usage record: first offered, last offered and last called, in days before now."""
    meta = {}
    if first is not None:
        meta["first_seen"] = str(now - first * _DAY)
    if offered is not None:
        meta["last_used"] = str(now - offered * _DAY)
    if called is not None:
        meta["last_called"] = str(now - called * _DAY)
    return meta


@pytest.mark.asyncio
class TestScheduledToolPruning:
    """The job prunes a registry tool the model has stopped calling: offered for the whole
    window (inactivity_threshold_days, 30), still offered within it, and not called within
    it. Until 2026-10-02 a tool with NO record was pruned, nothing ever cleared a mark, and
    the record was of tools offered rather than called, so the first run would have hidden
    every tool a tenant had not sent yet, for good, and saved nothing."""

    @staticmethod
    async def _verdict(meta):
        from middleware.g08_tool_loading import ScheduledToolPruning
        redis = _RecordingRedis()
        if meta is not None:
            redis.hashes["tok_opt:tool:usage:tool_x:meta"] = meta
        return await ScheduledToolPruning(redis).should_prune_tool("tool_x")

    @pytest.mark.parametrize("record,expected", [
        (lambda now: None, False),
        (lambda now: {"last_used": str(now)}, False),
        (lambda now: _meta(now, first=31, offered=0.1), True),
        (lambda now: _meta(now, first=40, offered=0.1, called=31), True),
        (lambda now: _meta(now, first=40, offered=0.1, called=29), False),
        (lambda now: _meta(now, first=29, offered=0.1), False),
        (lambda now: _meta(now, first=60, offered=31), False),
    ], ids=["no-record", "record-without-first-offer", "never-called", "called-31-days-ago",
            "called-29-days-ago", "offered-under-30-days", "no-longer-offered"])
    async def test_which_tools_are_candidates(self, record, expected):
        assert await self._verdict(record(time.time())) is expected

    async def test_no_redis_never_prunes(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        assert await ScheduledToolPruning(None).should_prune_tool("any_tool") is False

    @staticmethod
    def _redis_with(**records):
        redis = _RecordingRedis()
        for name, meta in records.items():
            redis.hashes[f"t:acme:tok_opt:tool:usage:{name}:meta"] = meta
        return redis

    async def test_a_dry_run_reports_and_writes_nothing(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        now = time.time()
        redis = self._redis_with(stale=_meta(now, first=40, offered=0.1),
                                 busy=_meta(now, first=40, offered=0.1, called=1))
        result = await ScheduledToolPruning(redis).run_scheduled_pruning(
            dry_run=True, prefix="t:acme:", registry_tools=["stale", "busy", "unsent"])
        assert result == {"status": "dry_run", "would_prune": ["stale"]}
        assert not any(":tool:manifest:" in key for key in redis.hashes)
        assert "first_seen" in redis.hashes["t:acme:tok_opt:tool:usage:stale:meta"]
        assert "t:acme:tok_opt:tool:pruning_lock" not in redis.strings     # lock released

    async def test_applying_marks_the_tool_and_the_mark_lapses_with_its_history(self):
        from middleware.g08_tool_loading import ScheduledToolPruning, _inactivity_threshold_days
        now = time.time()
        redis = self._redis_with(stale=_meta(now, first=40, offered=0.1))
        result = await ScheduledToolPruning(redis).run_scheduled_pruning(
            prefix="t:acme:", registry_tools=["stale"])
        assert result["status"] == "completed" and result["pruned"] == ["stale"]
        mark = "t:acme:tok_opt:tool:manifest:stale"
        assert redis.hashes[mark]["status"] == "pruned" and "pruned_at" in redis.hashes[mark]
        assert redis.ttl.get(mark) == _inactivity_threshold_days() * _DAY  # the mark lapses
        # ... and the tool's history starts again from its next offer
        assert "first_seen" not in redis.hashes["t:acme:tok_opt:tool:usage:stale:meta"]

    async def test_only_the_tools_of_the_registry_given_are_considered(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        now = time.time()
        redis = self._redis_with(stale=_meta(now, first=40, offered=0.1),
                                 other=_meta(now, first=40, offered=0.1))
        result = await ScheduledToolPruning(redis).run_scheduled_pruning(
            dry_run=True, prefix="t:acme:", registry_tools=["other"])
        assert result["would_prune"] == ["other"]

    async def test_a_run_already_in_progress_is_left_alone(self):
        from middleware.g08_tool_loading import ScheduledToolPruning
        redis = _RecordingRedis()
        redis.strings["tok_opt:tool:pruning_lock"] = "1"
        result = await ScheduledToolPruning(redis).run_scheduled_pruning(registry_tools=["x"])
        assert result == {"status": "already_running", "pruned": []}


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

    async def test_an_unfaithful_description_compression_is_refused(self, make_ctx):
        # G08 holds description compression to the guard G01 holds every compressor to: a
        # compression that drops a negation would rewrite the tool's contract, so that
        # description is sent as written while a faithful one in the same request is kept.
        def drops_words(text):
            out = text.replace("not ", "").replace("really ", "")
            return {"compressed": out, "before": len(text), "after": len(out)}

        tools = [{"function": {"name": "web_search", "description": "Do not search private sites."}},
                 {"function": {"name": "run_python", "description": "Runs really any Python code."}}]
        ctx = make_ctx([{"role": "user", "content": "x"}], params={"tools": tools})
        ctx.config["groups"]["G8_tools"].update({"compress_descriptions": True})
        reg = [{"name": "web_search", "intents": ["default"]}, {"name": "run_python", "intents": ["default"]}]
        with patch("middleware.g08_tool_loading._load_registry", return_value=reg), \
                patch("middleware.prose_compress.compress", side_effect=drops_words):
            from middleware.g08_tool_loading import G08ToolLoading
            await G08ToolLoading().process_request(ctx)
        descs = [t["function"]["description"] for t in ctx.params["tools"]]
        assert descs == ["Do not search private sites.", "Runs any Python code."]


# ── Redis cost per request (usage records and pruned status) ──────────────────
class _RecordingRedis:
    """A dict-backed Redis double that logs every round trip and the commands in it. A
    command sent outside a pipeline is logged as its own round trip, whatever it is."""

    def __init__(self):
        self.hashes = {}
        self.strings = {}
        self.ttl = {}
        self.round_trips = []

    def pipeline(self, transaction=True):
        return _RecordingPipeline(self)

    def apply(self, op, key, *args, **kwargs):
        if op == "hset":
            self.hashes.setdefault(key, {})[args[0]] = args[1]
            return 1
        if op == "hsetnx":
            h = self.hashes.setdefault(key, {})
            if args[0] in h:
                return 0
            h[args[0]] = args[1]
            return 1
        if op == "hdel":
            h = self.hashes.get(key, {})
            return sum(1 for f in args if h.pop(f, None) is not None)
        if op == "hincrby":
            h = self.hashes.setdefault(key, {})
            h[args[0]] = str(int(h.get(args[0], 0)) + args[1])
            return int(h[args[0]])
        if op == "expire":
            self.ttl[key] = args[0]
            return True
        if op == "hget":
            return self.hashes.get(key, {}).get(args[0])
        if op == "hgetall":
            return dict(self.hashes.get(key, {}))
        if op == "set":
            if kwargs.get("nx") and key in self.strings:
                return None
            self.strings[key] = args[0]
            if kwargs.get("ex"):
                self.ttl[key] = kwargs["ex"]
            return True
        if op == "delete":
            gone = [k for k in (key, *args) if k in self.hashes or k in self.strings]
            for k in gone:
                self.hashes.pop(k, None)
                self.strings.pop(k, None)
            return len(gone)
        raise AssertionError(f"unexpected Redis command {op}")

    async def scan_iter(self, match="*", count=None):
        import fnmatch
        self.round_trips.append([("scan", match)])
        for key in sorted(set(self.hashes) | set(self.strings)):
            if fnmatch.fnmatchcase(key, match):
                yield key

    def __getattr__(self, op):
        async def command(key=None, *args, **kwargs):
            self.round_trips.append([(op, key)])
            return self.apply(op, key, *args, **kwargs)
        return command


class _RecordingPipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def __getattr__(self, op):
        def queue(key, *args, **kwargs):
            self.ops.append((op, key, args, kwargs))
            return self
        return queue

    async def execute(self):
        self.redis.round_trips.append([(op, key) for op, key, _, _ in self.ops])
        return [self.redis.apply(op, key, *args, **kwargs)
                for op, key, args, kwargs in self.ops]


_REGISTRY_20 = [{"name": f"tool_{i}", "intents": ["default"]} for i in range(20)]


@pytest.mark.asyncio
class TestRedisCostPerRequest:
    """Regression: every kept tool on every request added a sorted-set member kept 90 days
    and read by nothing, in five sequential writes plus a pruned-status read; the per-tool
    record never expired, and the caller's tool names became keys."""

    async def _run(self, make_ctx, tools, registry=_REGISTRY_20, redis=None):
        redis = redis or _RecordingRedis()
        ctx = make_ctx([{"role": "user", "content": "do the task"}], params={"tools": tools})
        ctx.redis_prefix = "t:acme:"
        with patch("middleware.g08_tool_loading._load_registry", return_value=registry), \
                patch("middleware.g08_tool_loading._get_redis", return_value=redis):
            from middleware.g08_tool_loading import G08ToolLoading
            ctx = await G08ToolLoading().process_request(ctx)
        return ctx, redis

    async def test_twenty_tools_cost_two_round_trips_and_no_sorted_set(self, make_ctx):
        tools = [{"function": {"name": f"tool_{i}"}} for i in range(20)]
        ctx, redis = await self._run(make_ctx, tools)
        assert len(redis.round_trips) == 2          # one status lookup, one usage record
        commands = {op for trip in redis.round_trips for op, _ in trip}
        assert commands == {"hget", "hset", "hsetnx", "hincrby", "expire"}
        assert len(ctx.params["tools"]) == 20

    async def test_the_usage_record_expires(self, make_ctx):
        from middleware.g08_tool_loading import _tool_usage_ttl_days
        _, redis = await self._run(make_ctx, [{"function": {"name": "tool_3"}}])
        meta = "t:acme:tok_opt:tool:usage:tool_3:meta"
        assert redis.hashes.get(meta, {}).get("total_calls") == "1"
        assert redis.ttl.get(meta) == _tool_usage_ttl_days() * 86400

    async def test_a_callers_own_tool_names_never_reach_redis(self, make_ctx):
        tools = [{"function": {"name": f"caller_tool_{i}"}} for i in range(50)]
        ctx, redis = await self._run(make_ctx, tools)
        assert redis.round_trips == []
        assert len(ctx.params["tools"]) == 50       # unknown tools are still kept

    async def test_a_pruned_tool_is_dropped_after_one_lookup(self, make_ctx):
        redis = _RecordingRedis()
        redis.hashes["t:acme:tok_opt:tool:manifest:tool_1"] = {"status": "pruned"}
        redis.hashes["t:acme:tok_opt:tool:manifest:tool_2"] = {"status": "active"}
        tools = [{"function": {"name": n}} for n in ("tool_0", "tool_1", "tool_2")]
        ctx, redis = await self._run(make_ctx, tools, redis=redis)
        assert [t["function"]["name"] for t in ctx.params["tools"]] == ["tool_0", "tool_2"]
        lookups = [trip for trip in redis.round_trips if trip[0][0] == "hget"]
        assert len(lookups) == 1 and len(lookups[0]) == 3

    async def test_what_is_recorded_is_what_the_pruning_check_reads(self, make_ctx):
        from middleware.g08_tool_loading import ScheduledToolPruning
        _, redis = await self._run(make_ctx, [{"function": {"name": "tool_5"}}])
        pruning = ScheduledToolPruning(redis)
        assert await pruning.should_prune_tool("tool_5", prefix="t:acme:") is False  # new
        meta = redis.hashes["t:acme:tok_opt:tool:usage:tool_5:meta"]
        meta["first_seen"] = str(time.time() - 31 * 86400)       # first offered 31 days ago
        assert await pruning.should_prune_tool("tool_5", prefix="t:acme:") is True
        assert await pruning.should_prune_tool("tool_6", prefix="t:acme:") is False  # no record

    async def test_a_redis_failure_leaves_the_request_alone(self, make_ctx):
        class _Broken(_RecordingRedis):
            def pipeline(self, transaction=True):
                raise ConnectionError("redis down")

        tools = [{"function": {"name": f"tool_{i}"}} for i in range(3)]
        ctx, _ = await self._run(make_ctx, tools, redis=_Broken())
        assert len(ctx.params["tools"]) == 3


# ── The pruning signal: which offered tools the model called ─────────────────
async def _g08_request(make_ctx, tools, redis=None, params=None, registry=_REGISTRY_20):
    redis = redis or _RecordingRedis()
    ctx = make_ctx([{"role": "user", "content": "do the task"}],
                   params={"tools": tools, **(params or {})})
    ctx.redis_prefix = "t:acme:"
    with patch("middleware.g08_tool_loading._load_registry", return_value=registry), \
            patch("middleware.g08_tool_loading._get_redis", return_value=redis):
        from middleware.g08_tool_loading import G08ToolLoading
        ctx = await G08ToolLoading().process_request(ctx)
    return ctx, redis


def test_the_names_of_the_called_tools_are_read_from_a_response():
    from middleware.g08_tool_loading import called_tool_names
    response = {"choices": [
        {"message": {"tool_calls": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]}},
        {"message": {"function_call": {"name": "c"}}},
        {"message": {"content": "no call"}}]}
    assert called_tool_names(response) == {"a", "b", "c"}
    assert called_tool_names({"choices": None}) == set() == called_tool_names(None)


@pytest.mark.asyncio
class TestCalledToolRecords:
    async def test_only_the_offered_registry_tools_are_recorded(self, make_ctx):
        from middleware.g08_tool_loading import _tool_usage_ttl_days, record_called_tools
        redis = _RecordingRedis()
        ctx = make_ctx()
        ctx.redis_prefix, ctx.g08_offered_tools = "t:acme:", ["tool_1"]
        with patch("middleware.g08_tool_loading._get_redis", return_value=redis):
            await record_called_tools(ctx, {"tool_1", "callers_own"})
        meta = "t:acme:tok_opt:tool:usage:tool_1:meta"
        assert "last_called" in redis.hashes.get(meta, {})
        assert float(redis.hashes[meta]["last_called"]) == pytest.approx(time.time(), abs=60)
        assert redis.ttl[meta] == _tool_usage_ttl_days() * _DAY
        assert not any("callers_own" in key for key in redis.hashes)

    async def test_nothing_offered_costs_no_round_trip(self, make_ctx):
        from middleware.g08_tool_loading import record_called_tools
        redis = _RecordingRedis()
        with patch("middleware.g08_tool_loading._get_redis", return_value=redis):
            await record_called_tools(make_ctx(), {"tool_1"})
        assert redis.round_trips == []

    async def test_the_request_remembers_the_registry_tools_it_offered(self, make_ctx):
        ctx, _ = await _g08_request(make_ctx, [{"function": {"name": "tool_1"}},
                                               {"function": {"name": "callers_own"}}])
        assert ctx.g08_offered_tools == ["tool_1"]

    async def test_the_first_offer_is_kept(self, make_ctx):
        redis = _RecordingRedis()
        await _g08_request(make_ctx, [{"function": {"name": "tool_1"}}], redis=redis)
        meta = redis.hashes["t:acme:tok_opt:tool:usage:tool_1:meta"]
        first = meta["first_seen"]
        await _g08_request(make_ctx, [{"function": {"name": "tool_1"}}], redis=redis)
        assert meta["first_seen"] == first and meta["total_calls"] == "2"

    async def test_a_request_naming_a_pruned_tool_brings_it_back(self, make_ctx):
        redis = _RecordingRedis()
        redis.hashes["t:acme:tok_opt:tool:manifest:tool_1"] = {"status": "pruned"}
        tools = [{"function": {"name": "tool_1"}}]
        choice = {"tool_choice": {"type": "function", "function": {"name": "tool_1"}}}
        ctx, _ = await _g08_request(make_ctx, tools, redis=redis, params=choice)
        assert [t["function"]["name"] for t in ctx.params["tools"]] == ["tool_1"]
        assert "t:acme:tok_opt:tool:manifest:tool_1" not in redis.hashes
        ctx, _ = await _g08_request(make_ctx, tools, redis=redis)    # back for every request
        assert [t["function"]["name"] for t in ctx.params["tools"]] == ["tool_1"]

    async def test_the_response_pipeline_records_the_models_calls(self, make_ctx):
        from middleware.pipeline import OptimisationPipeline
        redis = _RecordingRedis()
        ctx = make_ctx([{"role": "user", "content": "look it up"}])
        ctx.redis_prefix, ctx.g08_offered_tools = "t:acme:", ["lookup"]
        provider = {"id": "c", "object": "chat.completion", "model": "gpt-4o", "choices": [{
            "index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        with patch("middleware.g08_tool_loading._get_redis", return_value=redis), \
                patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock):
            await OptimisationPipeline().process_response(ctx, provider)
        assert "last_called" in redis.hashes.get("t:acme:tok_opt:tool:usage:lookup:meta", {})


# ── The daily pass ──────────────────────────────────────────────────────────
def _pass_config(enabled=True, dry_run_first=True, tenants=None):
    pruning = {"enabled": enabled}
    if dry_run_first is not None:                       # None: the key is not set
        pruning["dry_run_first"] = dry_run_first
    return {"groups": {"G8_tools": {
                "enabled": True, "registry_path": "default-registry", "pruning": pruning}},
            "tenants": tenants or {}}


@pytest.mark.asyncio
class TestDailyPruningPass:
    """Once a day, for every tenant with usage records, against that tenant's registry.
    Report only (log + metric) until pruning.dry_run_first is set to false."""

    @staticmethod
    def _redis():
        now = time.time()
        redis = _RecordingRedis()
        redis.hashes["t:acme:tok_opt:tool:usage:stale:meta"] = _meta(now, first=40, offered=0.1)
        redis.hashes["tok_opt:tool:usage:old_default:meta"] = _meta(now, first=40, offered=0.1)
        return redis

    @staticmethod
    async def _pass(config, redis, registries=None):
        from middleware.g08_tool_loading import run_pruning_pass
        registries = registries or {}
        seen = []

        def load(cfg):
            seen.append(cfg.get("registry_path"))
            names = registries.get(cfg.get("registry_path"), ["stale", "old_default"])
            return [{"name": n} for n in names]

        with patch("middleware.g08_tool_loading._load_registry", side_effect=load):
            result = await run_pruning_pass(lambda: config, redis=redis)
        return result, seen

    async def test_pruning_switched_off_does_nothing(self):
        redis = self._redis()
        result, _ = await self._pass(_pass_config(enabled=False), redis)
        assert result == {} and redis.round_trips == []

    async def test_by_default_it_reports_and_marks_nothing(self, caplog):
        from middleware.g18_observability import TOOL_PRUNING_CANDIDATES
        redis = self._redis()
        with caplog.at_level("INFO", logger="middleware.g08_tool_loading"):
            result, _ = await self._pass(_pass_config(), redis)
        assert result.get("acme") == {"status": "dry_run", "would_prune": ["stale"]}
        assert result.get("default") == {"status": "dry_run", "would_prune": ["old_default"]}
        assert not any(":tool:manifest:" in key for key in redis.hashes)
        assert TOOL_PRUNING_CANDIDATES.labels(tenant_id="acme")._value.get() == 1
        assert "report only" in caplog.text and "stale" in caplog.text

    async def test_with_dry_run_first_off_it_marks(self):
        redis = self._redis()
        result, _ = await self._pass(_pass_config(dry_run_first=False), redis)
        assert result.get("acme", {}).get("pruned") == ["stale"]
        assert redis.hashes["t:acme:tok_opt:tool:manifest:stale"]["status"] == "pruned"

    async def test_without_dry_run_first_set_it_only_reports(self):
        redis = self._redis()
        result, _ = await self._pass(_pass_config(dry_run_first=None), redis)
        assert result.get("acme") == {"status": "dry_run", "would_prune": ["stale"]}
        assert not any(":tool:manifest:" in key for key in redis.hashes)

    async def test_a_tenant_whose_run_is_locked_keeps_its_last_count(self):
        from middleware.g18_observability import TOOL_PRUNING_CANDIDATES
        redis = self._redis()
        redis.strings["t:acme:tok_opt:tool:pruning_lock"] = "1"
        TOOL_PRUNING_CANDIDATES.labels(tenant_id="acme").set(7)
        result, _ = await self._pass(_pass_config(), redis)
        assert result.get("acme") == {"status": "already_running", "pruned": []}
        assert TOOL_PRUNING_CANDIDATES.labels(tenant_id="acme")._value.get() == 7

    async def test_it_runs_once_a_day(self):
        redis = self._redis()
        first, _ = await self._pass(_pass_config(), redis)
        again, _ = await self._pass(_pass_config(), redis)
        assert first and again == {}

    async def test_each_tenant_is_checked_against_its_own_registry(self):
        redis = self._redis()
        config = _pass_config(tenants={
            "acme": {"groups": {"G8_tools": {"registry_path": "acme-registry"}}}})
        result, seen = await self._pass(config, redis, registries={"acme-registry": ["other"]})
        assert sorted(seen) == ["acme-registry", "default-registry"]
        assert result.get("acme", {}).get("would_prune") == []   # 'stale' is not in its registry


@pytest.mark.parametrize("schedule,clock,expected", [
    ("0 2 * * *", "2026-10-02T01:00:00", 3600),
    ("0 2 * * *", "2026-10-02T03:00:00", 23 * 3600),
    ("30 2 * * *", "2026-10-02T02:30:00", 24 * 3600),
    ("*/5 * * * *", "2026-10-02T01:00:00", 24 * 3600),      # not the daily form: every 24 h
    ("", "2026-10-02T01:00:00", 24 * 3600),
    ("0 25 * * *", "2026-10-02T01:00:00", 24 * 3600),
])
def test_seconds_until_the_next_run(schedule, clock, expected):
    from datetime import datetime, timezone
    from middleware.g08_tool_loading import _seconds_until_next_run
    now = datetime.fromisoformat(clock).replace(tzinfo=timezone.utc).timestamp()
    try:
        got = _seconds_until_next_run(schedule, now)
    except Exception as exc:
        pytest.fail(f"{schedule!r} raised {exc!r}")
    assert got == expected


@pytest.mark.asyncio
async def test_the_loop_outlives_a_failing_pass():
    import asyncio
    import middleware.g08_tool_loading as g08
    passes, sleeps = [], []

    async def failing_pass(get_config, redis=None):
        passes.append(1)
        raise RuntimeError("redis down")

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with patch.object(g08, "run_pruning_pass", failing_pass):
        try:
            await g08.run_tool_pruning_loop(lambda: {}, sleep=sleep)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            pytest.fail(f"the loop died: {exc!r}")
    assert len(passes) == 2


def test_the_proxy_starts_the_daily_pruning():
    import ast
    import inspect
    import textwrap
    import main
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.lifespan)))
    started = {ast.unparse(call.args[0].func) for call in ast.walk(tree)
               if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_service_task"
               and call.args and isinstance(call.args[0], ast.Call)}
    assert "run_tool_pruning_loop" in started
