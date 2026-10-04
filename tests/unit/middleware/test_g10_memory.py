"""Unit tests for G10 — Conversation & Memory Management."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import asyncio
import json
import subprocess
import threading
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG10Memory:
    async def test_disabled_passes_through(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages)
        ctx.config["groups"]["G10_memory"]["enabled"] = False
        original_count = len(ctx.messages)
        from middleware.g10_memory import G10Memory
        ctx = await G10Memory().process_request(ctx)
        assert len(ctx.messages) == original_count

    async def test_no_session_id_skips(self, make_ctx):
        # Use very few messages so the sliding window doesn't trigger (turns <= window*2)
        ctx = make_ctx([{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}])
        # No session_id — _apply_sliding_window runs but with 2 turns and window=2: 2 <= 2*2 → returns early
        from middleware.g10_memory import G10Memory
        ctx = await G10Memory().process_request(ctx)
        # Nothing trimmed (too few turns)
        assert len(ctx.messages) == 2

    async def test_sliding_window_truncates_old_turns(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages, params={"x_session_id": "session-123"})
        ctx.config["groups"]["G10_memory"]["sliding_window_turns"] = 2
        tokens_before = ctx.current_token_count

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)   # new session
        mock_redis.set = AsyncMock()
        mock_redis.expire = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g10_memory._get_redis", return_value=mock_redis):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock,
                       return_value="Summary of old turns."):
                from middleware.g10_memory import G10Memory
                ctx = await G10Memory().process_request(ctx)

        tokens_after = ctx.current_token_count
        assert tokens_after <= tokens_before

    async def test_sliding_window_records_step_saving(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages, params={"x_session_id": "session-456"})
        ctx.config["groups"]["G10_memory"]["sliding_window_turns"] = 1

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()
        mock_redis.expire = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("middleware.g10_memory._get_redis", return_value=mock_redis):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock,
                       return_value="Short summary."):
                from middleware.g10_memory import G10Memory
                ctx = await G10Memory().process_request(ctx)

        if any(s.group == "G10" for s in ctx.savings.step_savings):
            step = next(s for s in ctx.savings.step_savings if s.group == "G10")
            assert step.tokens_after <= step.tokens_before

    async def test_redis_error_fallback(self, make_ctx, long_messages):
        ctx = make_ctx(long_messages, params={"x_session_id": "session-789"})
        # Redis fails → falls back to _apply_sliding_window; _summarise is called for old turns
        with patch("middleware.g10_memory._get_redis", side_effect=Exception("redis down")):
            with patch("middleware.g10_memory._summarise", new_callable=AsyncMock,
                       return_value="Summary."):
                from middleware.g10_memory import G10Memory
                ctx = await G10Memory().process_request(ctx)
        # Should not raise; context trimmed to window
        assert ctx is not None


class _SessionRedis:
    """The session store, kept across requests."""

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _turns(*contents):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": c}
            for i, c in enumerate(contents)]


@pytest.mark.asyncio
class TestSessionState:
    """A request with a session id used to cost a summary call every time: G10 summarised the
    whole conversation and added the summary to the next request, which a normal chat client
    had resent in full anyway. The session now records how many turns the client sent. A
    request that brings more than last time gets neither; one that brings no more (a client
    relying on the proxy to remember) gets the stored summary, and its update builds on it."""

    @staticmethod
    async def _request(make_ctx, redis, turns, summarised, window=50):
        ctx = make_ctx([{"role": "system", "content": "Be helpful."}, *turns],
                       params={"x_session_id": "s1"})
        ctx.config["groups"]["G10_memory"]["sliding_window_turns"] = window

        async def summarise(history, model, ctx):
            summarised.append([m.get("content") for m in history])
            return f"summary {len(summarised)}"

        with patch("middleware.g10_memory._get_redis", return_value=redis), \
             patch("middleware.g10_memory._summarise", new=summarise):
            from middleware.g10_memory import G10Memory
            return await G10Memory().process_request(ctx)

    @staticmethod
    def _session_context(ctx):
        return [m["content"] for m in ctx.messages
                if m.get("role") == "system" and m["content"].startswith("[Session context]")]

    async def test_a_client_that_resends_the_conversation_costs_one_summary_per_session(
            self, make_ctx):
        redis, summarised = _SessionRedis(), []
        await self._request(make_ctx, redis, _turns("q1"), summarised)
        second = await self._request(make_ctx, redis, _turns("q1", "a1", "q2"), summarised)
        third = await self._request(make_ctx, redis, _turns("q1", "a1", "q2", "a2", "q3"),
                                    summarised)
        assert len(summarised) == 1                    # the session's first request only
        assert self._session_context(second) == [] and self._session_context(third) == []

    async def test_a_client_relying_on_the_proxy_gets_a_memory_that_builds_up(self, make_ctx):
        redis, summarised = _SessionRedis(), []
        await self._request(make_ctx, redis, _turns("q1"), summarised)
        second = await self._request(make_ctx, redis, _turns("q2"), summarised)
        third = await self._request(make_ctx, redis, _turns("q3"), summarised)
        assert self._session_context(second) == ["[Session context]\nsummary 1"]
        assert self._session_context(third) == ["[Session context]\nsummary 2"]
        # Each update summarises the previous summary with the new turns.
        assert summarised == [["q1"], ["[Session context]\nsummary 1", "q2"],
                              ["[Session context]\nsummary 2", "q3"]]

    async def test_a_client_that_stops_resending_still_finds_the_memory(self, make_ctx):
        redis, summarised = _SessionRedis(), []
        await self._request(make_ctx, redis, _turns("q1"), summarised)
        await self._request(make_ctx, redis, _turns("q1", "a1", "q2"), summarised)   # resent
        later = await self._request(make_ctx, redis, _turns("q3", "a3"), summarised)
        assert self._session_context(later) == ["[Session context]\nsummary 1"]
        assert len(summarised) == 2

    async def test_the_session_counts_the_turns_the_client_sent_not_those_left_after_the_window(
            self, make_ctx):
        redis, summarised = _SessionRedis(), []
        await self._request(make_ctx, redis, _turns("q1", "a1", "q2", "a2", "q3"), summarised,
                            window=1)
        stored = json.loads(next(iter(redis.store.values())))
        assert stored["turn_count"] == 5

    async def test_the_window_still_shortens_a_resent_conversation(self, make_ctx):
        redis, summarised = _SessionRedis(), []
        await self._request(make_ctx, redis, _turns("q1"), summarised, window=1)
        out = await self._request(make_ctx, redis, _turns("q1", "a1", "q2", "a2", "q3"),
                                  summarised, window=1)
        system = [m["content"] for m in out.messages if m.get("role") == "system"]
        assert any(s.startswith("[Conversation summary — earlier turns]") for s in system)
        assert self._session_context(out) == []
        # The session's first request, then the window's summary of the trimmed turns only.
        assert summarised == [["q1"], ["q1", "a1", "q2"]]


def _tool_call(cid: str, name: str = "f", args: str = "{}") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def _assert_no_orphan_tool_results(messages) -> None:
    """Every role:"tool" message must reference a tool_call_id declared by a
    *preceding* assistant tool_calls entry — otherwise litellm/OpenAI/Anthropic
    reject the whole request with a 400 (the C1 failure mode)."""
    declared: set = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if tc.get("id"):
                    declared.add(tc["id"])
        elif m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            assert tcid in declared, (
                f"orphaned tool result {tcid!r} at the window boundary — provider would 400"
            )


class TestSlidingWindowToolPairing:
    """C1 regression — the sliding-window cut must be tool-pairing-aware so a long
    agentic (tool-calling) conversation never gets an orphaned role:"tool" at the
    window boundary."""

    def test_split_no_tool_messages_is_plain_positional(self):
        from middleware.g10_memory import _safe_window_split
        turns = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
            for i in range(8)
        ]
        # No tool messages → identical to the old blind cut (len 8 - keep 4).
        assert _safe_window_split(turns, 4) == 4

    def test_split_snaps_back_over_orphaned_tool_result(self):
        from middleware.g10_memory import _safe_window_split
        turns = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_a")]},
            {"role": "tool", "tool_call_id": "call_a", "content": "result_a"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "user", "content": "final"},
        ]
        # keep=4 → blind boundary is index 4 (the tool result); it must snap back
        # to the declaring assistant at index 3 rather than orphan the result.
        assert _safe_window_split(turns, 4) == 3

    def test_split_snaps_over_parallel_tool_results(self):
        from middleware.g10_memory import _safe_window_split
        turns = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("a"), _tool_call("b", name="g")]},
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
            {"role": "user", "content": "final"},
        ]
        # keep=3 → blind boundary is index 2 (first of two parallel results);
        # snap back over both results to the single declaring assistant (index 1).
        assert _safe_window_split(turns, 3) == 1

    def test_split_pathological_all_tool_returns_zero(self):
        from middleware.g10_memory import _safe_window_split
        turns = [{"role": "assistant", "content": None, "tool_calls": [_tool_call("x")]}] + [
            {"role": "tool", "tool_call_id": "x", "content": "r"} for _ in range(6)
        ]
        # A boundary buried in an unbroken tool run has no clean cut → 0 (trim nothing).
        assert _safe_window_split(turns, 3) == 0

    @pytest.mark.asyncio
    async def test_apply_sliding_window_keeps_tool_pairs_wellformed(self, make_ctx):
        from middleware import g10_memory
        turns = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_a")]},
            {"role": "tool", "tool_call_id": "call_a", "content": "result_a"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "user", "content": "final"},
        ]
        ctx = make_ctx([{"role": "system", "content": "sys"}] + turns)
        # The naive turns[-4:] cut would have started the window on the orphaned
        # tool result; guard against a regression by asserting that shape up front.
        assert turns[-4:][0] == {"role": "tool", "tool_call_id": "call_a", "content": "result_a"}

        with patch.object(g10_memory, "_summarise", new_callable=AsyncMock,
                          return_value="earlier-turns summary"):
            await g10_memory._apply_sliding_window(ctx, window=2, summary_model="gpt-4o-mini")

        # No orphaned tool result survived the cut, and the declaring assistant did.
        _assert_no_orphan_tool_results(ctx.messages)
        assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in ctx.messages)
        # Still trimmed (a summary system message was injected).
        assert any("summary" in (m.get("content") or "") for m in ctx.messages
                   if m.get("role") == "system")


# ── The lookup query and the skills threshold are the group's settings ───────
_LONG_QUESTION = "q" * 1000


@pytest.mark.asyncio
class TestLookupSettingsFromConfig:
    """memory_query_max_chars and skills_similarity_threshold are G10 settings a tenant can
    change (the portal offers the first). The MEMORY_QUERY_MAX_CHARS and
    SKILLS_SIMILARITY_THRESHOLD env vars only supply the defaults."""

    @staticmethod
    def _ctx(make_ctx, **settings):
        ctx = make_ctx([{"role": "user", "content": _LONG_QUESTION}])
        ctx.config["groups"]["G10_memory"].update(settings)
        return ctx

    @staticmethod
    async def _mem0_query(ctx):
        from middleware.g10_memory import G10Memory
        mem0 = MagicMock()
        mem0.retrieve_memories = AsyncMock(return_value=[])
        mem0.store_exchange = MagicMock(return_value=True)
        with patch.object(G10Memory, "_get_mem0", return_value=mem0):
            await G10Memory().process_request(ctx)
        return mem0.retrieve_memories.await_args.args[1]

    async def test_the_mem0_query_is_cut_at_the_groups_setting(self, make_ctx):
        assert await self._mem0_query(self._ctx(make_ctx, memory_query_max_chars=120)) == "q" * 120

    async def test_without_the_setting_the_env_default_applies(self, make_ctx, monkeypatch):
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_MEMORY_QUERY_MAX_CHARS", 50)
        assert await self._mem0_query(self._ctx(make_ctx)) == "q" * 50

    async def test_an_unreadable_setting_falls_back_to_the_default(self, make_ctx, monkeypatch):
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_MEMORY_QUERY_MAX_CHARS", 50)
        ctx = self._ctx(make_ctx, memory_query_max_chars="lots")
        assert await self._mem0_query(ctx) == "q" * 50

    async def test_the_qdrant_skills_lookup_follows_the_groups_settings(self, make_ctx):
        from middleware.g10_memory import G10Memory, SkillsManager
        ctx = self._ctx(make_ctx, memory_query_max_chars=150, skills_enabled=True,
                        skills_similarity_threshold=0.85)
        with patch.object(SkillsManager, "search_skills", new_callable=AsyncMock,
                          return_value=[]) as search:
            await G10Memory().process_request(ctx)
        assert search.await_args.args[0] == "q" * 150
        assert search.await_args.kwargs.get("score_threshold") == 0.85

    async def test_search_skills_hands_its_threshold_to_qdrant(self):
        import numpy as np
        from middleware.g10_memory import SkillsManager
        model = MagicMock()
        model.embed.return_value = iter([np.zeros(3)])
        client = MagicMock()
        client.search.return_value = []
        with patch("qdrant_client.QdrantClient", return_value=client), \
                patch("ml_models.get_text_embedding", return_value=model), \
                patch("ml_models.qdrant_client_kwargs", return_value={}):
            await SkillsManager("skills_t").search_skills("q", top_k=2, score_threshold=0.85)
        assert client.search.call_args.kwargs["score_threshold"] == 0.85

    async def test_the_other_skills_lookup_follows_the_groups_settings(self, make_ctx):
        import middleware.g07_retrieval as g07
        from middleware.g10_memory import G10Memory
        ctx = self._ctx(make_ctx, memory_query_max_chars=150, skills_enabled=True,
                        skills_qdrant_enabled=False, skills_similarity_threshold=0.85)
        with patch.object(g07, "_hybrid_search", new_callable=AsyncMock,
                          return_value=[]) as search, \
                patch.object(g07, "_rerank", new_callable=AsyncMock, return_value=[]) as rerank:
            await G10Memory().process_request(ctx)
        assert search.await_args.args[0] == "q" * 150
        assert rerank.await_args.args[3] == 0.85

    async def test_the_skills_threshold_defaults_to_the_env_value(self, make_ctx, monkeypatch):
        import middleware.g07_retrieval as g07
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_SKILLS_SIMILARITY_THRESHOLD", 0.6)
        ctx = self._ctx(make_ctx, skills_enabled=True, skills_qdrant_enabled=False)
        with patch.object(g07, "_hybrid_search", new_callable=AsyncMock, return_value=[]), \
                patch.object(g07, "_rerank", new_callable=AsyncMock, return_value=[]) as rerank:
            await g10.G10Memory().process_request(ctx)
        assert rerank.await_args.args[3] == 0.6

    async def test_an_unreadable_skills_threshold_falls_back_to_the_default(self, make_ctx,
                                                                            monkeypatch):
        import middleware.g07_retrieval as g07
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_SKILLS_SIMILARITY_THRESHOLD", 0.6)
        ctx = self._ctx(make_ctx, skills_enabled=True, skills_qdrant_enabled=False,
                        skills_similarity_threshold="high")
        with patch.object(g07, "_hybrid_search", new_callable=AsyncMock, return_value=[]), \
                patch.object(g07, "_rerank", new_callable=AsyncMock, return_value=[]) as rerank:
            await g10.G10Memory().process_request(ctx)
        assert rerank.await_args.args[3] == 0.6


# ── Mem0 long-term memory ────────────────────────────────────────────────────
# mem0ai 2.x (requirements pins 2.0.17) exports AsyncMemoryClient. Its search() refuses a
# top-level user_id (the user goes in filters=), takes top_k and answers {"results": [...]};
# add() takes the messages plus user_id and metadata. The fake below keeps those rules.

_ENTITY_PARAMS = {"user_id", "agent_id", "app_id", "run_id"}


class _FakeMem0:
    def __init__(self, results=None, search_delay=0.0, add_gate=None):
        self.results = results or []
        self.search_delay = search_delay
        self.add_gate = add_gate
        self.searches, self.adds = [], []

    async def search(self, query, options=None, **kwargs):
        refused = _ENTITY_PARAMS & set(kwargs)
        if refused:
            raise ValueError(f"Top-level entity parameters {refused} are not supported in "
                             "search(). Use filters={'user_id': '...'} instead.")
        if self.search_delay:
            await asyncio.sleep(self.search_delay)
        self.searches.append((query, kwargs))
        return {"results": list(self.results)}

    async def add(self, messages, options=None, **kwargs):
        if self.add_gate is not None:
            await self.add_gate.wait()
        self.adds.append((messages, kwargs))
        return {"results": []}


def _mem0_client(monkeypatch, factory):
    import middleware.g10_memory as g10
    monkeypatch.setattr(g10, "_mem0_available", True)
    return g10.Mem0MemoryClient(api_url="https://mem0.example", api_key="k", factory=factory)


async def _ready(monkeypatch, fake):
    client = _mem0_client(monkeypatch, lambda key, host: fake)
    assert client.client() is None                    # the first call starts building it
    await client._starting
    assert client.client() is fake
    return client


@pytest.mark.asyncio
class TestMem0Client:
    async def test_the_client_is_built_in_a_worker_thread(self, monkeypatch):
        seen = {}

        def factory(key, host):
            seen["thread"], seen["args"] = threading.current_thread(), (key, host)
            return _FakeMem0()

        client = _mem0_client(monkeypatch, factory)
        assert client.client() is None
        await client._starting
        assert seen["thread"] is not threading.current_thread()
        assert seen["args"] == ("k", "https://mem0.example")
        assert isinstance(client.client(), _FakeMem0)

    async def test_the_library_client_gets_the_url_the_key_and_a_short_timeout(self,
                                                                              monkeypatch):
        import middleware.g10_memory as g10
        made = {}

        class _Library:
            def __init__(self, **kwargs):
                made.update(kwargs)

        monkeypatch.setattr(g10, "AsyncMemoryClient", _Library, raising=False)
        g10._build_mem0_client("k", "https://mem0.example")
        assert (made["api_key"], made["host"]) == ("k", "https://mem0.example")
        assert made["client"].timeout.read == g10._MEM0_HTTP_TIMEOUT_S == 10.0
        await made["client"].aclose()

    @pytest.mark.parametrize("url,key", [("", "k"), ("https://mem0.example", "")])
    async def test_without_a_url_and_a_key_nothing_is_built(self, monkeypatch, url, key):
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_mem0_available", True)
        client = g10.Mem0MemoryClient(api_url=url, api_key=key,
                                      factory=lambda *a: pytest.fail("built"))
        assert client.client() is None and client._starting is None

    async def test_a_failed_start_waits_before_trying_again(self, monkeypatch):
        import middleware.g10_memory as g10
        calls = []

        def factory(key, host):
            calls.append(host)
            raise ValueError("Error: invalid API key")

        client = _mem0_client(monkeypatch, factory)
        client.client()
        await client._starting
        assert client.client() is None and client._starting is None      # not at once
        monkeypatch.setattr(g10, "_MEM0_RETRY_S", 0.0)
        client.client()
        await client._starting
        assert len(calls) == 2

    async def test_search_filters_on_the_user_and_reads_the_results(self, monkeypatch):
        fake = _FakeMem0(results=[{"memory": "prefers tea", "metadata": {"tenant_id": "acme"}}])
        client = await _ready(monkeypatch, fake)
        got = await client.retrieve_memories("acme::alice", "drinks?", limit=3, tenant_id="acme")
        assert got == ["prefers tea"]
        assert fake.searches == [("drinks?", {"filters": {"user_id": "acme::alice"}, "top_k": 3})]

    async def test_a_memory_stored_for_another_tenant_is_dropped(self, monkeypatch):
        fake = _FakeMem0(results=[{"memory": "beta's", "metadata": {"tenant_id": "beta"}},
                                  {"memory": "mine", "metadata": {"tenant_id": "acme"}}])
        client = await _ready(monkeypatch, fake)
        assert await client.retrieve_memories("acme::alice", "q", tenant_id="acme") == ["mine"]

    async def test_a_slow_search_does_not_hold_the_request(self, monkeypatch):
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_MEM0_SEARCH_WAIT_S", 0.05)
        client = await _ready(monkeypatch, _FakeMem0(results=[{"memory": "late"}],
                                                     search_delay=5.0))
        started = time.monotonic()
        assert await client.retrieve_memories("acme::alice", "q") == []
        assert time.monotonic() - started < 2.0

    async def test_a_store_runs_in_the_background(self, monkeypatch):
        gate = asyncio.Event()
        fake = _FakeMem0(add_gate=gate)
        client = await _ready(monkeypatch, fake)
        exchange = [{"role": "user", "content": "I prefer tea"}]
        assert client.store_exchange("acme::alice", exchange, {"tenant_id": "acme"}) is True
        assert fake.adds == [] and len(client._stores) == 1    # returned before Mem0 answered
        gate.set()
        await asyncio.gather(*client._stores)
        assert fake.adds == [(exchange, {"user_id": "acme::alice",
                                         "metadata": {"tenant_id": "acme"}})]
        assert not client._stores

    async def test_stores_waiting_on_a_slow_mem0_are_capped(self, monkeypatch):
        import middleware.g10_memory as g10
        monkeypatch.setattr(g10, "_MEM0_MAX_PENDING_STORES", 2)
        gate = asyncio.Event()
        client = await _ready(monkeypatch, _FakeMem0(add_gate=gate))
        exchange = [{"role": "user", "content": "x"}]
        assert [client.store_exchange("acme::alice", exchange, {}) for _ in range(3)] == \
            [True, True, False]
        gate.set()
        await asyncio.gather(*client._stores)


def _mem0_double():
    mem0 = MagicMock()
    mem0.retrieve_memories = AsyncMock(return_value=[])
    mem0.store_exchange = MagicMock(return_value=True)
    return mem0


async def _run_with_mem0(ctx, mem0):
    from middleware.g10_memory import G10Memory
    memory = G10Memory()
    memory._mem0 = mem0
    ctx.config["groups"]["G10_memory"]["mem0_enabled"] = True
    with patch("middleware.g10_memory._get_redis", side_effect=Exception("no redis")):
        await memory.process_request(ctx)


@pytest.mark.asyncio
class TestMem0Identity:
    """Long-term memory belongs to one user of one tenant: the AUTHENTICATED user. A tenant
    key authenticates the tenant (its user_id is the tenant id), so it carries no user."""

    async def test_a_tenant_key_without_a_user_gets_no_memory(self, make_ctx, caplog):
        ctx = make_ctx([{"role": "user", "content": "hi"}], params={"_auth_tenant_id": "acme"})
        ctx.user_id, ctx.tenant_id = "acme", "acme"
        mem0 = _mem0_double()
        with caplog.at_level("WARNING", logger="middleware.g10_memory"):
            await _run_with_mem0(ctx, mem0)
        mem0.retrieve_memories.assert_not_awaited()
        mem0.store_exchange.assert_not_called()
        assert any("X-User-ID" in r.getMessage() for r in caplog.records)

    async def test_an_allow_listed_user_gets_their_own_memory(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hi"}], params={"_auth_tenant_id": "acme"})
        ctx.user_id, ctx.tenant_id = "alice@acme.com", "acme"
        mem0 = _mem0_double()
        await _run_with_mem0(ctx, mem0)
        assert mem0.retrieve_memories.await_args.args[0] == "acme::alice@acme.com"
        assert mem0.store_exchange.call_args.args[0] == "acme::alice@acme.com"

    async def test_a_user_named_in_the_request_body_is_ignored(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hi"}],
                       params={"user_id": "victim", "x_user_id": "victim"})
        ctx.user_id, ctx.tenant_id = "bob", "acme"             # a legacy per-user key
        mem0 = _mem0_double()
        await _run_with_mem0(ctx, mem0)
        assert mem0.retrieve_memories.await_args.args[0] == "acme::bob"
        assert mem0.store_exchange.call_args.args[0] == "acme::bob"

    async def test_the_exchange_is_stored_once_in_order(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "first"},
                        {"role": "assistant", "content": "an answer"},
                        {"role": "user", "content": "I prefer tea"}])
        ctx.tenant_id = "acme"
        mem0 = _mem0_double()
        await _run_with_mem0(ctx, mem0)
        mem0.store_exchange.assert_called_once()
        user, exchange, metadata = mem0.store_exchange.call_args.args
        assert exchange == [{"role": "assistant", "content": "an answer"},
                            {"role": "user", "content": "I prefer tea"}]
        assert metadata["tenant_id"] == "acme"

    async def test_each_stored_turn_is_cut_to_500_characters(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "x" * 900}])
        mem0 = _mem0_double()
        await _run_with_mem0(ctx, mem0)
        assert mem0.store_exchange.call_args.args[1] == [{"role": "user", "content": "x" * 500}]

    async def test_content_that_is_not_text_is_not_sent(self, make_ctx):
        parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        ctx = make_ctx([{"role": "user", "content": parts}])
        mem0 = _mem0_double()
        await _run_with_mem0(ctx, mem0)
        mem0.retrieve_memories.assert_not_awaited()
        mem0.store_exchange.assert_not_called()


_PROXY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                          "src", "proxy"))


@pytest.mark.parametrize("preset,at_import", [(None, "false"), ("true", "true")])
def test_mem0_analytics_are_off_unless_the_operator_turns_them_on(tmp_path, preset, at_import):
    """mem0ai reports usage to its analytics service unless MEM0_TELEMETRY is false, and it
    reads the variable when it is imported. A stand-in mem0 records what it saw then."""
    (tmp_path / "mem0").mkdir()
    (tmp_path / "mem0" / "__init__.py").write_text(
        "import os\nSEEN = os.environ.get('MEM0_TELEMETRY')\n"
        "class AsyncMemoryClient:\n    pass\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "MEM0_TELEMETRY"}
    if preset is not None:
        env["MEM0_TELEMETRY"] = preset
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), _PROXY_SRC])
    out = subprocess.run(
        [sys.executable, "-c",
         "import middleware.g10_memory as g, mem0; print(g._mem0_available, mem0.SEEN)"],
        env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split()[-2:] == ["True", at_import]


def test_a_mem0_that_cannot_write_its_home_does_not_stop_the_proxy(tmp_path):
    """mem0ai creates ~/.mem0 when it is imported, and the proxy image installs it whether
    or not Mem0 is on. On a read-only filesystem that import fails with an OSError, which
    must leave Mem0 off rather than stop G10 (and the proxy) from loading."""
    (tmp_path / "mem0").mkdir()
    (tmp_path / "mem0" / "__init__.py").write_text(
        "raise PermissionError(13, 'Permission denied', '/home/app/.mem0')\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), _PROXY_SRC]))
    out = subprocess.run(
        [sys.executable, "-c", "import middleware.g10_memory as g; print(g._mem0_available)"],
        env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split()[-1:] == ["False"]
