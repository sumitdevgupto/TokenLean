"""G01 must not compress a prompt below the provider's minimum cacheable size.

Backlog #41, measured on DS8 2026-09-04 (`run-20260904-093125`). Both OpenAI and
Anthropic decline to cache a prefix under ~1024 tokens, and they decline SILENTLY — no
cache read, no cache write, no error, nothing in the response to notice. The all-on stack
compressed a 2,044-token shared prefix to ~736 tokens, so caching stopped entirely while
G21 carried on injecting `cache_control` markers that could never pay out:

    only-G21   54,150 tokens sent   51,707 reads + 1,783 writes   input $0.0242
    all-on     19,878 tokens sent        0 reads +     0 writes   input $0.0596

63% fewer tokens, 2.5x the input cost.

The guard ships DEFAULT OFF and these tests pin that, because whether the trade is bad is
workload-shaped: it only bites when a prefix actually repeats, and on a workload with no
repetition compression wins outright. Enabling it is an operator decision made with
evidence.

Re-expressed 2026-09-07: the guard is no longer a whole-prompt, all-or-nothing test
inside G01. It is a reservation made once before Stage 3 (`middleware.cache_floor`) and
honoured by G01, G08 and G19 alike, scoped to the span the PROVIDER measures, and decided
by arithmetic over that provider's cache rates and the observed reuse rather than by a
threshold. The old shape had four defects — it compared the wrong quantity, it threw away
the suffix's compression too, it left G08/G19 free to undo it, and being cost-blind it
would have fired on a provider where holding tokens back costs 1.66x MORE. Every test
below that still describes true behaviour is kept verbatim; the ones that encoded the old
shape are re-expressed here rather than deleted, because the behaviour they cared about
(don't silently forfeit the discount) is still the point.
"""
import pytest

from middleware import cache_floor
from middleware.g01_compression import G01Compression


class _Adapter:
    """Minimal stand-in for a provider adapter. No provider NAME appears in the
    middleware under test (Gate 3) — the floor, the span and the cache rates all arrive
    through this interface."""

    name = "testprov"

    def __init__(self, floor, read_mult=0.1, write_mult=1.25, span_roles=None):
        self._floor = floor
        self._read = read_mult
        self._write = write_mult
        self._span_roles = span_roles  # None = whole prompt

    def min_cacheable_prompt_tokens(self, config, model):
        return self._floor

    def cacheable_span_messages(self, messages, params, cfg):
        if self._span_roles is None:
            return list(messages)
        return [m for m in messages if m.get("role") in self._span_roles]

    def cache_read_cost_multiplier(self, config):
        return self._read

    def cache_write_cost_multiplier(self, config):
        return self._write


async def _reserve(ctx, monkeypatch, reuse=30):
    """Make the reservation the pipeline would make, with the reuse count stubbed.

    The real count lives in Redis (it has to: a per-process counter would under-count by
    exactly the scale-out factor). Stubbing it here keeps the unit tests off the network
    while still exercising the same decision path.
    """
    monkeypatch.setattr(cache_floor, "_observed_reuse",
                        lambda *a, **k: _immediate(max(0, reuse - 1)))
    await cache_floor.reserve(ctx)


async def _immediate(value):
    return value


def _long_user(n=400):
    # Verbose, compressible prose — deterministic_fallback shortens it without a sidecar.
    return ("The quarterly infrastructure review notes that the deployment pipeline was "
            "in fact substantially delayed due to a number of various different factors. " * n)


def _crossing_floor(ctx):
    """A floor the prompt currently CLEARS but would fall under once compressed.

    The guard deliberately does nothing when `tokens_before` is already below the floor
    (the prefix was never cacheable, so compression is pure win) — so a floor set absurdly
    high does NOT exercise it. It has to straddle.
    """
    from savings.calculator import count_messages_tokens
    return count_messages_tokens(ctx.messages, ctx.model) - 1


def _ctx(make_ctx, minimal_config, floor, **g01):
    cfg = {"enabled": True, "min_tokens_to_compress": 10, "compress_user_messages": True,
           "kompress_enabled": False, "layered_composition_enabled": False,
           "selective_context_enabled": False, "deterministic_fallback": True}
    cfg.update(g01)
    minimal_config["groups"]["G1_compression"] = cfg
    ctx = make_ctx([{"role": "user", "content": _long_user()}], model="gpt-4o",
                   config=minimal_config)
    if floor is not None:
        ctx.provider_adapter = _Adapter(floor)
    return ctx


@pytest.mark.asyncio
class TestGuardIsOffByDefault:
    async def test_default_config_compresses_exactly_as_before(
            self, make_ctx, minimal_config, monkeypatch):
        """Byte-identical default: the guard must not be a silent behaviour change.

        Now proves it one step earlier than it used to — the RESERVATION declines, so
        nothing downstream (G01, G08 or G19) has anything to honour.
        """
        ctx = _ctx(make_ctx, minimal_config, None)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))  # would fire IF consulted
        before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before, (
            "with preserve_cacheable_prefix unset, even a floor the compression would "
            "cross must be ignored entirely"
        )
        assert getattr(out, "g01_cache_floor_skips", 0) == 0
        assert out.cache_floor_action == "none"


@pytest.mark.asyncio
class TestGuardWhenEnabled:
    async def test_the_span_is_held_when_holding_it_is_cheaper(
            self, make_ctx, minimal_config, monkeypatch):
        """Re-expression of `test_compression_is_abandoned_when_it_crosses_the_floor`.

        Same behaviour it always asserted — a compression that would drop the span under
        the provider's minimum does not happen — but reached through the arithmetic
        rather than a bare threshold, and with the reuse count high enough that holding
        the tokens genuinely IS the cheaper bill. No sidecar is configured here, so the
        floor-targeted retry produces nothing and this lands on the preserve fallback.
        """
        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))
        before = [dict(m) for m in ctx.messages]
        await _reserve(ctx, monkeypatch, reuse=30)
        out = await G01Compression().process_request(ctx)
        assert out.messages == before, "messages must be left exactly as they arrived"
        assert out.g01_cache_floor_skips == 1
        assert out.cache_floor_action == "preserved"

    async def test_a_single_shot_request_still_compresses(
            self, make_ctx, minimal_config, monkeypatch):
        """NEW, and the reason the old shape was a defect: at N=1 every floor LOSES.

        The first request pays a cache WRITE (>= 1.0x, 1.25x here) on a bigger span, so
        holding tokens back costs 1.7-3.5x what compressing costs. A guard that fires
        here takes money off the customer to protect a discount that never arrives.
        """
        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))
        before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch, reuse=1)
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before
        assert out.cache_floor_action == "none"

    async def test_a_provider_with_no_cache_discount_never_holds_tokens_back(
            self, make_ctx, minimal_config, monkeypatch):
        """NEW, and the reason a bare floor test is a defect rather than merely a blunt
        instrument: what the guard is buying is a DISCOUNT, and some providers offer
        none (the generic adapter's 1.0/1.0 default). Holding tokens back then pays full
        price for more tokens, forever, at every N. The exact per-provider break-evens
        live in test_cache_floor.py, where the DS8 numbers can be stated precisely."""
        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx), read_mult=1.0, write_mult=1.0)
        before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch, reuse=30)
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before
        assert out.cache_floor_action == "none"

    async def test_content_outside_the_cacheable_span_is_still_compressed(
            self, make_ctx, minimal_config, monkeypatch):
        """NEW, and the second defect of the first cut: it abandoned the WHOLE
        compression. The suffix is never cached, so its saving was never at stake —
        throwing it away paid for a discount it could not buy."""
        cfg = {"enabled": True, "min_tokens_to_compress": 10, "compress_user_messages": True,
               "compress_system_prompt": True, "kompress_enabled": False,
               "layered_composition_enabled": False, "selective_context_enabled": False,
               "deterministic_fallback": True, "preserve_cacheable_prefix": True}
        minimal_config["groups"]["G1_compression"] = cfg
        ctx = make_ctx([{"role": "system", "content": _long_user()},
                        {"role": "user", "content": _long_user()}],
                       model="gpt-4o", config=minimal_config)
        from savings.calculator import count_messages_tokens
        system_tokens = count_messages_tokens([ctx.messages[0]], ctx.model)
        ctx.provider_adapter = _Adapter(system_tokens - 1, span_roles={"system"})
        user_before = ctx.messages[1]["content"]
        system_before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch, reuse=30)
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] == system_before, "the span must be held"
        assert out.messages[1]["content"] != user_before, (
            "the suffix is never cached — holding it back buys nothing and costs tokens"
        )

    async def test_compression_proceeds_when_it_stays_above_the_floor(
            self, make_ctx, minimal_config, monkeypatch):
        """The guard must not become a blanket off-switch for compression."""
        ctx = _ctx(make_ctx, minimal_config, 1, preserve_cacheable_prefix=True)
        before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch)
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before
        assert getattr(out, "g01_cache_floor_skips", 0) == 0

    async def test_a_prompt_already_below_the_floor_is_still_compressed(
            self, make_ctx, minimal_config, monkeypatch):
        """Nothing to protect: the prefix was never cacheable, so compression is pure win.

        Pinning this matters — guarding it too would forfeit real savings to protect a
        discount that was never available in the first place.
        """
        ctx = _ctx(make_ctx, minimal_config, 10_000_000, preserve_cacheable_prefix=True)
        before = ctx.messages[0]["content"]  # floor far above the ORIGINAL size
        await _reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before, (
            "a span under the floor was never cacheable — compress freely"
        )

    async def test_a_provider_with_no_declared_minimum_is_unaffected(
            self, make_ctx, minimal_config, monkeypatch):
        ctx = _ctx(make_ctx, minimal_config, 0, preserve_cacheable_prefix=True)
        before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is False
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before

    async def test_a_broken_adapter_never_breaks_the_request(
            self, make_ctx, minimal_config, monkeypatch):
        """An adapter predating this interface must degrade to today's behaviour."""
        class _Old:
            name = "legacy"

        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Old()
        before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch)
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before

    async def test_an_unreachable_reuse_counter_is_inert(
            self, make_ctx, minimal_config, monkeypatch):
        """Unknown reuse must never buy a cache write. Redis down, absent or erroring
        reads as "no idea", and the fail-SAFE direction is to compress."""
        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))
        monkeypatch.setattr(cache_floor, "_observed_reuse", lambda *a, **k: _immediate(None))
        before = ctx.messages[0]["content"]
        await cache_floor.reserve(ctx)
        assert cache_floor.get(ctx).active is False
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before

    async def test_a_cache_hit_makes_no_reservation(
            self, make_ctx, minimal_config, monkeypatch):
        """The provider is never called on a cache hit, so there is no prefix cache to
        protect — and counting the sighting would inflate the reuse total with requests
        the provider never saw."""
        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))
        ctx.cache_hit = True
        seen = []
        monkeypatch.setattr(cache_floor, "_observed_reuse",
                            lambda *a, **k: (seen.append(1), _immediate(29))[1])
        await cache_floor.reserve(ctx)
        assert cache_floor.get(ctx).active is False
        assert seen == [], "a cache hit must not increment the reuse counter"


class TestAdapterFloorResolution:
    """The floor is config-driven so an operator can track a provider's published
    minimum without a redeploy (Gate 2)."""

    def _adapter(self):
        from providers.openai_adapter import OpenAIAdapter
        return OpenAIAdapter()

    def test_reads_the_value_from_a_list_shaped_providers_block(self):
        cfg = {"providers": [{"name": "openai", "min_cacheable_tokens": 1024}]}
        assert self._adapter().min_cacheable_prompt_tokens(cfg, "gpt-4o-mini") == 1024

    def test_reads_the_value_from_a_dict_shaped_providers_block(self):
        cfg = {"providers": {"openai": {"min_cacheable_tokens": 2048}}}
        assert self._adapter().min_cacheable_prompt_tokens(cfg, "gpt-4o-mini") == 2048

    def test_absent_means_zero_which_leaves_the_guard_inert(self):
        assert self._adapter().min_cacheable_prompt_tokens({"providers": []}, "gpt-4o-mini") == 0
        assert self._adapter().min_cacheable_prompt_tokens({}, "gpt-4o-mini") == 0

    def test_a_junk_value_degrades_to_inert_rather_than_raising(self):
        """config.yaml is operator-edited; a typo must not 500 every request."""
        cfg = {"providers": [{"name": "openai", "min_cacheable_tokens": "lots"}]}
        assert self._adapter().min_cacheable_prompt_tokens(cfg, "gpt-4o-mini") == 0

    def test_a_negative_value_is_clamped(self):
        cfg = {"providers": [{"name": "openai", "min_cacheable_tokens": -5}]}
        assert self._adapter().min_cacheable_prompt_tokens(cfg, "gpt-4o-mini") == 0

    def test_the_shipped_template_declares_a_minimum(self):
        """The knob is useless if no provider declares a floor."""
        import yaml
        from pathlib import Path
        tmpl = Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
        cfg = yaml.safe_load(tmpl.read_text(encoding="utf-8"))
        declared = {p["name"]: p.get("min_cacheable_tokens")
                    for p in cfg.get("providers", []) if isinstance(p, dict)}
        assert declared.get("openai") == 1024
        assert cfg["groups"]["G1_compression"]["preserve_cacheable_prefix"] is False, (
            "the guard must ship OFF — enabling it changes savings on the default path"
        )


@pytest.mark.asyncio
class TestArmC:
    """Arm C — compress the span DOWN TO the floor instead of below it.

    This is the arm that makes the guard worth having: arm B (hold the span whole) gives
    back every token in the prefix, while arm C gives back only the ones that stood
    between the compressed span and the provider's minimum. It needs a compression engine
    with a rate dial, which on this deployment is the LLMLingua sidecar — `ratio` is a
    KEEP-fraction, so aiming just above the floor is one more call rather than a search.

    WITHOUT that sidecar there is no dial and the code falls back to arm B, preserving the
    span whole. The tests below stub the sidecar precisely because the deterministic
    fallback cannot express a target rate; `docs/config-reference.md` and the config
    template say so plainly rather than implying arm C is always available.
    """

    def _stub_sidecar(self, monkeypatch, keep=None):
        """A sidecar that honours `ratio` as a keep-fraction, on word boundaries.

        `keep` overrides it with a fixed fraction — the undershoot case, where the engine
        simply cannot land where it was asked to.
        """
        import middleware.g01_compression as g01

        async def _call(url, text, ratio, force_reserve_digit=True):
            words = text.split()
            frac = keep if keep is not None else ratio
            return " ".join(words[:max(1, int(len(words) * frac))])

        monkeypatch.setattr(g01, "_call_llmlingua", _call)

    def _ctx_two_part(self, make_ctx, minimal_config, first_pass_ratio=0.3):
        """A marker-shaped provider: `system` is the cacheable span, `user` is the suffix
        that is never cached and whose compression is therefore never at stake."""
        minimal_config["groups"]["G1_compression"] = {
            "enabled": True, "min_tokens_to_compress": 10, "min_chars_to_compress": 50,
            "compress_user_messages": True, "compress_system_prompt": True,
            "kompress_enabled": False, "layered_composition_enabled": False,
            "selective_context_enabled": False, "deterministic_fallback": False,
            "preserve_cacheable_prefix": True,
            "compression_ratio_target": first_pass_ratio,
        }
        sentence = "The deployment pipeline review records the same finding again. "
        return make_ctx(
            [{"role": "system", "content": sentence * 400},
             {"role": "user", "content": sentence * 200}],
            model="gpt-4o", config=minimal_config)

    def _span_floor(self, ctx, fraction=0.5):
        from savings.calculator import count_messages_tokens
        span = count_messages_tokens(
            [m for m in ctx.messages if m["role"] == "system"], ctx.model)
        return int(span * fraction), span

    async def test_the_span_lands_on_the_floor_and_the_suffix_stays_compressed(
            self, make_ctx, minimal_config, monkeypatch):
        ctx = self._ctx_two_part(make_ctx, minimal_config)
        floor, span_before = self._span_floor(ctx)
        ctx.provider_adapter = _Adapter(floor, span_roles={"system"})
        self._stub_sidecar(monkeypatch)
        sys_before, usr_before = (m["content"] for m in ctx.messages)
        await _reserve(ctx, monkeypatch)
        assert cache_floor.get(ctx).active is True

        out = await G01Compression().process_request(ctx)

        assert out.cache_floor_action == "floored", (
            "the milder pass landed above the floor, so this is arm C — reporting it as "
            "anything else would claim a mechanism that did not fire (Gate 8.6)"
        )
        from savings.calculator import count_messages_tokens
        landed = count_messages_tokens(
            [m for m in out.messages if m["role"] == "system"], ctx.model)
        assert landed >= floor, "arm C must land ON the floor, never under it"
        assert landed < span_before, "…and it must still be a compression, not arm B"
        assert out.messages[0]["content"] != sys_before
        assert out.messages[1]["content"] != usr_before, (
            "the suffix is never cached, so its compression is free money and must be "
            "kept even while the span is being protected"
        )
        assert getattr(out, "g01_cache_floor_skips", 0) == 0

    async def test_an_undershooting_retry_falls_back_to_preserving_the_span(
            self, make_ctx, minimal_config, monkeypatch):
        """When the engine cannot land where it was asked to, the result IS arm B and
        must be reported as arm B — and the in-span originals must come back whole, not
        be left at whatever the undershoot produced."""
        ctx = self._ctx_two_part(make_ctx, minimal_config)
        floor, _ = self._span_floor(ctx)
        ctx.provider_adapter = _Adapter(floor, span_roles={"system"})
        self._stub_sidecar(monkeypatch, keep=0.2)   # ignores the target rate
        sys_before, usr_before = (m["content"] for m in ctx.messages)
        await _reserve(ctx, monkeypatch)

        out = await G01Compression().process_request(ctx)

        assert out.cache_floor_action == "preserved"
        assert out.messages[0]["content"] == sys_before, "the span comes back WHOLE"
        assert out.messages[1]["content"] != usr_before, "the suffix stays compressed"
        assert getattr(out, "g01_cache_floor_skips", 0) == 1

    async def test_without_a_rate_dial_the_span_is_preserved_whole(
            self, make_ctx, minimal_config, monkeypatch):
        """No sidecar → the deterministic fallback compresses but cannot be aimed, so
        there is no arm C and the documented behaviour is arm B. This is what the config
        template and config-reference promise an operator with no sidecar deployed."""
        ctx = self._ctx_two_part(make_ctx, minimal_config)
        ctx.config["groups"]["G1_compression"]["deterministic_fallback"] = True
        import middleware.g01_compression as g01
        from savings.calculator import count_messages_tokens

        async def _dead(url, text, ratio, force_reserve_digit=True):
            return text          # what the real helper returns when the sidecar is down

        monkeypatch.setattr(g01, "_call_llmlingua", _dead)
        # Set the floor BETWEEN what the deterministic pass produces and the original, so
        # the first pass genuinely crosses it and the retry is the thing under test.
        _, span_before = self._span_floor(ctx)
        det = g01._prose_compress_text(ctx.messages[0]["content"])
        det_tokens = count_messages_tokens(
            [{**ctx.messages[0], "content": det}], ctx.model)
        assert det_tokens < span_before, "the deterministic fallback must compress here"
        floor = (det_tokens + span_before) // 2
        ctx.provider_adapter = _Adapter(floor, span_roles={"system"})
        sys_before = ctx.messages[0]["content"]
        await _reserve(ctx, monkeypatch)

        out = await G01Compression().process_request(ctx)

        assert out.cache_floor_action == "preserved"
        assert out.messages[0]["content"] == sys_before
