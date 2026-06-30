"""G06 routing-disabled / no-tiers path must preserve any model that resolves to a
configured provider (by prefix), not just ones enumerated in providers[].models — else a
deliberately-chosen provider model (e.g. gemini-2.5-flash) is silently downgraded to the
default OpenAI model.
"""
from middleware.g06_routing import _is_configured_model


def test_provider_prefix_model_is_configured():
    # 'gemini'/'claude'/'gpt' are in the built-in default prefix map even without a loaded
    # config, so these resolve to a configured provider and must be preserved.
    assert _is_configured_model("gemini-2.5-flash") is True
    assert _is_configured_model("claude-haiku-4-5") is True
    assert _is_configured_model("gpt-4o-mini") is True


def test_unknown_model_is_not_configured():
    assert _is_configured_model("totally-unknown-model-xyz") is False
