"""
Tests for G21 — Provider Prompt Cache Alignment.

Validates:
  - Message reordering for OpenAI prefix caching
  - Anthropic cache_control marker injection
  - Tool definition cache markers
  - Config-driven enable/disable
  - Provider detection (config-driven + default heuristic)
  - No-op when already aligned
  - Ablation pattern: cost comparison
"""
import copy
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "proxy"))

from middleware.g21_cache_alignment import G21CacheAlignment


def _make_config(enabled=True, openai_auto=True, anthropic_marker=True,
                 cache_type="ephemeral", provider_detection=None):
    cfg = {
        "groups": {
            "G21_cache_alignment": {
                "enabled": enabled,
                "providers": {
                    "openai": {"auto": openai_auto},
                    "anthropic": {"marker": anthropic_marker, "cache_type": cache_type},
                },
            }
        }
    }
    if provider_detection:
        cfg["groups"]["G21_cache_alignment"]["provider_detection"] = provider_detection
    return cfg


def _make_ctx(messages, model="gpt-4o", params=None, config=None, provider_adapter=None):
    """Lightweight context mock for G21 tests."""
    from tests.conftest import _make_savings
    from middleware import RequestContext

    if params is None:
        params = {}
    if config is None:
        config = _make_config()

    savings = _make_savings(messages, model)
    ctx = RequestContext(
        request_id="req-g21-test",
        user_id="test-user",
        original_messages=copy.deepcopy(messages),
        messages=copy.deepcopy(messages),
        model=model,
        routed_model=model,
        params=dict(params),
        config=config,
        savings=savings,
    )
    ctx.provider_adapter = provider_adapter
    return ctx


# ─── Test: disabled via config ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_skips():
    """G21 does nothing when disabled."""
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    ctx = _make_ctx(msgs, config=_make_config(enabled=False))
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    # Messages should be unchanged
    assert result.messages == msgs
    assert len(result.savings.step_savings) == 0


# ─── Test: OpenAI reordering ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_reorders_system_to_front():
    """System messages should be moved to position 0 for OpenAI."""
    msgs = [
        {"role": "user", "content": "What is 2+2?"},
        {"role": "system", "content": "You are a math tutor."},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "And 3+3?"},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o")
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "You are a math tutor."
    assert len([m for m in result.messages if m["role"] == "system"]) == 1
    assert len(result.savings.step_savings) == 1
    assert "G21" in result.savings.step_savings[0].group


@pytest.mark.asyncio
async def test_openai_already_aligned_noop():
    """No change when system messages are already at the front."""
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o")
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    assert result.messages == msgs
    assert len(result.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_openai_no_system_messages():
    """No crash when there are no system messages."""
    msgs = [{"role": "user", "content": "Hello"}]
    ctx = _make_ctx(msgs, model="gpt-4o")
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    assert result.messages == msgs
    assert len(result.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_openai_disabled_via_provider_config():
    """OpenAI alignment disabled when auto=false."""
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o", config=_make_config(openai_auto=False))
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    assert result.messages == msgs


# ─── Test: Anthropic cache markers ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_injects_cache_control():
    """Last system message gets cache_control marker for Anthropic (requires adapter)."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "First system."},
        {"role": "system", "content": "Second system."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", provider_adapter=AnthropicAdapter())
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    # Second (last) system msg should have cache_control
    assert result.messages[1]["cache_control"] == {"type": "ephemeral"}
    # First system msg should NOT have cache_control
    assert "cache_control" not in result.messages[0]
    assert len(result.savings.step_savings) == 1


@pytest.mark.asyncio
async def test_anthropic_marks_tools():
    """Tool definitions get cache_control markers for Anthropic (requires adapter)."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    tools = [
        {"type": "function", "function": {"name": "get_weather", "parameters": {}}},
        {"type": "function", "function": {"name": "search", "parameters": {}}},
    ]
    ctx = _make_ctx(msgs, model="claude-opus-4", params={"tools": tools},
                    provider_adapter=AnthropicAdapter())
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    result_tools = result.params["tools"]
    assert result_tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in result_tools[0]


@pytest.mark.asyncio
async def test_anthropic_custom_cache_type():
    """Configurable cache_type is injected (requires adapter)."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    cfg = _make_config(cache_type="persistent")
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", config=cfg,
                    provider_adapter=AnthropicAdapter())
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    assert result.messages[0]["cache_control"] == {"type": "persistent"}


@pytest.mark.asyncio
async def test_anthropic_marker_per_tenant_override_enables():
    """A tenant can flip marker:true over a global marker:false (captures the 90% Anthropic discount)."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    cfg = _make_config(anthropic_marker=False)  # global default OFF
    cfg["tenants"] = {
        "acme": {"groups": {"G21_cache_alignment": {"providers": {"anthropic": {"marker": True}}}}}
    }
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", config=cfg,
                    provider_adapter=AnthropicAdapter())
    ctx.tenant_id = "acme"
    result = await G21CacheAlignment().process_request(ctx)
    # Tenant override wins → cache_control injected; cache_type inherited from base.
    assert result.messages[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_anthropic_marker_per_tenant_override_disables():
    """A tenant can flip marker:false over a global marker:true (tenant wins both directions)."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    cfg = _make_config(anthropic_marker=True)  # global default ON
    cfg["tenants"] = {
        "openai-co": {"groups": {"G21_cache_alignment": {"providers": {"anthropic": {"marker": False}}}}}
    }
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", config=cfg,
                    provider_adapter=AnthropicAdapter())
    ctx.tenant_id = "openai-co"
    result = await G21CacheAlignment().process_request(ctx)
    assert "cache_control" not in result.messages[0]


@pytest.mark.asyncio
async def test_anthropic_disabled_via_provider_config():
    """Anthropic alignment disabled when marker=false even with adapter set."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5",
                    config=_make_config(anthropic_marker=False),
                    provider_adapter=AnthropicAdapter())
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    assert "cache_control" not in result.messages[0]


# ─── T04: Adapter gating — cache_control never injected without AnthropicAdapter ─

@pytest.mark.asyncio
async def test_no_cache_control_without_adapter():
    """cache_control must NOT be injected when ctx.provider_adapter is None (T04)."""
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    # No provider_adapter set → Anthropic path must be skipped
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5")
    assert ctx.provider_adapter is None
    result = await G21CacheAlignment().process_request(ctx)
    assert "cache_control" not in result.messages[0]


@pytest.mark.asyncio
async def test_no_cache_control_with_openai_adapter_on_claude_model():
    """OpenAI adapter on a claude model must not trigger cache_control injection (T04)."""
    from providers.openai_adapter import OpenAIAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", provider_adapter=OpenAIAdapter())
    result = await G21CacheAlignment().process_request(ctx)
    assert "cache_control" not in result.messages[0]


@pytest.mark.asyncio
async def test_anthropic_adapter_enables_cache_control():
    """AnthropicAdapter set → cache_control injected (T04 positive case)."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", provider_adapter=AnthropicAdapter())
    result = await G21CacheAlignment().process_request(ctx)
    assert result.messages[0]["cache_control"] == {"type": "ephemeral"}


# ─── Test: provider detection ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_provider_noop():
    """Unknown provider skips alignment."""
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    ctx = _make_ctx(msgs, model="mistral-large-latest")
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    assert result.messages == msgs
    assert len(result.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_config_driven_provider_detection():
    """Custom provider detection via config overrides default heuristics."""
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    cfg = _make_config(
        provider_detection={
            "openai": ["mistral"],  # Map mistral to openai alignment
        }
    )
    ctx = _make_ctx(msgs, model="mistral-large-latest", config=cfg)
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    # Should have been reordered (treated as openai)
    assert result.messages[0]["role"] == "system"


@pytest.mark.asyncio
async def test_empty_provider_detection_falls_back_to_defaults():
    """An empty provider_detection dict must not disable default heuristics.

    `{}` is falsy, so the code should fall through to the built-in heuristics
    rather than treating every model as 'unknown'.
    """
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    cfg = _make_config(provider_detection={})
    ctx = _make_ctx(msgs, model="gpt-4o", config=cfg)
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)
    # gpt-4o should still be detected as openai and reordered
    assert result.messages[0]["role"] == "system"
    assert len(result.savings.step_savings) == 1


# ─── Test: original messages not mutated ─────────────────────────────────────

@pytest.mark.asyncio
async def test_original_messages_not_mutated():
    """ctx.original_messages must not be affected by reordering."""
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o")
    original_before = copy.deepcopy(ctx.original_messages)
    g21 = G21CacheAlignment()
    await g21.process_request(ctx)
    assert ctx.original_messages == original_before


# ─── Test: multiple system messages preserved ────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_system_messages_order_preserved():
    """Multiple system messages stay in their original relative order."""
    msgs = [
        {"role": "user", "content": "Q1"},
        {"role": "system", "content": "First policy."},
        {"role": "assistant", "content": "A1"},
        {"role": "system", "content": "Second policy."},
        {"role": "user", "content": "Q2"},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o")
    g21 = G21CacheAlignment()
    result = await g21.process_request(ctx)

    assert result.messages[0]["content"] == "First policy."
    assert result.messages[1]["content"] == "Second policy."
    assert all(m["role"] != "system" for m in result.messages[2:])


# ─── P1: Provider cache policy (prompt_cache_key) ────────────────────────────

@pytest.mark.asyncio
async def test_openai_sets_prompt_cache_key():
    """OpenAI adapter → deterministic, tenant-scoped prompt_cache_key on ctx.params."""
    from providers.openai_adapter import OpenAIAdapter
    msgs = [
        {"role": "system", "content": "You are a support agent. Policy: never reset passwords."},
        {"role": "user", "content": "Help"},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o", provider_adapter=OpenAIAdapter())
    result = await G21CacheAlignment().process_request(ctx)
    assert "prompt_cache_key" in result.params
    assert len(result.params["prompt_cache_key"]) == 32


@pytest.mark.asyncio
async def test_no_cache_key_without_adapter():
    """No provider_adapter → graceful, no prompt_cache_key injected."""
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o")  # provider_adapter=None
    result = await G21CacheAlignment().process_request(ctx)
    assert "prompt_cache_key" not in result.params


@pytest.mark.asyncio
async def test_anthropic_no_prompt_cache_key():
    """Anthropic uses cache_control, not a request-side prompt_cache_key."""
    from providers.anthropic_adapter import AnthropicAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx = _make_ctx(msgs, model="claude-sonnet-4-5", provider_adapter=AnthropicAdapter())
    result = await G21CacheAlignment().process_request(ctx)
    assert "prompt_cache_key" not in result.params


@pytest.mark.asyncio
async def test_prompt_cache_key_disabled_via_config():
    """providers.openai.prompt_cache_key: false → no key emitted."""
    from providers.openai_adapter import OpenAIAdapter
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    cfg = _make_config()
    cfg["groups"]["G21_cache_alignment"]["providers"]["openai"]["prompt_cache_key"] = False
    ctx = _make_ctx(msgs, model="gpt-4o", config=cfg, provider_adapter=OpenAIAdapter())
    result = await G21CacheAlignment().process_request(ctx)
    assert "prompt_cache_key" not in result.params


@pytest.mark.asyncio
async def test_prompt_cache_key_differs_per_tenant():
    """Same prefix, different tenant → different cache key (tenant isolation)."""
    from providers.openai_adapter import OpenAIAdapter
    msgs = [
        {"role": "system", "content": "Shared system prefix."},
        {"role": "user", "content": "Hi"},
    ]
    ctx_a = _make_ctx(msgs, model="gpt-4o", provider_adapter=OpenAIAdapter())
    ctx_a.tenant_id = "acme"
    ctx_b = _make_ctx(msgs, model="gpt-4o", provider_adapter=OpenAIAdapter())
    ctx_b.tenant_id = "globex"
    ra = await G21CacheAlignment().process_request(ctx_a)
    rb = await G21CacheAlignment().process_request(ctx_b)
    assert ra.params["prompt_cache_key"] != rb.params["prompt_cache_key"]


@pytest.mark.asyncio
async def test_skips_when_bypassed():
    """ctx.bypassed → no-op (no LLM call, no cache policy)."""
    from providers.openai_adapter import OpenAIAdapter
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o", provider_adapter=OpenAIAdapter())
    ctx.bypassed = True
    result = await G21CacheAlignment().process_request(ctx)
    assert result.messages == msgs                 # not reordered
    assert "prompt_cache_key" not in result.params
    assert len(result.savings.step_savings) == 0


@pytest.mark.asyncio
async def test_skips_when_cache_hit():
    """ctx.cache_hit → no-op (response already cached)."""
    from providers.openai_adapter import OpenAIAdapter
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful."},
    ]
    ctx = _make_ctx(msgs, model="gpt-4o", provider_adapter=OpenAIAdapter())
    ctx.cache_hit = True
    result = await G21CacheAlignment().process_request(ctx)
    assert result.messages == msgs
    assert "prompt_cache_key" not in result.params


# ── #33 prefix stabilisation ────────────────────────────────────────────────

from providers.openai_adapter import OpenAIAdapter


def _stabilise_cfg(patterns, enabled=True, max_chars=2000, **kw):
    cfg = _make_config(**kw)
    cfg["groups"]["G21_cache_alignment"]["stabilise"] = {
        "enabled": enabled, "patterns": patterns,
        "relocate_to": "trailing_system", "max_relocated_chars": max_chars,
    }
    return cfg


_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def _turn(ts, question):
    return [
        {"role": "system", "content": f"You are Acme support. Current time: {ts}. Be concise."},
        {"role": "user", "content": question},
    ]


class TestPrefixStabilisation:
    """A volatile value early in a system prompt invalidates the whole cached prefix, so
    every turn pays a full cache WRITE instead of a discounted read — at an identical token
    count. Relocating it out of the prefix fixes that without hiding anything from the model.
    """

    @pytest.mark.asyncio
    async def test_prefix_becomes_byte_stable_across_turns(self):
        cfg = _stabilise_cfg([_TS])
        outs = []
        for ts in ("2026-09-03T10:00:00Z", "2026-09-03T11:30:00Z"):
            ctx = _make_ctx(_turn(ts, "What are the SLA tiers?"), config=cfg,
                            provider_adapter=OpenAIAdapter())
            res = await G21CacheAlignment().process_request(ctx)
            outs.append(res.messages[0]["content"])
        assert outs[0] == outs[1], "the cached prefix still differs between turns"
        assert "2026-09-03" not in outs[0]

    @pytest.mark.asyncio
    async def test_prompt_cache_key_is_identical_across_turns(self):
        """The cheapest possible proof the fix works — and it needs no provider call.

        Drift breaks caching TWICE: the prefix bytes stop matching, and the seed feeding
        OpenAI's prompt_cache_key changes, sending the request to a different shard.
        """
        cfg = _stabilise_cfg([_TS])
        keys = []
        for ts in ("2026-09-03T10:00:00Z", "2026-09-03T11:30:00Z"):
            ctx = _make_ctx(_turn(ts, "Same question"), config=cfg,
                            provider_adapter=OpenAIAdapter())
            res = await G21CacheAlignment().process_request(ctx)
            keys.append(res.params["prompt_cache_key"])
        assert keys[0] == keys[1], "volatile span still reaches the cache-shard key"

    @pytest.mark.asyncio
    async def test_unstabilised_control_really_does_drift(self):
        """Guards against a vacuous pass: without the fix, the keys MUST differ."""
        cfg = _stabilise_cfg([_TS], enabled=False)
        keys = []
        for ts in ("2026-09-03T10:00:00Z", "2026-09-03T11:30:00Z"):
            ctx = _make_ctx(_turn(ts, "Same question"), config=cfg,
                            provider_adapter=OpenAIAdapter())
            res = await G21CacheAlignment().process_request(ctx)
            keys.append(res.params["prompt_cache_key"])
        assert keys[0] != keys[1]

    @pytest.mark.asyncio
    async def test_relocated_content_still_reaches_the_model(self):
        """Relocation, not deletion — dropping a value the prompt depends on would be a
        correctness bug in a savings costume."""
        ts = "2026-09-03T10:00:00Z"
        ctx = _make_ctx(_turn(ts, "Q"), config=_stabilise_cfg([_TS]),
                        provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        assert any(ts in (m.get("content") or "") for m in res.messages), (
            "the volatile value was dropped instead of relocated")

    @pytest.mark.asyncio
    async def test_relocation_target_is_a_trailing_system_message(self):
        """Never the first user turn: G05's L2 store recomputes its semantic key from
        post-G21 USER turns, so writing there would desynchronise store from lookup."""
        ts = "2026-09-03T10:00:00Z"
        ctx = _make_ctx(_turn(ts, "Q"), config=_stabilise_cfg([_TS]),
                        provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        assert res.messages[-1]["role"] == "system"
        assert ts in res.messages[-1]["content"]
        user_turns = [m for m in res.messages if m.get("role") == "user"]
        assert all(ts not in m["content"] for m in user_turns), "user turns must be untouched"

    @pytest.mark.asyncio
    async def test_disabled_is_byte_identical(self):
        msgs = _turn("2026-09-03T10:00:00Z", "Q")
        ctx = _make_ctx(copy.deepcopy(msgs), config=_stabilise_cfg([_TS], enabled=False),
                        provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        assert [m["content"] for m in res.messages] == [m["content"] for m in msgs]

    @pytest.mark.asyncio
    async def test_invalid_pattern_is_skipped_not_fatal(self):
        ctx = _make_ctx(_turn("2026-09-03T10:00:00Z", "Q"),
                        config=_stabilise_cfg(["(unclosed", _TS]),
                        provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        assert "2026-09-03" not in res.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_oversized_span_is_left_in_place_not_truncated(self):
        """A truncated identifier reaching the model is worse than an unstable prefix."""
        ctx = _make_ctx(_turn("2026-09-03T10:00:00Z", "Q"),
                        config=_stabilise_cfg([_TS], max_chars=2),
                        provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        assert "2026-09-03T10:00:00Z" in res.messages[0]["content"]


class TestSharedPrefixProfile:
    """Cross-artefact convergence: artefacts declaring one profile share a cache shard
    instead of each paying to build their own copy."""

    @pytest.mark.asyncio
    async def _key_for(self, system_text, profile=None):
        cfg = _make_config()
        if profile:
            cfg["groups"]["G21_cache_alignment"]["prefix_profile"] = profile
        ctx = _make_ctx([{"role": "system", "content": system_text},
                         {"role": "user", "content": "Q"}],
                        config=cfg, provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        return res.params["prompt_cache_key"]

    @pytest.mark.asyncio
    async def test_same_profile_converges_divergent_prompts(self):
        a = await self._key_for("You are Acme support. Be concise.", profile="support-v1")
        b = await self._key_for("You are Acme support. Be brief.", profile="support-v1")
        assert a == b, "artefacts sharing a profile must share one cache shard"

    @pytest.mark.asyncio
    async def test_different_profiles_stay_separate(self):
        a = await self._key_for("Same prompt.", profile="support-v1")
        b = await self._key_for("Same prompt.", profile="billing-v1")
        assert a != b

    @pytest.mark.asyncio
    async def test_no_profile_keeps_content_derived_key(self):
        a = await self._key_for("Prompt A")
        b = await self._key_for("Prompt B")
        assert a != b


class TestStabilisationRespectsG05CacheKeys:
    """G21 mutates ctx.messages AFTER G05 computed its lookup key, which is safe only
    because of two properties. Stabilisation must not break either.

    1. L1 memoises its key on ctx.params["_g05_l1_cache_key"] at lookup and reuses it at
       store, so post-G21 edits cannot split store from lookup.
    2. L2's semantic text is built from USER turns only — which is exactly why relocation
       targets a trailing SYSTEM message and never the first user turn.
    """

    @pytest.mark.asyncio
    async def test_l1_memoised_key_is_untouched(self):
        ctx = _make_ctx(_turn("2026-09-03T10:00:00Z", "Q"), config=_stabilise_cfg([_TS]),
                        provider_adapter=OpenAIAdapter())
        ctx.params["_g05_l1_cache_key"] = "t:acme:tok_opt:l1:precomputed"
        res = await G21CacheAlignment().process_request(ctx)
        assert res.params["_g05_l1_cache_key"] == "t:acme:tok_opt:l1:precomputed", (
            "G21 must never disturb the memoised L1 key — the store path reuses it")

    @pytest.mark.asyncio
    async def test_l2_semantic_text_is_unchanged_by_stabilisation(self):
        """L2 recomputes its query text from post-G21 messages at STORE time. If
        stabilisation altered user turns, store and lookup would key differently and the
        semantic cache would quietly fill with unreachable entries."""
        from middleware.g05_cache import _semantic_query_text
        msgs = _turn("2026-09-03T10:00:00Z", "What are the SLA tiers?")
        before = _semantic_query_text(copy.deepcopy(msgs))
        ctx = _make_ctx(msgs, config=_stabilise_cfg([_TS]), provider_adapter=OpenAIAdapter())
        res = await G21CacheAlignment().process_request(ctx)
        assert _semantic_query_text(res.messages) == before

