"""Unit tests for G26 — Budget-Aware Context Management (BACM-style)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import copy
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware.g26_context_budget import (
    G26ContextBudget,
    _get_model_context_window,
    _local_cache_put,
    _resolve_thresholds,
    _span_tokens,
    _usable_window,
    _LOCAL_SUMMARY_CACHE,
    _LOCAL_SUMMARY_CACHE_MAX,
    _WARNED,
)
from savings.calculator import count_messages_tokens


# ─── Helpers ──────────────────────────────────────────────────────────────────

def g26_cfg(**overrides):
    """A G26 config tuned so a modest conversation crosses the trigger."""
    cfg = {
        "enabled": True,
        "compact_at_pct": 60,
        "target_pct": 30,
        "keep_recent_turns": 4,
        "reserve_output_tokens": 0,
        "rungs": {"prune": True, "compress": True, "summarize": True, "drop": False},
        "tool_result_max_chars": 200,
        "summary_model": "gpt-4o-mini",
        "summary_ttl_seconds": 3600,
        "metrics_enabled": True,
        "default_context_window": 1000,
        "model_context_window": {"gpt-4o": 1000, "gpt-4o-mini": 1000},
    }
    cfg.update(overrides)
    return cfg


def conversation(n_pairs=12, filler="Please note that in order to answer the question "):
    """A long, compressible conversation with a system prompt."""
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"{filler}number {i}, tell me about topic {i}."})
        msgs.append({"role": "assistant", "content": f"{filler}the answer for topic {i} is detailed."})
    return msgs


def prep(ctx, cfg=None, **cfg_overrides):
    ctx.config["groups"]["G26_context_budget"] = cfg or g26_cfg(**cfg_overrides)
    return ctx


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Keep the bounded local summary cache and one-time warnings test-local."""
    _LOCAL_SUMMARY_CACHE.clear()
    _WARNED.clear()
    yield
    _LOCAL_SUMMARY_CACHE.clear()
    _WARNED.clear()


@pytest.fixture(autouse=True)
def _no_redis():
    """Force the in-process cache fallback — no Redis in unit tests."""
    def _boom():
        raise ConnectionError("no redis in unit tests")
    with patch("cache.redis_pool.get_redis", side_effect=_boom):
        yield


@pytest.fixture
def summariser():
    with patch(
        "middleware.g26_context_budget.summarise_turns",
        new_callable=AsyncMock,
        return_value="Earlier the user asked about topics 0-7.",
    ) as m:
        yield m


# ─── Guards / no-op paths ─────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGuards:
    async def test_disabled_is_byte_identical(self, make_ctx):
        ctx = prep(make_ctx(conversation()), enabled=False)
        before = copy.deepcopy(ctx.messages)
        ctx = await G26ContextBudget().process_request(ctx)
        assert ctx.messages == before
        assert not [s for s in ctx.savings.step_savings if s.group == "G26"]

    async def test_missing_config_block_is_noop(self, make_ctx):
        ctx = make_ctx(conversation())
        ctx.config["groups"].pop("G26_context_budget", None)
        before = copy.deepcopy(ctx.messages)
        ctx = await G26ContextBudget().process_request(ctx)
        assert ctx.messages == before

    async def test_under_budget_records_no_step(self, make_ctx):
        # Two short turns against a 1000-token window — nowhere near 60%.
        ctx = prep(make_ctx([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]))
        before = copy.deepcopy(ctx.messages)
        ctx = await G26ContextBudget().process_request(ctx)
        assert ctx.messages == before
        assert not [s for s in ctx.savings.step_savings if s.group == "G26"]

    async def test_bypassed_and_cache_hit_skip(self, make_ctx):
        for flag in ("bypassed", "cache_hit"):
            ctx = prep(make_ctx(conversation()))
            setattr(ctx, flag, True)
            before = copy.deepcopy(ctx.messages)
            ctx = await G26ContextBudget().process_request(ctx)
            assert ctx.messages == before, flag

    async def test_all_turns_inside_protected_tail_is_noop(self, make_ctx):
        # keep_recent_turns covers every turn → no old span exists to compact.
        ctx = prep(make_ctx(conversation(n_pairs=12)), keep_recent_turns=100)
        before = copy.deepcopy(ctx.messages)
        ctx = await G26ContextBudget().process_request(ctx)
        assert ctx.messages == before

    async def test_internal_error_preserves_original_messages(self, make_ctx):
        ctx = prep(make_ctx(conversation()))
        before = copy.deepcopy(ctx.messages)
        with patch("middleware.g26_context_budget.safe_window_split", side_effect=RuntimeError("boom")):
            ctx = await G26ContextBudget().process_request(ctx)
        assert ctx.messages == before
        assert not [s for s in ctx.savings.step_savings if s.group == "G26"]


# ─── Window resolution ────────────────────────────────────────────────────────

class TestWindowResolution:
    def test_exact_prefix_and_default(self):
        cfg = {
            "model_context_window": {"gpt-4o": 128000, "claude-": 200000, "claude-3-5-sonnet": 250000},
            "default_context_window": 4096,
        }
        assert _get_model_context_window("gpt-4o", cfg) == 128000          # exact
        assert _get_model_context_window("claude-3-opus", cfg) == 200000   # prefix
        assert _get_model_context_window("claude-3-5-sonnet-x", cfg) == 250000  # longest prefix wins
        assert _get_model_context_window("mystery-model", cfg) == 4096     # default
        assert _get_model_context_window(None, cfg) == 4096

    def test_max_tokens_reserves_output_headroom(self, make_ctx):
        cfg = g26_cfg(reserve_output_tokens=100)
        ctx = make_ctx([], model="gpt-4o", params={})
        assert _usable_window(ctx, cfg) == 900          # 1000 − configured reserve
        ctx = make_ctx([], model="gpt-4o", params={"max_tokens": 400})
        assert _usable_window(ctx, cfg) == 600          # caller's max_tokens wins

    def test_target_above_trigger_is_clamped_and_warns_once(self, make_ctx, caplog):
        ctx = make_ctx([])
        cfg = g26_cfg(compact_at_pct=80, target_pct=90)
        compact_at, target = _resolve_thresholds(ctx, cfg)
        assert compact_at == 80 and target < compact_at
        n_warnings = len(_WARNED)
        _resolve_thresholds(ctx, cfg)
        assert len(_WARNED) == n_warnings  # second call does not re-warn


# ─── The ladder ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestLadder:
    async def test_prune_alone_can_reach_target(self, make_ctx):
        # A history that is mostly byte-exact duplicates: rung 1 is enough.
        dup = {"role": "user", "content": "Please note that in order to proceed, you must confirm "
                                          "the ticket id and the account tier before answering."}
        msgs = [{"role": "system", "content": "sys"}] + [dict(dup) for _ in range(24)]
        msgs += [{"role": "user", "content": "final question"}]
        ctx = prep(make_ctx(msgs), rungs={"prune": True, "compress": False, "summarize": False, "drop": False})
        ctx = await G26ContextBudget().process_request(ctx)
        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert "prune" in step.description
        assert len(ctx.messages) < len(msgs)

    async def test_truncates_long_old_tool_results(self, make_ctx):
        msgs = [{"role": "system", "content": "sys"}]
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": "c1", "function": {"name": "logs", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "c1", "content": "L" * 5000})
        for i in range(6):
            msgs.append({"role": "user", "content": f"q{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        ctx = prep(make_ctx(msgs), tool_result_max_chars=100,
                   rungs={"prune": True, "compress": False, "summarize": False, "drop": False})
        ctx = await G26ContextBudget().process_request(ctx)
        tool_msgs = [m for m in ctx.messages if m.get("role") == "tool"]
        assert tool_msgs and len(tool_msgs[0]["content"]) < 200
        assert tool_msgs[0]["content"].endswith("[truncated]")

    async def test_escalates_to_compress(self, make_ctx):
        ctx = prep(make_ctx(conversation()),
                   rungs={"prune": True, "compress": True, "summarize": False, "drop": False})
        before = count_messages_tokens(ctx.messages, ctx.model)
        ctx = await G26ContextBudget().process_request(ctx)
        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert "compress" in step.description
        assert count_messages_tokens(ctx.messages, ctx.model) < before

    async def test_escalates_to_summarize(self, make_ctx, summariser):
        msgs = conversation()
        ctx = prep(make_ctx(msgs))
        tail_before = msgs[-4:]
        ctx = await G26ContextBudget().process_request(ctx)

        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert "summarize" in step.description
        summaries = [m for m in ctx.messages
                     if m.get("role") == "system" and "[Conversation summary" in str(m.get("content"))]
        assert len(summaries) == 1
        # System prompt first, summary directly after it, protected tail verbatim at the end.
        assert ctx.messages[0] == msgs[0]
        assert ctx.messages[1] is summaries[0]
        assert ctx.messages[-4:] == tail_before
        assert summariser.await_count == 1

    async def test_summariser_failure_keeps_compressed_output(self, make_ctx):
        ctx = prep(make_ctx(conversation()))
        with patch("middleware.g26_context_budget.summarise_turns",
                   new_callable=AsyncMock, return_value="[summary unavailable]"):
            ctx = await G26ContextBudget().process_request(ctx)
        assert not [m for m in ctx.messages if "[Conversation summary" in str(m.get("content"))]
        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert "summarize" not in step.description
        assert "compress" in step.description

    async def test_cut_never_orphans_a_tool_result(self, make_ctx, summariser):
        # The naive keep_recent_turns=4 cut would land on the tool result.
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(6):
            msgs.append({"role": "user", "content": f"Please note that in order to check topic {i}, look it up."})
            msgs.append({"role": "assistant", "content": f"Checking topic {i} in detail now."})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": "call_x", "function": {"name": "lookup", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "call_x", "content": "result payload"})
        msgs.append({"role": "assistant", "content": "Here is what I found."})
        msgs.append({"role": "user", "content": "thanks"})

        ctx = prep(make_ctx(msgs), keep_recent_turns=3)
        ctx = await G26ContextBudget().process_request(ctx)

        non_system = [m for m in ctx.messages if m.get("role") != "system"]
        assert non_system[0].get("role") != "tool"
        # Every surviving tool result still has its declaring assistant turn.
        declared = {tc["id"] for m in ctx.messages for tc in (m.get("tool_calls") or [])}
        for m in ctx.messages:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in declared

    async def test_multimodal_image_parts_are_untouched(self, make_ctx):
        image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(16):
            # Distinct text per turn so rung 1 cannot collapse them and rung 2 is
            # the rung actually under test here.
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": f"Please note that in order to describe image {i} "
                                         f"accurately, you should be brief and factual."},
                copy.deepcopy(image_part),
            ]})
            msgs.append({"role": "assistant", "content": f"Described image {i} in some detail here."})
        ctx = prep(make_ctx(msgs), rungs={"prune": True, "compress": True, "summarize": False, "drop": False})
        ctx = await G26ContextBudget().process_request(ctx)

        parts = [p for m in ctx.messages if isinstance(m.get("content"), list) for p in m["content"]]
        images = [p for p in parts if p.get("type") == "image_url"]
        assert images, "image parts must survive compaction"
        assert all(p == image_part for p in images)
        texts = [p for p in parts if p.get("type") == "text"]
        assert any("Please note that" not in p["text"] for p in texts)


@pytest.mark.asyncio
class TestRungToggles:
    async def test_drop_is_off_by_default(self, make_ctx):
        ctx = prep(make_ctx(conversation()),
                   rungs={"prune": True, "compress": False, "summarize": False, "drop": False})
        ctx = await G26ContextBudget().process_request(ctx)
        steps = [s for s in ctx.savings.step_savings if s.group == "G26"]
        assert not steps or "drop" not in steps[0].description

    async def test_drop_when_enabled_removes_oldest_only(self, make_ctx):
        msgs = conversation(n_pairs=12)
        system_msg, tail_before = msgs[0], msgs[-4:]
        ctx = prep(make_ctx(msgs), keep_recent_turns=4,
                   rungs={"prune": False, "compress": False, "summarize": False, "drop": True})
        ctx = await G26ContextBudget().process_request(ctx)

        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert "drop" in step.description
        assert ctx.messages[0] == system_msg            # system survives
        assert ctx.messages[-4:] == tail_before          # protected tail survives
        assert len(ctx.messages) < len(msgs)
        # What remains of the old span is a suffix of it — only the OLDEST went.
        assert ctx.messages[1] in msgs[1:-4] or ctx.messages[1] in tail_before

    async def test_drop_never_leaves_an_orphaned_tool_result(self, make_ctx):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(5):
            msgs.append({"role": "user", "content": f"question {i} with a reasonably long body of text"})
            msgs.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": f"c{i}", "function": {"name": "f", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"payload {i} " * 10})
        msgs.append({"role": "user", "content": "final"})
        ctx = prep(make_ctx(msgs), keep_recent_turns=2,
                   rungs={"prune": False, "compress": False, "summarize": False, "drop": True})
        ctx = await G26ContextBudget().process_request(ctx)

        declared = {tc["id"] for m in ctx.messages for tc in (m.get("tool_calls") or [])}
        for m in ctx.messages:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in declared

    async def test_all_rungs_off_is_noop(self, make_ctx):
        ctx = prep(make_ctx(conversation()),
                   rungs={"prune": False, "compress": False, "summarize": False, "drop": False})
        before = copy.deepcopy(ctx.messages)
        ctx = await G26ContextBudget().process_request(ctx)
        assert ctx.messages == before
        assert not [s for s in ctx.savings.step_savings if s.group == "G26"]


@pytest.mark.asyncio
class TestTenantOverride:
    async def test_tenant_block_overrides_base(self, make_ctx):
        ctx = make_ctx(conversation())
        ctx.tenant_id = "NOVA-STG-01"
        ctx.config["groups"]["G26_context_budget"] = g26_cfg(enabled=False)
        ctx.config.setdefault("tenants", {})["NOVA-STG-01"] = {
            "groups": {"G26_context_budget": {"enabled": True,
                                              "rungs": {"summarize": False}}}
        }
        ctx = await G26ContextBudget().process_request(ctx)
        steps = [s for s in ctx.savings.step_savings if s.group == "G26"]
        assert steps, "tenant override should have enabled G26"
        assert "summarize" not in steps[0].description  # nested rung override merged


@pytest.mark.asyncio
class TestSummaryCache:
    async def test_second_request_reuses_cached_summary(self, make_ctx, summariser):
        for _ in range(2):
            ctx = prep(make_ctx(conversation()))
            ctx = await G26ContextBudget().process_request(ctx)
            assert [m for m in ctx.messages if "[Conversation summary" in str(m.get("content"))]
        assert summariser.await_count == 1, "identical prefix must hit the cache"

    async def test_expired_entry_triggers_resummarisation(self, make_ctx, summariser):
        ctx = prep(make_ctx(conversation()))
        await G26ContextBudget().process_request(ctx)
        assert summariser.await_count == 1
        # Expire everything in the local store.
        for k, (_exp, v) in list(_LOCAL_SUMMARY_CACHE.items()):
            _LOCAL_SUMMARY_CACHE[k] = (time.time() - 1, v)
        ctx = prep(make_ctx(conversation()))
        await G26ContextBudget().process_request(ctx)
        assert summariser.await_count == 2

    async def test_cache_key_is_tenant_scoped(self, make_ctx, summariser):
        for tenant, prefix in (("A-STG-01", "tenant:a:"), ("B-STG-01", "tenant:b:")):
            ctx = prep(make_ctx(conversation()))
            ctx.tenant_id, ctx.redis_prefix = tenant, prefix
            await G26ContextBudget().process_request(ctx)
        assert summariser.await_count == 2, "one tenant must not read another's summary"
        assert all(k.startswith(("tenant:a:", "tenant:b:")) for k in _LOCAL_SUMMARY_CACHE)

    async def test_local_store_is_bounded(self):
        for i in range(_LOCAL_SUMMARY_CACHE_MAX + 50):
            _local_cache_put(f"k{i}", f"v{i}", 3600)
        assert len(_LOCAL_SUMMARY_CACHE) <= _LOCAL_SUMMARY_CACHE_MAX


@pytest.mark.asyncio
class TestObservability:
    async def test_counter_increments_once_per_applied_rung(self, make_ctx, summariser):
        counter = MagicMock()
        ctx = prep(make_ctx(conversation()))
        with patch("middleware.g18_observability.CONTEXT_BUDGET_COMPACTIONS_TOTAL", counter):
            ctx = await G26ContextBudget().process_request(ctx)
        rungs = [c.kwargs["rung"] for c in counter.labels.call_args_list]
        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert rungs == step.description.split("rungs=")[1].split("+")
        assert counter.labels.return_value.inc.call_count == len(rungs)

    async def test_metrics_can_be_disabled(self, make_ctx, summariser):
        counter = MagicMock()
        ctx = prep(make_ctx(conversation()), metrics_enabled=False)
        with patch("middleware.g18_observability.CONTEXT_BUDGET_COMPACTIONS_TOTAL", counter):
            await G26ContextBudget().process_request(ctx)
        counter.labels.assert_not_called()

    async def test_savings_step_reports_real_reduction(self, make_ctx, summariser):
        ctx = prep(make_ctx(conversation()))
        before = ctx.current_request_token_count
        ctx = await G26ContextBudget().process_request(ctx)
        step = [s for s in ctx.savings.step_savings if s.group == "G26"][0]
        assert step.tokens_before == before
        assert step.tokens_after == ctx.current_request_token_count < before


class TestTokenAccounting:
    def test_per_message_counts_are_additive(self):
        """The ladder tracks its progress incrementally instead of recounting the
        whole prompt after every rung — which is only sound if per-message counts
        sum exactly to the whole-list count."""
        msgs = conversation(n_pairs=6)
        assert sum(count_messages_tokens([m], "gpt-4o") for m in msgs) == _span_tokens(msgs, "gpt-4o")
