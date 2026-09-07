"""The prefix-cache floor is arithmetic, not a threshold — these pin the arithmetic.

Backlog #41. A provider that will not cache a span declines in SILENCE: no read, no
write, no error. So a compression that crosses the minimum can send far fewer tokens and
still cost more. The obvious fix — "stop compressing at the floor" — is itself a defect,
because whether holding tokens back is cheaper depends on the provider's cache rates and
on how often the prefix repeats, and on the SAME workload those point in opposite
directions.

The DS8 measurement (2026-09-04, a 2,044-token shared prefix over 30 requests) is used
here as the fixture, in units of input tokens billed at the full rate:

    read x0.10 / write x1.25   compress 22,080   preserve  8,483   floor-to-F  4,250
    read x0.50 / write x1.00   compress 19,140   preserve 31,682   floor-to-F 15,872
                                                 ^ preserving costs 1.66x MORE

Two-sided by construction (Gate 8.3): the same tables show that at N=1 every floor loses,
because the first request pays a cache write on a bigger span.
"""
import pytest

from middleware import cache_floor
from middleware.cache_floor import compress_is_cheaper

# DS8, measured 2026-09-04.
P = 2044      # cacheable span, uncompressed
F = 1024      # provider minimum
P_BREAKPOINT = 736   # what the all-on stack compressed it to on the breakpoint provider
P_WHOLE = 638        # ... and on the whole-prompt provider


def _arm_a(span_after, n):
    return n * span_after


def _arm_b(span, n, r, w):
    return w * span + (n - 1) * r * span


def _arm_c(floor, n, r, w):
    return w * floor + (n - 1) * r * floor


class TestTheDS8Tables:
    """The published figures must be reproducible from the formulas, or the formulas are
    not what produced them."""

    def test_breakpoint_provider_at_thirty_reuses(self):
        assert round(_arm_a(P_BREAKPOINT, 30)) == 22080
        assert round(_arm_b(P, 30, 0.1, 1.25)) == 8483
        assert round(_arm_c(F, 30, 0.1, 1.25)) == 4250

    def test_whole_prompt_provider_at_thirty_reuses(self):
        assert round(_arm_a(P_WHOLE, 30)) == 19140
        assert round(_arm_b(P, 30, 0.5, 1.0)) == 31682
        assert round(_arm_c(F, 30, 0.5, 1.0)) == 15872

    def test_preserving_the_whole_span_costs_more_on_the_whole_prompt_provider(self):
        """The finding that makes a bare floor test a DEFECT and not just a blunt one:
        a discounted read of the uncompressed span (0.5 x 2044 = 1022) already costs more
        per request than the compressed span (638), so preserving loses at every N."""
        for n in (1, 2, 5, 30, 1000):
            assert _arm_b(P, n, 0.5, 1.0) > _arm_a(P_WHOLE, n)

    def test_flooring_beats_preserving_everywhere(self):
        """C caches a strictly smaller block at the same multipliers, so F < P is the
        whole proof. Worth pinning: it is why the design aims at the floor rather than
        abandoning the compression."""
        for r, w in ((0.1, 1.25), (0.5, 1.0), (0.25, 1.0)):
            for n in (1, 2, 5, 30):
                assert _arm_c(F, n, r, w) < _arm_b(P, n, r, w)


class TestTheDecision:
    """`compress_is_cheaper` is the one place the arithmetic is implemented; every
    consumer asks it rather than re-deriving a rule of thumb."""

    def test_single_shot_always_compresses(self):
        """At N=1 the first request pays a cache WRITE on a bigger span, so every floor
        loses — 1.7x on the breakpoint provider, 1.6x on the whole-prompt one. A guard
        that fires here takes money off the customer for a discount that never arrives."""
        assert compress_is_cheaper(P, P_BREAKPOINT, F, 0.1, 1.25, 1) is True
        assert compress_is_cheaper(P, P_WHOLE, F, 0.5, 1.0, 1) is True

    def test_break_even_on_the_breakpoint_provider_is_two(self):
        assert compress_is_cheaper(P, P_BREAKPOINT, F, 0.1, 1.25, 1) is True
        assert compress_is_cheaper(P, P_BREAKPOINT, F, 0.1, 1.25, 2) is False

    def test_break_even_on_the_whole_prompt_provider_is_five(self):
        """Not 2 — the smaller discount needs more reuse to repay the write."""
        assert compress_is_cheaper(P, P_WHOLE, F, 0.5, 1.0, 4) is True
        assert compress_is_cheaper(P, P_WHOLE, F, 0.5, 1.0, 5) is False

    def test_a_provider_with_no_discount_never_holds_tokens_back(self):
        """The generic adapter ships 1.0/1.0. There is no discount to protect, so holding
        tokens back is a permanent loss at every N."""
        for n in (1, 5, 30, 1000):
            assert compress_is_cheaper(P, P_BREAKPOINT, F, 1.0, 1.0, n) is True

    def test_no_declared_floor_is_inert(self):
        assert compress_is_cheaper(P, P_BREAKPOINT, 0, 0.1, 1.25, 30) is True

    def test_a_span_that_was_never_cacheable_is_inert(self):
        """Below the floor to begin with: there is no discount to forfeit, so guarding it
        would cost real savings to protect a benefit that never existed."""
        assert compress_is_cheaper(500, 300, F, 0.1, 1.25, 30) is True

    def test_a_compression_that_stays_above_the_floor_is_always_taken(self):
        """Fewer tokens AND still cached — the best of both, and the case a
        whole-prompt-shaped guard would have wrongly blocked when the SUFFIX was what
        shrank."""
        assert compress_is_cheaper(2044, 1500, F, 0.1, 1.25, 30) is True


class TestReservation:
    """The reservation is fail-SAFE in every direction: anything unknown leaves it inert,
    which is byte-identical to the behaviour before this existed."""

    class _Adapter:
        name = "testprov"

        def __init__(self, floor=1024, roles=None):
            self._floor = floor
            self._roles = roles

        def min_cacheable_prompt_tokens(self, config, model):
            return self._floor

        def cacheable_span_messages(self, messages, params, cfg):
            if self._roles is None:
                return list(messages)
            return [m for m in messages if m.get("role") in self._roles]

        def cache_read_cost_multiplier(self, config):
            return 0.1

        def cache_write_cost_multiplier(self, config):
            return 1.25

    def _ctx(self, make_ctx, minimal_config, enabled=True, roles=None, floor=1024):
        minimal_config["groups"]["G1_compression"] = {
            "enabled": True, "preserve_cacheable_prefix": enabled}
        ctx = make_ctx(
            [{"role": "system", "content": "SYS " * 3000},
             {"role": "user", "content": "USR " * 3000}],
            model="gpt-4o", config=minimal_config)
        ctx.provider_adapter = self._Adapter(floor, roles)
        return ctx

    async def _reserve(self, ctx, monkeypatch, reuse=30):
        async def _n(*a, **k):
            return max(0, reuse - 1)
        monkeypatch.setattr(cache_floor, "_observed_reuse", _n)
        await cache_floor.reserve(ctx)

    @pytest.mark.asyncio
    async def test_knob_off_is_inert(self, make_ctx, minimal_config, monkeypatch):
        ctx = self._ctx(make_ctx, minimal_config, enabled=False)
        await self._reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False

    @pytest.mark.asyncio
    async def test_no_adapter_is_inert(self, make_ctx, minimal_config, monkeypatch):
        ctx = self._ctx(make_ctx, minimal_config)
        ctx.provider_adapter = None
        await self._reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False

    @pytest.mark.asyncio
    async def test_span_scoping_follows_the_adapter(self, make_ctx, minimal_config, monkeypatch):
        """A provider that caches only the block up to its marker must not have its floor
        compared against the whole prompt: that is the wrong quantity, and it makes the
        guard fire on the wrong requests and stand down on the right ones."""
        ctx = self._ctx(make_ctx, minimal_config, roles={"system"})
        await self._reserve(ctx, monkeypatch)
        fl = cache_floor.get(ctx)
        assert fl.active is True
        assert fl.span_is_whole_prompt is False
        assert fl.covers(ctx.messages[0]) is True     # the reserved system message
        assert fl.covers(ctx.messages[1]) is False    # user — outside the marked block
        whole = cache_floor.span_tokens(
            ctx, ctx.messages) if fl.span_is_whole_prompt else None
        assert whole is None  # scoping, not a whole-prompt count

    @pytest.mark.asyncio
    async def test_whole_prompt_adapter_covers_every_role(
            self, make_ctx, minimal_config, monkeypatch):
        ctx = self._ctx(make_ctx, minimal_config, roles=None)
        await self._reserve(ctx, monkeypatch)
        fl = cache_floor.get(ctx)
        assert fl.span_is_whole_prompt is True
        assert fl.covers(ctx.messages[1]) is True     # user, and it was reserved

    @pytest.mark.asyncio
    async def test_a_message_injected_after_the_reservation_is_not_in_the_span(
            self, make_ctx, minimal_config, monkeypatch):
        """G07 and G10 inject retrieved documents and memories as `system`/`tool`
        messages AFTER Stage 3 has begun — after this reservation was taken. Matching by
        ROLE alone would pull that per-query content into the span, inflating it and
        making the shrinking groups refuse to prune a retrieved chunk in order to defend
        a prefix that changes on the very next request anyway."""
        ctx = self._ctx(make_ctx, minimal_config, roles={"system"})
        await self._reserve(ctx, monkeypatch)
        fl = cache_floor.get(ctx)
        reserved_span = cache_floor.span_tokens(ctx, ctx.messages)

        injected = {"role": "system", "content": "RETRIEVED " * 2000}
        assert fl.covers(injected) is False, "same role, but never reserved"
        ctx.messages = [ctx.messages[0], injected, ctx.messages[1]]
        assert cache_floor.span_tokens(ctx, ctx.messages) == reserved_span, (
            "the span must not grow by content the reservation never saw"
        )

    @pytest.mark.asyncio
    async def test_a_rewritten_in_span_message_is_still_counted(
            self, make_ctx, minimal_config, monkeypatch):
        """The snapshot is by content, so a compressed message no longer matches it.
        Membership is therefore decided on the BEFORE list and tokens counted on the
        AFTER list at the same index — otherwise the guard would read the span as having
        fallen to zero and hold back tokens that were never at risk."""
        ctx = self._ctx(make_ctx, minimal_config, roles={"system"})
        await self._reserve(ctx, monkeypatch)
        shrunk = [{**ctx.messages[0], "content": "SYS " * 500}, ctx.messages[1]]

        paired = cache_floor.span_tokens_paired(ctx, ctx.messages, shrunk)
        assert paired > 0
        assert paired < cache_floor.span_tokens(ctx, ctx.messages)
        # The naive re-test drops the rewrite entirely — the bug this exists to avoid.
        assert cache_floor.span_tokens(ctx, shrunk) == 0

    @pytest.mark.asyncio
    async def test_reuse_uses_the_count_before_this_request(
            self, make_ctx, minimal_config, monkeypatch):
        """The first sighting of a prefix must read as no reuse. Counting this request
        itself would make every prefix look repeated and hold tokens back on single-shot
        traffic, which is exactly where a floor loses."""
        ctx = self._ctx(make_ctx, minimal_config)
        await self._reserve(ctx, monkeypatch, reuse=1)
        assert cache_floor.get(ctx).reuse == 1

    @pytest.mark.asyncio
    async def test_unknown_reuse_is_inert(self, make_ctx, minimal_config, monkeypatch):
        async def _none(*a, **k):
            return None
        ctx = self._ctx(make_ctx, minimal_config)
        monkeypatch.setattr(cache_floor, "_observed_reuse", _none)
        await cache_floor.reserve(ctx)
        assert cache_floor.get(ctx).active is False

    @pytest.mark.asyncio
    async def test_the_reuse_key_is_tenant_scoped(self, make_ctx, minimal_config, monkeypatch):
        """Cross-instance AND cross-tenant. A bare key would let one tenant's traffic
        raise another tenant's reuse count and hold ITS tokens back."""
        seen = {}

        class _Redis:
            async def set(self, key, value, nx=None, ex=None):
                seen["set"] = (key, value, nx, ex)
                return True

            async def incr(self, key):
                seen["key"] = key
                return 5

        import cache.redis_pool as pool
        monkeypatch.setattr(pool, "get_redis", lambda: _Redis())
        ctx = self._ctx(make_ctx, minimal_config)
        ctx.redis_prefix = "tenant:ACME-STG-01:"
        await cache_floor.reserve(ctx)
        assert seen["key"].startswith("tenant:ACME-STG-01:cachefloor:")
        # INCR returned 5, so 4 sightings preceded this one; N counts this request too.
        assert cache_floor.get(ctx).reuse == 5

    @pytest.mark.asyncio
    async def test_the_reuse_window_is_fixed_not_sliding(
            self, make_ctx, minimal_config, monkeypatch):
        """`INCR` + `EXPIRE` on every sighting renews the TTL, so the key never expires
        while traffic continues and N becomes a LIFETIME count. N is exactly what decides
        whether tokens are held back, so a prefix seen once a minute for a day would be
        credited with a reuse the provider's cache lost hours ago. `SET key 0 NX EX ttl`
        starts the window once and lets it end."""
        calls = []

        class _Redis:
            async def set(self, key, value, nx=None, ex=None):
                calls.append(("set", key, value, nx, ex))
                return True

            async def incr(self, key):
                calls.append(("incr", key))
                return 3

            async def expire(self, key, ttl):  # must NOT be called
                calls.append(("expire", key, ttl))
                return True

        import cache.redis_pool as pool
        monkeypatch.setattr(pool, "get_redis", lambda: _Redis())
        ctx = self._ctx(make_ctx, minimal_config)
        await cache_floor.reserve(ctx)
        kinds = [c[0] for c in calls]
        assert kinds == ["set", "incr"], f"no sliding EXPIRE: {calls}"
        assert calls[0][2] == 0 and calls[0][3] is True and calls[0][4] == 300, (
            "SET ... 0 NX EX <window> — only the first sighting starts the clock"
        )

    @pytest.mark.asyncio
    async def test_a_bypassed_request_makes_no_reservation(
            self, make_ctx, minimal_config, monkeypatch):
        ctx = self._ctx(make_ctx, minimal_config)
        ctx.bypassed = True
        await self._reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False


class TestTargetRate:
    """`ratio` is a KEEP-fraction on the compression sidecar, so aiming a span just above
    the floor is one more call rather than a search. That is what makes "compress down to
    the floor" affordable at all."""

    def _floor(self, span=2044, floor=1024, margin=0.05):
        return cache_floor.CacheFloor(
            active=True, floor=floor, span_tokens=span, read_mult=0.1,
            write_mult=1.25, reuse=30, margin=margin, span_is_whole_prompt=True)

    def test_rate_lands_just_above_the_floor(self):
        class _Ctx:
            pass
        ctx = _Ctx()
        ctx.cache_floor = self._floor()
        rate = cache_floor.target_rate(ctx, 2044)
        assert rate == pytest.approx(1024 * 1.05 / 2044, rel=1e-3)
        assert 2044 * rate > 1024, "the margin must land ABOVE the floor, not on it"

    def test_no_rate_when_the_span_is_already_at_the_floor(self):
        class _Ctx:
            pass
        ctx = _Ctx()
        ctx.cache_floor = self._floor(span=1000)
        assert cache_floor.target_rate(ctx, 1000) is None

    def test_inert_without_a_reservation(self):
        class _Ctx:
            pass
        assert cache_floor.target_rate(_Ctx(), 2044) is None


class TestConsumers:
    """G01 is not the only group that shrinks the span, and on the shipped defaults it is
    not even the one that shrinks a single-turn prefix (it compresses `assistant` only).
    G19 rewrites every role including `system`, and G08 trims tool descriptions — both of
    which sit inside the cacheable block, and both of which run AFTER G01. A guard that
    lived only in G01 would be undone here, silently.
    """

    def _floor(self, floor, roles=None):
        return cache_floor.CacheFloor(
            active=True, floor=floor, span_tokens=floor + 10, read_mult=0.1,
            write_mult=1.25, reuse=30, span_roles=frozenset(roles or []),
            span_is_whole_prompt=roles is None)

    def _g19_ctx(self, make_ctx, minimal_config, monkeypatch):
        """G19 with its content detector and compressor stubbed.

        The subject here is the floor guard, not G19's log/JSON heuristics — stubbing
        them keeps this test measuring the thing it claims to measure, and keeps it from
        going red the next time those heuristics are tuned.
        """
        import middleware.g19_headroom as g19
        minimal_config["groups"]["G19_headroom"] = {
            "enabled": True, "min_length_to_compress": 10,
            "compression_strategies": {"logs": {}}}
        monkeypatch.setattr(g19, "_detect_content_type", lambda *a, **k: "logs")
        monkeypatch.setattr(g19, "_compress", lambda text, *a, **k: text[: len(text) // 4])
        return make_ctx([{"role": "system", "content": "ERROR db timeout at pool-1 " * 400}],
                        model="gpt-4o", config=minimal_config)

    @pytest.mark.asyncio
    async def test_g19_holds_the_span_rather_than_pruning_it_under_the_floor(
            self, make_ctx, minimal_config, monkeypatch):
        from middleware.g19_headroom import G19Headroom
        from savings.calculator import count_messages_tokens

        ctx = self._g19_ctx(make_ctx, minimal_config, monkeypatch)
        before = ctx.messages[0]["content"]
        ctx.cache_floor = self._floor(count_messages_tokens(ctx.messages, ctx.model) - 1)
        out = await G19Headroom().process_request(ctx)
        assert out.messages[0]["content"] == before, (
            "G19 runs after G01 — if it can still prune the span under the floor, the "
            "guard is decorative"
        )
        assert out.cache_floor_action == "preserved"

    @pytest.mark.asyncio
    async def test_g19_is_unaffected_without_a_reservation(
            self, make_ctx, minimal_config, monkeypatch):
        from middleware.g19_headroom import G19Headroom

        ctx = self._g19_ctx(make_ctx, minimal_config, monkeypatch)
        before = ctx.messages[0]["content"]
        out = await G19Headroom().process_request(ctx)
        assert out.messages[0]["content"] != before, "default must be byte-identical"

    @pytest.mark.asyncio
    async def test_g08_keeps_the_tool_list_whole_rather_than_trimming_under_the_floor(
            self, make_ctx, minimal_config):
        """Tool definitions sit FIRST inside a marker-based provider's cached block, so a
        description trim is one of the ways the span slips under the minimum unseen."""
        from middleware.g08_tool_loading import G08ToolLoading
        from savings.calculator import count_tools_tokens

        tools = [{"type": "function", "function": {
            "name": f"tool_{i}",
            "description": ("This particular function is in fact used in order to "
                            "retrieve the relevant data that is needed. " * 6),
            "parameters": {"type": "object", "properties": {}}}} for i in range(6)]
        minimal_config["groups"]["G8_tools"] = {
            "enabled": True, "compress_descriptions": True, "registry_path": ""}
        ctx = make_ctx([{"role": "user", "content": "fetch the data"}],
                       model="gpt-4o", config=minimal_config)
        ctx.params["tools"] = tools
        span = count_tools_tokens(tools, ctx.model)
        ctx.cache_floor = self._floor(span - 1)
        before = [t["function"]["description"] for t in tools]
        out = await G08ToolLoading().process_request(ctx)
        after = [t["function"]["description"] for t in out.params["tools"]]
        assert after == before, "the tool list must be left whole"
        assert out.cache_floor_action == "preserved"


class TestConfigExposure:
    """Gate 7c. These knobs trade tokens for cost using published provider rates and an
    observed reuse rate — a tenant can see neither, and a wrong value costs money rather
    than degrading quality. Operator-only, and the portal catalog is a strict whitelist,
    so absence from it is the enforcement."""

    def test_the_knobs_are_not_tenant_facing(self):
        # `api/portal.py` is commercial and gitignored: the OSS tree and public CI ship
        # only `src/proxy/api/__init__.py`, so an unguarded import here turns the
        # clean-checkout gate red for a test that has nothing to assert there.
        try:
            from api.portal import _GROUP_CATALOG
        except Exception:
            pytest.skip("commercial portal not present in this tree")
        knobs = {k.get("key")
                 for group in _GROUP_CATALOG for k in (group.get("knobs") or [])}
        for operator_only in ("preserve_cacheable_prefix", "cacheable_prefix_margin",
                              "assumed_prefix_reuse", "prefix_reuse_window_seconds"):
            assert operator_only not in knobs, (
                f"{operator_only} is an operator knob — exposing it would let a tenant "
                "raise their own bill with no way to see the rates it trades against"
            )

    def test_the_template_ships_the_guard_off(self):
        import yaml
        from pathlib import Path
        tmpl = Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
        cfg = yaml.safe_load(tmpl.read_text(encoding="utf-8"))
        g01 = cfg["groups"]["G1_compression"]
        assert g01["preserve_cacheable_prefix"] is False
        assert g01["assumed_prefix_reuse"] == 1, (
            "assuming reuse a deployment does not have costs money; evidence, not optimism"
        )


@pytest.mark.asyncio
class TestTheSnapshotSurvivesTheHandoff:
    """The budget is SHARED, so it has to survive being handed from one group to the next.

    `covers()` identifies a span member by CONTENT. The moment G01 assigns its rewritten
    list to `ctx.messages` the snapshot describes messages that no longer exist, and the
    next group's `span_tokens` reads the span as EMPTY — which the arithmetic treats as
    "never cacheable, nothing to protect" and permits any shrink. Fail-OPEN, landing
    exactly where the guard has just succeeded: G01 puts the span ON the floor and reports
    `floored`, G19 prunes the same message straight through it while measuring zero, and
    the request forfeits the discount while its own observable claims otherwise.
    """

    _SENTENCE = "The deployment pipeline review records the same finding again. "

    def _ctx(self, make_ctx, minimal_config, monkeypatch, first_pass_ratio=0.3):
        import middleware.g01_compression as g01
        import middleware.g19_headroom as g19

        minimal_config["groups"]["G1_compression"] = {
            "enabled": True, "min_tokens_to_compress": 10, "min_chars_to_compress": 50,
            "compress_system_prompt": True, "compress_user_messages": True,
            "kompress_enabled": False, "layered_composition_enabled": False,
            "selective_context_enabled": False, "deterministic_fallback": False,
            "preserve_cacheable_prefix": True,
            "compression_ratio_target": first_pass_ratio,
        }
        minimal_config["groups"]["G19_headroom"] = {
            "enabled": True, "min_length_to_compress": 10,
            "compression_strategies": {"logs": {}}}
        # G19's own heuristics are not the subject; stubbing them keeps this measuring
        # the handoff and keeps it green when those heuristics are next tuned.
        monkeypatch.setattr(g19, "_detect_content_type", lambda *a, **k: "logs")
        monkeypatch.setattr(g19, "_compress", lambda text, *a, **k: text[: len(text) // 4])
        return make_ctx(
            [{"role": "system", "content": self._SENTENCE * 400},
             {"role": "user", "content": self._SENTENCE * 200}],
            model="gpt-4o", config=minimal_config)

    def _stub_sidecar(self, monkeypatch, keep=None):
        import middleware.g01_compression as g01

        async def _call(url, text, ratio, force_reserve_digit=True):
            words = text.split()
            frac = keep if keep is not None else ratio
            return " ".join(words[: max(1, int(len(words) * frac))])

        monkeypatch.setattr(g01, "_call_llmlingua", _call)

    async def _reserve(self, ctx, monkeypatch, floor_fraction=0.5, reuse=30):
        from savings.calculator import count_messages_tokens

        class _Adapter:
            name = "testprov"

            def __init__(self, floor):
                self._floor = floor

            def min_cacheable_prompt_tokens(self, config, model):
                return self._floor

            def cacheable_span_messages(self, messages, params, config):
                return [m for m in messages if m.get("role") == "system"]

            def cache_read_cost_multiplier(self, config):
                return 0.1

            def cache_write_cost_multiplier(self, config):
                return 1.25

        span = count_messages_tokens(
            [m for m in ctx.messages if m["role"] == "system"], ctx.model)
        floor = int(span * floor_fraction)
        ctx.provider_adapter = _Adapter(floor)

        async def _n(*a, **k):
            return max(0, reuse - 1)

        monkeypatch.setattr(cache_floor, "_observed_reuse", _n)
        await cache_floor.reserve(ctx)
        return floor, span

    async def test_g19_still_sees_the_span_g01_floored(
            self, make_ctx, minimal_config, monkeypatch):
        from middleware.g01_compression import G01Compression
        from middleware.g19_headroom import G19Headroom

        ctx = self._ctx(make_ctx, minimal_config, monkeypatch)
        self._stub_sidecar(monkeypatch)
        floor, _ = await self._reserve(ctx, monkeypatch)

        await G01Compression().process_request(ctx)
        assert ctx.cache_floor_action == "floored", "precondition: arm C fired"
        floored_content = ctx.messages[0]["content"]

        # The handoff itself: the NEXT group must measure the live span, not zero.
        measured = cache_floor.span_tokens(ctx, ctx.messages)
        assert measured >= floor, (
            f"G19 would measure the span as {measured} against a floor of {floor}; "
            "below the floor the arithmetic concludes there is nothing to protect and "
            "permits any shrink"
        )

        out = await G19Headroom().process_request(ctx)
        assert out.messages[0]["content"] == floored_content, (
            "G19 pruned the span G01 had just landed ON the floor — the discount is "
            "forfeited on a request whose own observable says it was saved"
        )
        assert cache_floor.span_tokens(out, out.messages) >= floor

    async def test_arm_b_leaves_the_snapshot_matching(
            self, make_ctx, minimal_config, monkeypatch):
        """Arm B restores the originals, so the snapshot must still match after it — a
        re-snapshot that dropped them would break the guard in the other direction."""
        from middleware.g01_compression import G01Compression

        ctx = self._ctx(make_ctx, minimal_config, monkeypatch)
        self._stub_sidecar(monkeypatch, keep=0.2)   # ignores the target rate → undershoot
        floor, span = await self._reserve(ctx, monkeypatch)

        await G01Compression().process_request(ctx)
        assert ctx.cache_floor_action == "preserved", "precondition: arm B fired"
        assert cache_floor.get(ctx).covers(ctx.messages[0]) is True
        assert cache_floor.span_tokens(ctx, ctx.messages) == span

    async def test_the_handoff_is_a_no_op_without_a_reservation(
            self, make_ctx, minimal_config, monkeypatch):
        """`resnapshot` must be inert when the guard is off — it runs on every compressed
        request, and the default path has to stay byte-identical."""
        ctx = self._ctx(make_ctx, minimal_config, monkeypatch)
        ctx.config["groups"]["G1_compression"]["preserve_cacheable_prefix"] = False
        self._stub_sidecar(monkeypatch)
        await self._reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False

        from middleware.g01_compression import G01Compression
        out = await G01Compression().process_request(ctx)
        assert cache_floor.get(out).active is False
        assert out.messages[0]["content"] != self._SENTENCE * 400


@pytest.mark.asyncio
class TestStaleSnapshotBackstop:
    """The structural half of the same defect: `resnapshot` at every rewrite site is the
    fix, this is the backstop for the site nobody remembered to add.

    A reservation is only ever taken when the span was already at or above the floor, so a
    live span that measures ZERO cannot be the truth — every message the snapshot named
    has been rewritten by a group that did not re-take it. The arithmetic would read that
    as "never cacheable, nothing to protect" and permit any shrink: fail-OPEN, silently.
    Refusing instead can cost tokens; it can never forfeit a discount.
    """

    def _stale_ctx(self, make_ctx, minimal_config, monkeypatch):
        import middleware.g19_headroom as g19
        from savings.calculator import count_messages_tokens

        minimal_config["groups"]["G19_headroom"] = {
            "enabled": True, "min_length_to_compress": 10,
            "compression_strategies": {"logs": {}}}
        monkeypatch.setattr(g19, "_detect_content_type", lambda *a, **k: "logs")
        monkeypatch.setattr(g19, "_compress", lambda text, *a, **k: text[: len(text) // 4])
        ctx = make_ctx([{"role": "system", "content": "ERROR db timeout at pool-1 " * 400}],
                       model="gpt-4o", config=minimal_config)
        span = count_messages_tokens(ctx.messages, ctx.model)
        ctx.cache_floor = cache_floor.CacheFloor(
            active=True, floor=span // 2, span_tokens=span, read_mult=0.1,
            write_mult=1.25, reuse=30, span_roles=frozenset({"system"}),
            span_fingerprints=frozenset(
                cache_floor.message_fingerprint(m) for m in ctx.messages),
            span_is_whole_prompt=False)
        return ctx

    async def test_a_stale_snapshot_refuses_the_shrink_and_says_so(
            self, make_ctx, minimal_config, monkeypatch, caplog):
        import logging
        from middleware.g19_headroom import G19Headroom

        ctx = self._stale_ctx(make_ctx, minimal_config, monkeypatch)
        # A forgetful group: rewrites an in-span message and does NOT call resnapshot.
        ctx.messages = [{**ctx.messages[0], "content": "ERROR db timeout at pool-1 " * 390}]
        assert cache_floor.span_tokens(ctx, ctx.messages) == 0, (
            "precondition: the snapshot is stranded, which is what makes the span "
            "unmeasurable"
        )
        before = ctx.messages[0]["content"]

        with caplog.at_level(logging.WARNING, logger="middleware.cache_floor"):
            out = await G19Headroom().process_request(ctx)

        assert out.messages[0]["content"] == before, (
            "the span may already be sitting on the floor; shrinking it blind is the "
            "fail-OPEN this backstop exists to stop"
        )
        assert cache_floor.snapshot_is_stale(out) is True
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("resnapshot" in w and "G19" in w for w in warnings), warnings

    async def test_the_warning_is_once_per_request(
            self, make_ctx, minimal_config, monkeypatch, caplog):
        """One line names the missing call. A line per group per request would bury it."""
        import logging

        ctx = self._stale_ctx(make_ctx, minimal_config, monkeypatch)
        ctx.messages = [{**ctx.messages[0], "content": "x" * 4000}]
        with caplog.at_level(logging.WARNING, logger="middleware.cache_floor"):
            for group in ("G01", "G08", "G19"):
                assert cache_floor.allows_shrink(ctx, 0, 0, group) is False
        stale = [r for r in caplog.records if "resnapshot" in r.getMessage()]
        assert len(stale) == 1, [r.getMessage() for r in stale]

    async def test_a_fresh_snapshot_is_unaffected(
            self, make_ctx, minimal_config, monkeypatch, caplog):
        """The backstop must not fire on a healthy request — it would hold tokens back
        for no reason on every compressible prompt."""
        import logging
        from middleware.g19_headroom import G19Headroom

        ctx = self._stale_ctx(make_ctx, minimal_config, monkeypatch)
        before = ctx.messages[0]["content"]
        assert cache_floor.span_tokens(ctx, ctx.messages) > 0

        with caplog.at_level(logging.WARNING, logger="middleware.cache_floor"):
            out = await G19Headroom().process_request(ctx)

        # Refused on the ARITHMETIC (pruning to a quarter crosses the floor), not on
        # staleness — the ordinary, correct outcome.
        assert out.messages[0]["content"] == before
        assert cache_floor.snapshot_is_stale(out) is False
        assert not [r for r in caplog.records if "resnapshot" in r.getMessage()]

    async def test_the_backstop_is_inert_without_a_reservation(self, make_ctx, minimal_config):
        ctx = make_ctx([{"role": "system", "content": "hi"}], config=minimal_config)
        assert cache_floor.allows_shrink(ctx, 0, 0, "G19") is True
        assert cache_floor.snapshot_is_stale(ctx) is False


class TestEveryRewriterRefreshesTheSnapshot:
    """Source-inspection guard: a future group that rewrites `ctx.messages` between G01
    and G19 must not be able to strand the reservation by forgetting to re-take it.

    The failure it prevents is silent and fails OPEN — a stranded snapshot reads as an
    EMPTY span, which the arithmetic interprets as "never cacheable, nothing to protect".
    No test of the new group would notice; the only symptom is a prefix-cache discount
    that quietly stops arriving. So the invariant is enforced HERE, over the source, for
    every group in the window rather than for the ones we happened to think of.

    The group ordering is read out of `pipeline.py`'s Stage 3 list, not hardcoded — if the
    pipeline is reordered, or a group is inserted into the window, this test follows it.
    """

    # Assignment sites that do NOT need a re-snapshot, each with the evidence that says
    # so. Keyed by module, valued by the exact assignment text: if that text changes, the
    # exemption stops matching and the site must be re-examined rather than inherited.
    _INSERT_ONLY = {
        "g07_retrieval": (
            "ctx.messages = _inject_context(ctx.messages, context_text)",
            "`_inject_context` returns `injected + messages` — a pure prepend that leaves "
            "every existing message untouched, so the snapshot stays valid. An "
            "index-paired re-snapshot would in fact be WRONG here: the indices shift, and "
            "the retrieved document must NOT enter the span (it is per-query content, and "
            "counting it is the very over-inclusion the fingerprint snapshot fixed).",
        ),
    }
    # Generous enough for a real explanatory comment between the assignment and the call,
    # tight enough that the call has to belong to that assignment.
    _WINDOW = 15

    def _src(self, name):
        from pathlib import Path
        return (Path(__file__).resolve().parents[3] / "src" / "proxy" / "middleware"
                / f"{name}.py").read_text(encoding="utf-8")

    def _stage3_window(self):
        """The group ids that run from G01 to G19 inclusive, read from the pipeline."""
        import re
        src = self._src("pipeline")
        stage3 = src.split("# Stage 3 — Into the LLM", 1)
        assert len(stage3) == 2, "pipeline.py no longer marks Stage 3 — update this guard"
        body = stage3[1].split("# Stage 3b", 1)[0]
        order = re.findall(r'self\.g\d+,\s*"(G\d+)"', body)
        assert order[0] == "G01", order
        assert "G19" in order, order
        return order[: order.index("G19") + 1]

    def _module_for(self, gid):
        import re
        src = self._src("pipeline")
        mods = re.findall(r"from middleware\.(g\d+_\w+) import", src)
        want = gid.lower()
        hits = [m for m in mods if m.split("_", 1)[0] == want]
        assert len(hits) == 1, f"{gid}: expected one module, got {hits}"
        return hits[0]

    def test_the_window_is_read_from_the_pipeline(self):
        window = self._stage3_window()
        assert window[0] == "G01" and window[-1] == "G19"
        assert len(window) >= 3, "a window of two would make this guard vacuous"

    def test_every_message_rewrite_in_the_window_re_takes_the_snapshot(self):
        offenders = []
        for gid in self._stage3_window():
            module = self._module_for(gid)
            lines = self._src(module).splitlines()
            for i, line in enumerate(lines):
                if "ctx.messages =" not in line or line.lstrip().startswith("#"):
                    continue
                stripped = line.strip()
                exempt = self._INSERT_ONLY.get(module)
                if exempt and stripped == exempt[0]:
                    continue
                window = "\n".join(lines[i + 1: i + 1 + self._WINDOW])
                if "cache_floor.resnapshot(" not in window:
                    offenders.append(f"{module}.py:{i + 1}  {stripped}")
        assert not offenders, (
            "these assignments strand the cache-floor reservation — the span reads as "
            "EMPTY for every later group, which the arithmetic treats as 'nothing to "
            "protect' and permits any shrink (fail-OPEN, and silent):\n  "
            + "\n  ".join(offenders)
            + "\n\nAdd `cache_floor.resnapshot(ctx, <the list you started from>, "
              "ctx.messages)` after the assignment. If the group only INSERTS or REMOVES "
              "messages, add it to _INSERT_ONLY here with the evidence instead — an "
              "index-paired re-snapshot would be wrong in that case."
        )

    def test_the_insert_only_exemptions_still_describe_the_code(self):
        """An exemption inherited after the code changed is worse than none."""
        for module, (assignment, _why) in self._INSERT_ONLY.items():
            assert assignment in self._src(module), (
                f"{module}.py no longer contains `{assignment}` — its cache-floor "
                "exemption was written for code that has changed; re-check whether it "
                "still only inserts, or drop the exemption and call resnapshot"
            )
