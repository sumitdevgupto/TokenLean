"""Unit tests for G28 — Contextual Content Reuse (CCR).

Focus: the system-role guard. In a pass-through chat completion there is no agent
loop to resolve a [CCR:ref] via headroom_retrieve, so G28 must never replace the
system instruction by default (doing so strips the policy/facts the answer needs).
"""
import json
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest


class _FakeRedis:
    """Minimal async Redis standing in for the shared pool.

    CCR's store is now Redis-backed on purpose: a reference that lives only in one
    process's memory cannot be resolved by the next request, which is why the feature was
    switched off. Tests therefore need a durable store, not a monkeypatched flag.
    """

    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttls[key] = ex

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if self.data.pop(k, None) is not None:
                self.ttls.pop(k, None)
                n += 1
        return n


@pytest.fixture
def fake_redis(monkeypatch):
    import middleware.g28_ccr as g28
    r = _FakeRedis()
    monkeypatch.setattr("cache.redis_pool.get_redis", lambda: r)
    g28._local_store.clear()
    g28._stats.clear()
    # No _resolvers_proven to clear: the proof is Redis-backed (backlog #40) and a
    # fresh _FakeRedis per test isolates it.
    return r


@pytest.fixture
def no_redis(monkeypatch):
    """Durable store unreachable — CCR must refuse to substitute, not fall back to RAM."""
    import middleware.g28_ccr as g28
    def _boom():
        raise ConnectionError("no redis")
    monkeypatch.setattr("cache.redis_pool.get_redis", _boom)
    g28._local_store.clear()


@pytest.fixture
def proven_resolver(fake_redis, monkeypatch):
    """A client that has already demonstrated it can resolve a reference.

    Substitution is earned, never assumed: see the handshake in process_request.
    """
    import middleware.g28_ccr as g28
    async def _always_proven(prefix):
        return True
    monkeypatch.setattr(g28, "_resolver_proven", _always_proven)
    return fake_redis


# Back-compat alias: the old fixture flipped _STORE_IS_DURABLE, which is now True in
# production. What tests actually need is a working durable store.
@pytest.fixture
def ccr_available(fake_redis):
    return fake_redis


# A block comfortably over the default min_tokens (300) regardless of the estimator.
_BIG = "Policy: eu-west and eu-central are GDPR-compliant for EU data residency. " * 100


# A reference is only substituted for a caller that ALREADY sends tools, because that is the
# only caller the CCR tools get advertised to. Tests that expect substitution must therefore
# look like an agent; tests that expect full content must not.
_AGENT_TOOLS = [{"type": "function", "function": {"name": "search_logs",
                                                  "description": "search", "parameters": {}}}]


@pytest.mark.asyncio
class TestProcessMessagesSystemGuard:
    """Direct tests for _process_messages role gating.

    `may_substitute=True` throughout: these test WHICH roles are eligible, not whether the
    caller earned substitution (that is TestResolveCapabilityHandshake).
    """

    async def test_system_role_preserved_by_default(self, fake_redis):
        from middleware.g28_ccr import _process_messages
        messages = [
            {"role": "system", "content": _BIG},
            {"role": "user", "content": _BIG},
        ]
        new_msgs, before, after = await _process_messages(
            messages, 300, "gpt-4o-mini", 3600, may_substitute=True)
        # System instruction is preserved verbatim...
        assert new_msgs[0]["content"] == _BIG
        assert "[CCR:" not in new_msgs[0]["content"]
        # ...while the user block (over threshold) is replaced by a compact reference.
        assert new_msgs[1]["content"].startswith("[CCR:")
        assert after < before

    async def test_system_role_compressed_when_opted_in(self, fake_redis):
        from middleware.g28_ccr import _process_messages
        messages = [{"role": "system", "content": _BIG}]
        new_msgs, before, after = await _process_messages(
            messages, 300, "gpt-4o-mini", 3600, compress_system=True, may_substitute=True
        )
        assert new_msgs[0]["content"].startswith("[CCR:")
        assert after < before

    async def test_short_content_left_untouched(self, fake_redis):
        from middleware.g28_ccr import _process_messages
        messages = [{"role": "user", "content": "hello there"}]
        new_msgs, before, after = await _process_messages(
            messages, 300, "gpt-4o-mini", 3600, may_substitute=True)
        assert new_msgs[0]["content"] == "hello there"
        assert before == after


@pytest.mark.asyncio
class TestG28ProcessRequest:
    async def test_disabled_is_noop(self, make_ctx):
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": False}
        original = list(ctx.messages)
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages == original

    async def test_system_prompt_preserved_by_default(self, make_ctx):
        ctx = make_ctx(
            [{"role": "system", "content": _BIG},
             {"role": "user", "content": "Which regions are GDPR compliant?"}],
            model="gpt-4o-mini",
        )
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "min_tokens": 300}
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["role"] == "system"
        assert ctx.messages[0]["content"] == _BIG  # verbatim, not a [CCR:...] reference

    async def test_compress_system_prompt_flag_wired(self, make_ctx, proven_resolver):
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini",
                       params={"tools": _AGENT_TOOLS})
        ctx.config["groups"]["G28_ccr"] = {
            "enabled": True, "min_tokens": 300, "compress_system_prompt": True,
        }
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["content"].startswith("[CCR:")

    async def test_per_tenant_override_deep_merges(self, make_ctx, proven_resolver):
        # A tenant flips compress_system_prompt without re-declaring the block; the
        # base keys (enabled/min_tokens) must survive the merge or G28 would no-op.
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini",
                       params={"tools": _AGENT_TOOLS})
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "min_tokens": 300}
        ctx.config.setdefault("tenants", {})["acme"] = {
            "groups": {"G28_ccr": {"compress_system_prompt": True}}
        }
        ctx.tenant_id = "acme"
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["content"].startswith("[CCR:")


class TestG28ProcessResponse:
    """G28 has a SECOND dispatch loop, near-identical to G15's, that had no test at all.

    Two loops executing the same sink must gate identically or the weaker one becomes the
    way in — which is exactly how the missing tenant prefix survived in G15 while G28's
    copy had it right (public 2768392).
    """

    @staticmethod
    def _resp(name="headroom_compress", args='{"text": "xxxxxxxxxxxxxxxxxxxx"}'):
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": name, "arguments": args}}]}}]}

    @staticmethod
    def _executed(out):
        return "result" in out["choices"][0]["message"]["tool_calls"][0]["function"]

    async def _run(self, make_ctx, *, injected, policy=None, mode="flag"):
        from middleware.g28_ccr import G28CCR, _local_store
        _local_store.clear()
        ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "ttl_seconds": 60}
        ctx.config["groups"]["G32_tool_eligibility"] = {
            "enabled": True, "mode": mode, "policy": policy or {}}
        ctx.ccr_tools_injected = injected
        return ctx, await G28CCR().process_response(ctx, self._resp())

    async def test_refuses_when_not_injected(self, make_ctx, ccr_available):
        ctx, out = await self._run(make_ctx, injected=False)
        assert not self._executed(out)
        assert ctx.tool_dispatch_blocked == ["headroom_compress"]

    async def test_refuses_a_policy_denied_tool_in_flag_mode(self, make_ctx, ccr_available):
        ctx, out = await self._run(
            make_ctx, injected=True, mode="flag", policy={"deny": ["headroom_*"]})
        assert not self._executed(out)

    async def test_dispatches_when_injected_and_permitted(self, make_ctx, ccr_available):
        _, out = await self._run(make_ctx, injected=True)
        assert self._executed(out)

    async def test_parity_with_g15(self, make_ctx, ccr_available):
        """Both loops must reach the same verdict for the same input."""
        from middleware.g32_tool_eligibility import authorize_dispatch
        ctx, _ = await self._run(make_ctx, injected=False)
        assert authorize_dispatch(ctx, "headroom_compress") == "not_injected"


@pytest.mark.asyncio
class TestHeadroomStatsIsTenantScoped:
    """headroom_stats returned process-global numbers - a cross-tenant side channel.

    len(_local_store) counted every tenant's blocks and _stats was one module-level pair,
    so polling the tool across turns revealed co-tenants' request volume, block-size
    distribution and activity timing. Content was safe after the prefix fix; counts were not.
    """

    async def test_stats_report_only_the_calling_tenants_blocks(self, fake_redis):
        from middleware.g28_ccr import _local_store, dispatch_mcp_tool
        await dispatch_mcp_tool("headroom_compress", {"text": "acme one"}, 60, prefix="t:acme:")
        await dispatch_mcp_tool("headroom_compress", {"text": "acme two"}, 60, prefix="t:acme:")
        for i in range(5):
            await dispatch_mcp_tool("headroom_compress", {"text": f"globex {i}"}, 60,
                                    prefix="t:globex:")
        acme = await dispatch_mcp_tool("headroom_stats", {}, 60, prefix="t:acme:")
        globex = await dispatch_mcp_tool("headroom_stats", {}, 60, prefix="t:globex:")
        assert acme["local_store_size"] == 2
        assert globex["local_store_size"] == 5
        assert len(_local_store) == 7, "sanity: the process really does hold both tenants"

    async def test_hit_miss_counters_do_not_leak_across_tenants(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "acme secret"}, 60,
                                         prefix="t:acme:")
        for _ in range(3):
            await dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, 60,
                                    prefix="t:acme:")
        globex = await dispatch_mcp_tool("headroom_stats", {}, 60, prefix="t:globex:")
        assert globex["hits"] == 0 and globex["misses"] == 0, (
            "globex reads acme retrieval activity - an activity-timing side channel")
        acme = await dispatch_mcp_tool("headroom_stats", {}, 60, prefix="t:acme:")
        assert acme["hits"] == 3


@pytest.mark.asyncio
class TestReferencesAreExactAndTenantScoped:
    """References carry the FULL sha and resolve by exact key - no prefix scan.

    The old ref was sha[:8] (32 bits) and retrieval returned the first insertion-order key
    with that prefix, so a collision handed the model a DIFFERENT document with a hit
    counted and no error anywhere (~1.2% at 10k blocks). Worse, the DEFAULT tenant prefix
    is the empty string, so startswith(prefix) matched every tenant key.
    """

    async def test_reference_carries_the_full_sha(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        out = await dispatch_mcp_tool("headroom_compress", {"text": "hello"}, 60, prefix="t:a:")
        assert out["ref"] == "[CCR:" + out["sha256"] + "]"
        assert len(out["sha256"]) == 64

    async def test_truncated_reference_is_rejected_not_guessed(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        out = await dispatch_mcp_tool("headroom_compress", {"text": "hello"}, 60, prefix="t:a:")
        short = "[CCR:" + out["sha256"][:8] + "]"
        res = await dispatch_mcp_tool("headroom_retrieve", {"ref": short}, 60, prefix="t:a:")
        assert "error" in res, "an 8-char ref must not resolve by scanning"

    async def test_default_tenant_cannot_read_other_tenants_blocks(self, fake_redis):
        """The default tenant prefix is empty - under the old scan it saw everything."""
        from middleware.g28_ccr import dispatch_mcp_tool
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "acme confidential"},
                                         60, prefix="t:acme:")
        leaked = await dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, 60,
                                         prefix="")
        assert "error" in leaked and "text" not in leaked

    async def test_owning_tenant_still_resolves(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "acme doc"}, 60,
                                         prefix="t:acme:")
        got = await dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, 60,
                                      prefix="t:acme:")
        assert got["text"] == "acme doc"

    async def test_malformed_reference_is_rejected(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        for bad in ("[CCR:nothex]", "not-a-ref", "[CCR:]", "[CCR:" + "z" * 64 + "]"):
            res = await dispatch_mcp_tool("headroom_retrieve", {"ref": bad}, 60, prefix="t:a:")
            assert "error" in res


@pytest.mark.asyncio
class TestContentAddressedStoreIsIdempotent:
    """The key IS sha256(value), so concurrent writers of the same content converge.

    This is the property that makes a shared cross-artefact store tractable instead of a
    distributed-locking problem - and it lets a second app reuse the first app stored
    block rather than writing its own copy.
    """

    async def test_same_content_yields_one_key_and_one_ref(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        a = await dispatch_mcp_tool("headroom_compress", {"text": "shared doc"}, 60, prefix="t:a:")
        b = await dispatch_mcp_tool("headroom_compress", {"text": "shared doc"}, 60, prefix="t:a:")
        assert a["ref"] == b["ref"]
        assert len(fake_redis.data) == 1, "identical content must not store twice"

    async def test_concurrent_writers_converge(self, fake_redis):
        import asyncio
        from middleware.g28_ccr import dispatch_mcp_tool
        results = await asyncio.gather(*[
            dispatch_mcp_tool("headroom_compress", {"text": "same block"}, 60, prefix="t:a:")
            for _ in range(8)
        ])
        assert len({r["ref"] for r in results}) == 1
        assert len(fake_redis.data) == 1

    async def test_different_tenants_stay_separate(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        await dispatch_mcp_tool("headroom_compress", {"text": "same"}, 60, prefix="t:a:")
        await dispatch_mcp_tool("headroom_compress", {"text": "same"}, 60, prefix="t:b:")
        assert len(fake_redis.data) == 2, "content-addressing must not cross tenants"

    async def test_oversized_input_is_refused(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        res = await dispatch_mcp_tool("headroom_compress", {"text": "x" * 50}, 60,
                                      prefix="t:a:", max_store_chars=10)
        assert "error" in res and not fake_redis.data


@pytest.mark.asyncio
class TestResolveCapabilityHandshake:
    """NEVER substitute a reference the caller cannot resolve.

    This is the 2026-06-30 regression, encoded. CCR replaced a 676-token system prompt with
    a reference in a pass-through completion that had no agent loop to call
    headroom_retrieve; the model answered from generic knowledge on a billed 200. The
    server-side resolve fix was rejected as ~25% net-negative. So a client must EARN
    substitution by demonstrating it can resolve one.
    """

    async def test_unproven_client_receives_full_content(self, fake_redis, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"] == _BIG, (
            "a client that has never resolved a reference must not be sent one")

    async def test_content_is_still_stored_for_later_reuse(self, fake_redis, make_ctx):
        """The first turn is honest AND useful: storing is what makes later turns cheap,
        including for a second artefact sending the same document."""
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        await G28CCR().process_request(ctx)
        assert fake_redis.data, "content should be stored even when not substituted"

    async def test_proven_client_gets_the_reference(self, proven_resolver, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini",
                       params={"tools": _AGENT_TOOLS})
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"].startswith("[CCR:")

    async def test_resolving_a_reference_proves_capability(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool, _resolver_proven
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "doc"}, 60, prefix="t:a:")
        assert not await _resolver_proven("t:a:")
        await dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, 60, prefix="t:a:")
        assert await _resolver_proven("t:a:"), "a successful retrieve is the proof"

    async def test_proof_does_not_leak_between_tenants(self, fake_redis):
        from middleware.g28_ccr import dispatch_mcp_tool, _resolver_proven
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "doc"}, 60, prefix="t:a:")
        await dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, 60, prefix="t:a:")
        assert not await _resolver_proven("t:b:")

    async def test_opt_out_restores_unconditional_substitution(self, fake_redis, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini",
                       params={"tools": _AGENT_TOOLS})
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "require_proven_resolver": False}
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"].startswith("[CCR:")


@pytest.mark.asyncio
class TestNeverSubstituteWhatCannotBeResolved:
    """A reference is only resolvable if the caller was offered headroom_retrieve.

    The CCR tools are advertised only to callers that already send tools. So for a caller
    that sends none, a [CCR:ref] is unresolvable BY CONSTRUCTION - there is no tool to call.
    That is a physical impossibility, not a trust decision, so unlike the proven-resolver
    handshake it is not overridable.

    DS22's first two live runs (2026-09-03) are the evidence: no tools in the dataset,
    `require_proven_resolver: false` waving the soft guard through, and the model answering
    from its own summary with every planted fact gone - while the run recorded 44.99%
    "savings". Wiring a retrieve loop into the harness did not help, because the model was
    never given a tool to call.
    """

    async def test_toolless_caller_gets_full_content(self, proven_resolver, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"] == _BIG

    async def test_the_override_cannot_reach_this_guard(self, fake_redis, make_ctx):
        """require_proven_resolver: false is an operator trust decision. It must not be able
        to authorise a substitution that nothing could ever resolve."""
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "require_proven_resolver": False}
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"] == _BIG

    async def test_content_is_still_stored(self, fake_redis, make_ctx):
        """Refusing to substitute must not refuse to store - the parking is what makes a
        later agentic turn cheap."""
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        await G28CCR().process_request(ctx)
        assert fake_redis.data

    async def test_expose_mcp_tools_off_also_blocks_substitution(self, proven_resolver, make_ctx):
        """Tools sent, but advertising disabled — still nothing to call."""
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini",
                       params={"tools": _AGENT_TOOLS})
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "expose_mcp_tools": False}
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"] == _BIG


@pytest.mark.asyncio
class TestProofIsRevokedWhenTheModelIgnoresAReference:
    """The handshake used to be write-once: resolve once, trusted until the TTL expired.

    Proven live on 2026-09-03 (DS22, the first real ablation run of G28): with the CCR tools
    advertised and the handshake force-declared, gpt-4o-mini returned `tool_calls: []` /
    `finish_reason: stop` and answered from the one-line summary, inventing the maintenance
    window and region that lived in the parked document. Every graded thread lost both
    planted facts while the run recorded 44.99% savings. A saving that buys a wrong answer
    is not a saving, and nothing detected it.

    So the proof now decays on evidence: substitute a reference, get a FINAL answer back
    that never resolved it, and the tenant returns to full content until it resolves one
    again. Costs at most one honest full-content turn; the alternative costs the answer.
    """

    @staticmethod
    def _final_answer(content="the window is 2am-4am"):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}]}

    @staticmethod
    def _retrieve_call(ref):
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "headroom_retrieve",
                    "arguments": json.dumps({"ref": ref})}}]}}]}

    async def _ctx(self, make_ctx, *, substituted):
        ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "ttl_seconds": 60}
        ctx.ccr_tools_injected = True
        ctx.ccr_refs_substituted = substituted
        return ctx

    async def test_answering_without_resolving_revokes_the_proof(self, make_ctx, ccr_available):
        from middleware.g28_ccr import G28CCR, _mark_resolver_proven, _resolver_proven
        ctx = await self._ctx(make_ctx, substituted=True)
        await _mark_resolver_proven(ctx.redis_prefix)
        assert await _resolver_proven(ctx.redis_prefix)
        await G28CCR().process_response(ctx, self._final_answer())
        assert not await _resolver_proven(ctx.redis_prefix), (
            "the model answered from the summary — it must not keep receiving references")

    async def test_next_turn_gets_full_content_again(self, make_ctx, ccr_available):
        """The revocation has to CHANGE something, not just clear a flag."""
        from middleware.g28_ccr import G28CCR, _mark_resolver_proven
        ctx = await self._ctx(make_ctx, substituted=True)
        await _mark_resolver_proven(ctx.redis_prefix)
        await G28CCR().process_response(ctx, self._final_answer())

        nxt = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        nxt.config["groups"]["G28_ccr"] = {"enabled": True}
        out = await G28CCR().process_request(nxt)
        assert out.messages[0]["content"] == _BIG

    async def test_a_resolving_turn_keeps_the_proof(self, make_ctx, ccr_available):
        from middleware.g28_ccr import (G28CCR, dispatch_mcp_tool, _mark_resolver_proven,
                                        _resolver_proven)
        ctx = await self._ctx(make_ctx, substituted=True)
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "doc" * 50}, 60,
                                         prefix=ctx.redis_prefix)
        await _mark_resolver_proven(ctx.redis_prefix)
        await G28CCR().process_response(ctx, self._retrieve_call(stored["ref"]))
        assert await _resolver_proven(ctx.redis_prefix), "it DID resolve — do not punish it"

    async def test_mid_loop_turn_is_not_judged(self, make_ctx, ccr_available):
        """tool_calls present = the conversation is still running; it may retrieve next turn.
        Revoking here would thrash a working agent on every non-CCR tool call it makes."""
        from middleware.g28_ccr import G28CCR, _mark_resolver_proven, _resolver_proven
        ctx = await self._ctx(make_ctx, substituted=True)
        await _mark_resolver_proven(ctx.redis_prefix)
        other_tool = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "search_logs", "arguments": "{}"}}]}}]}
        await G28CCR().process_response(ctx, other_tool)
        assert await _resolver_proven(ctx.redis_prefix)

    async def test_no_substitution_means_nothing_to_ignore(self, make_ctx, ccr_available):
        """A plain answer on a turn that sent no reference is not evidence of anything."""
        from middleware.g28_ccr import G28CCR, _mark_resolver_proven, _resolver_proven
        ctx = await self._ctx(make_ctx, substituted=False)
        await _mark_resolver_proven(ctx.redis_prefix)
        await G28CCR().process_response(ctx, self._final_answer())
        assert await _resolver_proven(ctx.redis_prefix)

    async def test_the_failure_is_counted(self, make_ctx, ccr_available, monkeypatch):
        """Silent degradation is the whole problem: the request succeeds and only the answer
        is wrong, so this must leave a countable trace."""
        import middleware.g28_ccr as g28
        seen = []
        monkeypatch.setattr(g28, "_record_ignored_reference", lambda p: seen.append(p))
        ctx = await self._ctx(make_ctx, substituted=True)
        await g28._mark_resolver_proven(ctx.redis_prefix)
        await g28.G28CCR().process_response(ctx, self._final_answer())
        assert seen == [ctx.redis_prefix]


@pytest.mark.asyncio
class TestStoreUnavailableRefusesToSubstitute:
    """No durable store means send the content. Never fall back to process memory.

    A reference resolvable only inside one process is the original bug: it fails on a
    billed 200 with the model improvising from its own earlier paraphrase, which presents
    as a model-quality problem rather than a storage outage.
    """

    async def test_no_redis_means_full_content(self, no_redis, make_ctx, monkeypatch):
        import middleware.g28_ccr as g28
        async def _always(prefix):
            return True
        monkeypatch.setattr(g28, "_resolver_proven", _always)
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        out = await g28.G28CCR().process_request(ctx)
        assert out.messages[0]["content"] == _BIG

    async def test_compress_tool_reports_the_outage(self, no_redis):
        from middleware.g28_ccr import dispatch_mcp_tool
        res = await dispatch_mcp_tool("headroom_compress", {"text": "doc"}, 60, prefix="t:a:")
        assert "error" in res

    async def test_missing_reference_is_loud(self, fake_redis, caplog):
        """A miss used to be a debug line on a billed 200 - invisible."""
        import logging
        from middleware.g28_ccr import dispatch_mcp_tool
        with caplog.at_level(logging.WARNING):
            res = await dispatch_mcp_tool(
                "headroom_retrieve", {"ref": "[CCR:" + "a" * 64 + "]"}, 60, prefix="t:a:")
        assert "error" in res
        assert any("not found" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
class TestToolsAreOnlyOfferedToAgenticCallers:
    """CCR advertises its tools ONLY to callers that already send tools.

    Injecting three tool definitions into every request would change the tool block - part
    of the cached prefix - churning the very prefix G21 stabilises, add tokens to requests
    that can never use them, and hand tools to a pass-through caller that sent none, which
    can make the model emit tool_calls the client never asked for.
    """

    async def test_no_tools_in_means_no_tools_out(self, fake_redis, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        out = await G28CCR().process_request(ctx)
        assert not out.params.get("tools")
        assert out.ccr_tools_injected is False

    async def test_agentic_caller_is_offered_the_tools(self, fake_redis, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        ctx.params["tools"] = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        out = await G28CCR().process_request(ctx)
        names = {t["function"]["name"] for t in out.params["tools"]}
        assert "headroom_retrieve" in names
        assert "search" in names, "the caller own tools must survive"
        assert out.ccr_tools_injected is True


class TestReferenceParsingToleratesModelFormatting:
    """The security property is the 64-hex exact-key lookup, not the brackets.

    A model copying `[CCR:<sha>]` out of a prompt re-emits it in whatever shape it considers
    clean - it reads the delimiters as markup. On 2026-09-03 DS22 thread 02 called
    headroom_retrieve TWICE with `"CCR:01bbfa2c..."`, was refused both times for the missing
    brackets alone, then improvised an answer - on a billed 200, with a sha that was perfectly
    correct. Sibling threads that happened to keep the brackets resolved and answered right.
    """

    SHA = "a" * 64

    def _parse(self, ref):
        from middleware.g28_ccr import _sha_from_ref
        return _sha_from_ref(ref)

    def test_the_canonical_form_works(self):
        assert self._parse(f"[CCR:{self.SHA}]") == self.SHA

    def test_brackets_are_optional(self):
        assert self._parse(f"CCR:{self.SHA}") == self.SHA

    def test_a_bare_sha_works(self):
        assert self._parse(self.SHA) == self.SHA

    def test_surrounding_whitespace_is_ignored(self):
        assert self._parse(f"  [CCR:{self.SHA}]  ") == self.SHA

    def test_case_is_normalised(self):
        assert self._parse(f"[CCR:{self.SHA.upper()}]") == self.SHA

    def test_a_short_hash_is_still_refused(self):
        """#29: 8-char refs resolved by SCANNING returned a DIFFERENT document on collision.
        Tolerating the wrapper must not reopen that."""
        assert self._parse(f"[CCR:{'a' * 8}]") is None

    def test_non_hex_is_refused(self):
        assert self._parse(f"[CCR:{'z' * 64}]") is None

    def test_junk_is_refused(self):
        assert self._parse("") is None
        assert self._parse(None) is None
        assert self._parse("the runbook") is None


@pytest.mark.asyncio
class TestResolverProofSurvivesScaleOut:
    """Backlog #40 — the proof and its revocation must reach every worker/instance.

    The record used to be a module-level dict, which is private to one uvicorn worker in
    one container. Missing PROOF is fail-safe (unproven → full content). Missing
    REVOCATION is not: a client that demonstrably stopped resolving kept receiving
    references from every OTHER worker and instance, answering from the summary on a
    billed 200. The whole value of the 2026-09-03 revocation guard was that it is fast,
    and a per-process guard is not fast anywhere but its own process.
    """

    async def test_proof_is_written_to_redis_under_the_tenant_key(self, fake_redis):
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("t:acme:")
        assert "t:acme:ccr:resolver_proven" in fake_redis.data

    async def test_default_tenant_key_is_bare_but_still_exact(self, fake_redis):
        """The default tenant's prefix is "" — the key must still be an exact lookup,
        never a prefix scan (the historical startswith("") cross-tenant hole)."""
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("")
        assert set(fake_redis.data) == {"ccr:resolver_proven"}
        assert not await g28._resolver_proven("t:other:")

    async def test_proof_carries_the_configured_ttl(self, fake_redis):
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("t:a:", ttl=120)
        assert fake_redis.ttls["t:a:ccr:resolver_proven"] == 120

    async def test_ttl_defaults_to_one_hour(self, fake_redis):
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("t:a:")
        assert fake_redis.ttls["t:a:ccr:resolver_proven"] == 3600

    async def test_dispatch_threads_the_operator_ttl(self, fake_redis):
        """The knob has to reach the sink, not just exist in config."""
        from middleware.g28_ccr import dispatch_mcp_tool
        stored = await dispatch_mcp_tool("headroom_compress", {"text": "doc"}, 60, prefix="t:a:")
        await dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, 60,
                                prefix="t:a:", proof_ttl=900)
        assert fake_redis.ttls["t:a:ccr:resolver_proven"] == 900

    async def test_a_second_instance_sees_the_proof(self, fake_redis):
        """Instance B shares only Redis with instance A — no module state at all."""
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("t:a:")
        # Nothing but the shared store carries the fact across.
        assert not hasattr(g28, "_resolvers_proven")
        assert await g28._resolver_proven("t:a:")

    async def test_a_second_instance_sees_the_REVOCATION(self, fake_redis):
        """The direction that was actually broken: A revokes, B must stop substituting."""
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("t:a:")
        await g28._revoke_resolver_proof("t:a:")
        assert "t:a:ccr:resolver_proven" not in fake_redis.data
        assert not await g28._resolver_proven("t:a:")

    async def test_the_per_instance_dict_must_not_come_back(self):
        """Regression guard: reintroducing the dict silently re-breaks revocation."""
        import middleware.g28_ccr as g28
        assert not hasattr(g28, "_resolvers_proven"), (
            "resolver proof must live in Redis — a module dict does not propagate across "
            "uvicorn workers or Cloud Run instances (backlog #40)"
        )

    async def test_redis_failure_on_lookup_reads_as_unproven(self, no_redis):
        """Fail-SAFE: an unreachable store must degrade to full content, never raise."""
        import middleware.g28_ccr as g28
        assert await g28._resolver_proven("t:a:") is False

    async def test_redis_failure_on_mark_is_quiet(self, no_redis):
        """Worst case is one more honest full-content turn — not worth a WARNING."""
        import middleware.g28_ccr as g28
        await g28._mark_resolver_proven("t:a:")  # must not raise

    async def test_failed_revoke_is_LOUD(self, no_redis, caplog):
        """The asymmetric one: a failed revoke leaves a stale proof live on every
        instance until its TTL, so it must not pass silently."""
        import logging
        import middleware.g28_ccr as g28
        with caplog.at_level(logging.WARNING):
            await g28._revoke_resolver_proof("t:a:")
        assert any("revoke resolver proof" in r.message for r in caplog.records)

    async def test_revocation_through_the_real_response_path(self, fake_redis, make_ctx):
        """End-to-end: the pipeline's revoke reaches Redis, not a local dict."""
        import middleware.g28_ccr as g28
        ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        ctx.ccr_refs_substituted = True
        await g28._mark_resolver_proven(ctx.redis_prefix)
        assert g28._proof_key(ctx.redis_prefix) in fake_redis.data
        await g28.G28CCR().process_response(ctx, {
            "choices": [{"message": {"role": "assistant", "content": "answered from memory"},
                         "finish_reason": "stop"}]
        })
        assert g28._proof_key(ctx.redis_prefix) not in fake_redis.data
