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
"""
import pytest

from middleware.g01_compression import G01Compression


class _Adapter:
    """Minimal stand-in for a provider adapter. No provider NAME appears in the
    middleware under test (Gate 3) — the floor arrives through this interface."""

    name = "testprov"

    def __init__(self, floor):
        self._floor = floor

    def min_cacheable_prompt_tokens(self, config, model):
        return self._floor


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
    async def test_default_config_compresses_exactly_as_before(self, make_ctx, minimal_config):
        """Byte-identical default: the guard must not be a silent behaviour change."""
        ctx = _ctx(make_ctx, minimal_config, None)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))  # would fire IF consulted
        before = ctx.messages[0]["content"]
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before, (
            "with preserve_cacheable_prefix unset, even a floor the compression would "
            "cross must be ignored entirely"
        )
        assert getattr(out, "g01_cache_floor_skips", 0) == 0


@pytest.mark.asyncio
class TestGuardWhenEnabled:
    async def test_compression_is_abandoned_when_it_crosses_the_floor(
            self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Adapter(_crossing_floor(ctx))
        before = [dict(m) for m in ctx.messages]
        out = await G01Compression().process_request(ctx)
        assert out.messages == before, "messages must be left exactly as they arrived"
        assert out.g01_cache_floor_skips == 1

    async def test_compression_proceeds_when_it_stays_above_the_floor(
            self, make_ctx, minimal_config):
        """The guard must not become a blanket off-switch for compression."""
        ctx = _ctx(make_ctx, minimal_config, 1, preserve_cacheable_prefix=True)
        before = ctx.messages[0]["content"]
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before
        assert getattr(out, "g01_cache_floor_skips", 0) == 0

    async def test_a_prompt_already_below_the_floor_is_still_compressed(
            self, make_ctx, minimal_config):
        """Nothing to protect: the prefix was never cacheable, so compression is pure win.

        Pinning this matters — guarding it too would forfeit real savings to protect a
        discount that was never available in the first place.
        """
        ctx = _ctx(make_ctx, minimal_config, 10_000_000, preserve_cacheable_prefix=True)
        before = ctx.messages[0]["content"]  # floor far above the ORIGINAL size
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before, (
            "tokens_before < floor means the prefix was never cacheable — compress freely"
        )

    async def test_a_provider_with_no_declared_minimum_is_unaffected(
            self, make_ctx, minimal_config):
        ctx = _ctx(make_ctx, minimal_config, 0, preserve_cacheable_prefix=True)
        before = ctx.messages[0]["content"]
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before

    async def test_a_broken_adapter_never_breaks_the_request(self, make_ctx, minimal_config):
        """An adapter predating this interface must degrade to today's behaviour."""
        class _Old:
            name = "legacy"

        ctx = _ctx(make_ctx, minimal_config, None, preserve_cacheable_prefix=True)
        ctx.provider_adapter = _Old()
        before = ctx.messages[0]["content"]
        out = await G01Compression().process_request(ctx)
        assert out.messages[0]["content"] != before


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
