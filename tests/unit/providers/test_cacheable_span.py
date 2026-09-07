"""Provider facts about prefix caching live on the adapter, never in middleware.

Backlog #41. Two facts decide whether a prefix-cache floor guard fires on the right
requests, and both are provider-specific:

  * the MINIMUM size a provider will cache a prefix at — and for at least one provider
    that minimum is model-dependent, so a single per-provider number is wrong for half
    its catalog;
  * WHICH span the provider measures that minimum over — the whole serialized prompt
    prefix for some, only the block up to an explicit cache-control marker for others.

Comparing the wrong span against the floor produces a guard that fires on the wrong
requests and stands down on the right ones, so this is pinned rather than assumed. Model
names appear here and in config; they never appear in middleware (Gate 3).
"""
import pytest


def _openai():
    from providers.openai_adapter import OpenAIAdapter
    return OpenAIAdapter()


def _anthropic():
    from providers.anthropic_adapter import AnthropicAdapter
    return AnthropicAdapter()


def _generic():
    from providers.generic_adapter import GenericLiteLLMAdapter
    return GenericLiteLLMAdapter("someprov")


class TestPerModelFloor:
    def test_a_by_model_entry_wins_over_the_flat_value(self):
        cfg = {"providers": [{"name": "anthropic", "min_cacheable_tokens": 1024,
                              "min_cacheable_tokens_by_model": {"claude-haiku-4-5": 4096}}]}
        a = _anthropic()
        assert a.min_cacheable_prompt_tokens(cfg, "claude-haiku-4-5") == 4096
        assert a.min_cacheable_prompt_tokens(cfg, "claude-sonnet-5") == 1024

    def test_the_longest_matching_pattern_wins(self):
        """`claude-3-5-haiku` must not be served `claude-3`'s value just because that
        prefix also matches — the specific model is the one the operator meant."""
        cfg = {"providers": [{"name": "anthropic", "min_cacheable_tokens": 1024,
                              "min_cacheable_tokens_by_model": {
                                  "claude-3": 999, "claude-3-5-haiku": 2048}}]}
        assert _anthropic().min_cacheable_prompt_tokens(cfg, "claude-3-5-haiku-20241022") == 2048

    def test_a_trailing_star_is_accepted(self):
        cfg = {"providers": [{"name": "anthropic",
                              "min_cacheable_tokens_by_model": {"claude-haiku*": 4096}}]}
        assert _anthropic().min_cacheable_prompt_tokens(cfg, "claude-haiku-4-5") == 4096

    def test_a_junk_by_model_value_falls_back_rather_than_raising(self):
        """config.yaml is operator-edited; a typo must not 500 every request."""
        cfg = {"providers": [{"name": "anthropic", "min_cacheable_tokens": 1024,
                              "min_cacheable_tokens_by_model": {"claude-sonnet": "lots"}}]}
        assert _anthropic().min_cacheable_prompt_tokens(cfg, "claude-sonnet-5") == 1024

    def test_a_malformed_by_model_block_is_ignored(self):
        cfg = {"providers": [{"name": "anthropic", "min_cacheable_tokens": 1024,
                              "min_cacheable_tokens_by_model": "nope"}]}
        assert _anthropic().min_cacheable_prompt_tokens(cfg, "claude-sonnet-5") == 1024


class TestCacheableSpan:
    _MSGS = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]

    def test_the_base_default_is_the_whole_prompt(self):
        """Both the commoner provider shape AND the behaviour that existed before any of
        this, so an adapter that does not override changes nothing."""
        assert _openai().cacheable_span_messages(self._MSGS, {}, {}) == self._MSGS
        assert _generic().cacheable_span_messages(self._MSGS, {}, {}) == self._MSGS

    # The marker this provider's cacheable block depends on is opt-in and ships OFF.
    _MARKER_ON = {"groups": {"G21_cache_alignment": {
        "providers": {"anthropic": {"marker": True}}}}}

    def test_a_marker_based_provider_measures_only_its_cached_block(self):
        span = _anthropic().cacheable_span_messages(self._MSGS, {}, self._MARKER_ON)
        assert span == [{"role": "system", "content": "policy"}]

    def test_no_marker_means_no_cacheable_span_at_all(self):
        """The marker ships OFF, and without it this provider caches nothing. Reporting a
        span anyway would have the floor guard hold tokens back to defend a discount that
        was never going to be granted — a straight loss for the customer, on the shipped
        default."""
        for config in ({}, {"groups": {"G21_cache_alignment": {
                "providers": {"anthropic": {"marker": False}}}}}):
            assert _anthropic().cacheable_span_messages(self._MSGS, {}, config) == []

    def test_the_span_matches_where_that_adapter_places_its_marker(self):
        """align_prefix marks the last SYSTEM message and the last tool. The block whose
        size decides whether the marker pays out must be the block the marker covers — if
        these two ever drift apart the guard measures one thing and the provider another.
        """
        import inspect
        src = inspect.getsource(_anthropic().align_prefix)
        assert "system_msgs[-1][\"cache_control\"]" in src
        assert "tools[-1][\"cache_control\"]" in src
        assert "provider_cfg.get(\"marker\"" in inspect.getsource(
            _anthropic().cacheable_span_messages), (
            "align_prefix returns False without the marker, so the span must be empty "
            "without it too — or the guard defends a cache that is never written"
        )
        span = _anthropic().cacheable_span_messages(self._MSGS, {}, self._MARKER_ON)
        assert all(m["role"] == "system" for m in span), (
            "the marker sits on system + tools, so the span is system + tools"
        )

    def test_an_empty_conversation_is_handled(self):
        assert _anthropic().cacheable_span_messages([], {}, self._MARKER_ON) == []


class TestTemplateDeclarations:
    """The knobs are useless if the shipped template declares nothing, and dangerous if
    it declares something unverified without saying so."""

    def _template(self):
        import yaml
        from pathlib import Path
        tmpl = Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
        return tmpl.read_text(encoding="utf-8"), yaml.safe_load(tmpl.read_text(encoding="utf-8"))

    def test_every_first_party_provider_declares_a_minimum(self):
        _raw, cfg = self._template()
        declared = {p["name"]: p.get("min_cacheable_tokens")
                    for p in cfg.get("providers", []) if isinstance(p, dict)}
        for name in ("openai", "anthropic", "gemini"):
            assert declared.get(name), f"{name} declares no minimum cacheable size"

    def test_the_model_dependent_provider_carries_a_by_model_map(self):
        _raw, cfg = self._template()
        entry = next(p for p in cfg["providers"] if p.get("name") == "anthropic")
        by_model = entry.get("min_cacheable_tokens_by_model") or {}
        assert by_model, "this provider's minimum is model-dependent; a flat value is wrong"
        assert len(set(by_model.values())) > 1, (
            "a by-model map whose values are all equal is a flat value wearing a costume"
        )

    def test_the_provenance_of_these_numbers_is_stated_in_the_template(self):
        """These came from provider documentation, not from a measurement of ours. An
        operator flipping the guard on needs to know that before trusting them."""
        raw, _cfg = self._template()
        assert raw.count("From provider docs, 2026-09 -- verify before any default flip.") >= 3
