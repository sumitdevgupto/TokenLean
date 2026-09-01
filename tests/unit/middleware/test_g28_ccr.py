"""Unit tests for G28 — Contextual Content Reuse (CCR).

Focus: the system-role guard. In a pass-through chat completion there is no agent
loop to resolve a [CCR:ref] via headroom_retrieve, so G28 must never replace the
system instruction by default (doing so strips the policy/facts the answer needs).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest


@pytest.fixture
def ccr_available(monkeypatch):
    """G28 refuses to run while its store is in-process only (see _STORE_IS_DURABLE).

    Tests of G28's own behaviour need the store treated as usable, so they opt in here.
    Availability is a separate guarantee, covered by TestCcrRefusesWhileStoreIsEphemeral —
    keeping them apart means flipping the real flag during the #28 work does not quietly
    turn the availability tests into no-ops.
    """
    import middleware.g28_ccr as g28
    monkeypatch.setattr(g28, "_STORE_IS_DURABLE", True)
    monkeypatch.setattr(g28, "_UNAVAILABLE_LOGGED", False)

# A block comfortably over the default min_tokens (300) regardless of the estimator.
_BIG = "Policy: eu-west and eu-central are GDPR-compliant for EU data residency. " * 100


class TestProcessMessagesSystemGuard:
    """Direct tests for _process_messages role gating."""

    def test_system_role_preserved_by_default(self):
        from middleware.g28_ccr import _process_messages
        messages = [
            {"role": "system", "content": _BIG},
            {"role": "user", "content": _BIG},
        ]
        new_msgs, before, after = _process_messages(messages, None, 300, "gpt-4o-mini", 3600)
        # System instruction is preserved verbatim...
        assert new_msgs[0]["content"] == _BIG
        assert "[CCR:" not in new_msgs[0]["content"]
        # ...while the user block (over threshold) is replaced by a compact reference.
        assert new_msgs[1]["content"].startswith("[CCR:")
        assert after < before

    def test_system_role_compressed_when_opted_in(self):
        from middleware.g28_ccr import _process_messages
        messages = [{"role": "system", "content": _BIG}]
        new_msgs, before, after = _process_messages(
            messages, None, 300, "gpt-4o-mini", 3600, compress_system=True
        )
        assert new_msgs[0]["content"].startswith("[CCR:")
        assert after < before

    def test_short_content_left_untouched(self):
        from middleware.g28_ccr import _process_messages
        messages = [{"role": "user", "content": "hello there"}]
        new_msgs, before, after = _process_messages(messages, None, 300, "gpt-4o-mini", 3600)
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

    async def test_compress_system_prompt_flag_wired(self, make_ctx, ccr_available):
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {
            "enabled": True, "min_tokens": 300, "compress_system_prompt": True,
        }
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["content"].startswith("[CCR:")

    async def test_per_tenant_override_deep_merges(self, make_ctx, ccr_available):
        # A tenant flips compress_system_prompt without re-declaring the block; the
        # base keys (enabled/min_tokens) must survive the merge or G28 would no-op.
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini")
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


class TestHeadroomStatsIsTenantScoped:
    """`headroom_stats` returned process-global numbers — a cross-tenant side channel.

    `len(_local_store)` counted every tenant's blocks and `_stats` was one module-level
    pair, so polling the tool across turns revealed co-tenants' request volume, block-size
    distribution and activity timing. Content was safe after the prefix fix; the counts
    were not.
    """

    def test_stats_report_only_the_calling_tenants_blocks(self):
        from middleware.g28_ccr import _local_store, _stats, dispatch_mcp_tool
        _local_store.clear()
        _stats.clear()

        dispatch_mcp_tool("headroom_compress", {"text": "acme one"}, None, 60, prefix="t:acme:")
        dispatch_mcp_tool("headroom_compress", {"text": "acme two"}, None, 60, prefix="t:acme:")
        for i in range(5):
            dispatch_mcp_tool("headroom_compress", {"text": f"globex {i}"}, None, 60,
                              prefix="t:globex:")

        acme = dispatch_mcp_tool("headroom_stats", {}, None, 60, prefix="t:acme:")
        globex = dispatch_mcp_tool("headroom_stats", {}, None, 60, prefix="t:globex:")

        assert acme["local_store_size"] == 2, (
            f"acme sees {acme['local_store_size']} blocks — the process holds 7, so it is "
            f"reading globex's traffic volume"
        )
        assert globex["local_store_size"] == 5
        assert len(_local_store) == 7, "sanity: the process really does hold both tenants"

    def test_hit_miss_counters_do_not_leak_across_tenants(self):
        from middleware.g28_ccr import _local_store, _stats, dispatch_mcp_tool
        _local_store.clear()
        _stats.clear()

        stored = dispatch_mcp_tool("headroom_compress", {"text": "acme secret"}, None, 60,
                                   prefix="t:acme:")
        for _ in range(3):
            dispatch_mcp_tool("headroom_retrieve", {"ref": stored["ref"]}, None, 60,
                              prefix="t:acme:")

        globex = dispatch_mcp_tool("headroom_stats", {}, None, 60, prefix="t:globex:")
        assert globex["hits"] == 0 and globex["misses"] == 0, (
            "globex reads acme's retrieval activity — an activity-timing side channel"
        )
        assert dispatch_mcp_tool("headroom_stats", {}, None, 60, prefix="t:acme:")["hits"] == 3


class TestCcrRefusesWhileStoreIsEphemeral:
    """G28 must refuse to run while its store cannot outlive the process.

    The config toggle is tenant-reachable and the portal copy used to recommend turning
    it on, so `enabled: true` was a supported thing to ask for — while the store is a
    module-level dict that dies with the instance. Honouring that request replaces content
    the model needs with a reference that resolves only on the instance that made it, and
    fails on a billed HTTP 200 with no metric. Refusing loudly is the honest state until
    demand-driven-features.md #28 lands.
    """

    async def test_enabled_is_not_honoured(self, make_ctx):
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "min_tokens": 300}
        out = await G28CCR().process_request(ctx)
        assert not out.messages[0]["content"].startswith("[CCR:"), (
            "content was replaced with a reference the store cannot durably resolve"
        )
        assert out.messages[0]["content"] == _BIG

    async def test_mcp_tools_are_not_advertised(self, make_ctx):
        """If the tools are never offered, nothing can ask the proxy to execute them —
        this is also what keeps the G15 dispatch surface dormant."""
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "expose_mcp_tools": True}
        out = await G28CCR().process_request(ctx)
        names = {t.get("function", {}).get("name") for t in (out.params.get("tools") or [])}
        assert not (names & {"headroom_compress", "headroom_retrieve", "headroom_stats"})
        assert out.ccr_tools_injected is False

    async def test_refusal_is_logged_at_error_once(self, make_ctx, caplog):
        import logging
        import middleware.g28_ccr as g28
        from middleware.g28_ccr import G28CCR
        g28._UNAVAILABLE_LOGGED = False
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True}
        with caplog.at_level(logging.ERROR):
            await G28CCR().process_request(ctx)
            await G28CCR().process_request(ctx)
        hits = [r for r in caplog.records
                if r.levelno >= logging.ERROR and "REFUSED" in r.getMessage()]
        assert len(hits) == 1, "the refusal must be visible, but must not spam every request"

    async def test_opting_in_restores_the_feature(self, make_ctx, ccr_available):
        """The guard is a gate, not a removal — #28 flips one flag."""
        from middleware.g28_ccr import G28CCR
        ctx = make_ctx([{"role": "user", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "min_tokens": 300}
        out = await G28CCR().process_request(ctx)
        assert out.messages[0]["content"].startswith("[CCR:")
