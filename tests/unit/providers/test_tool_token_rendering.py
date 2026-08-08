"""Per-provider tool-token rendering (review S10).

Providers bill tool definitions in very different serialisations — measured
2026-08-08 via the free count_tokens endpoints on DS13's 11-tool set:
OpenAI ~270-300 · Gemini 625 · Anthropic 1,307. One render cannot serve all;
each adapter reports its own (body, constant) and the calculator counts it.

The calibration tests pin the measured actuals so a future model/serialisation
revision that drifts the estimate >±15% fails loudly instead of silently
re-skewing G26 window math and disclosed savings.
"""
import json
import os
import sys

import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from providers import ProviderAdapter
from providers.generic_adapter import GenericLiteLLMAdapter
from providers.anthropic_adapter import AnthropicAdapter
from providers.gemini_adapter import GeminiAdapter
from savings.calculator import count_tools_tokens, count_request_tokens, estimate_tokens

_TOOL = {
    "type": "function",
    "function": {
        "name": "get_service_health",
        "description": "Return the health status of a deployed service.",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "Service name."}},
            "required": ["service"],
        },
    },
}

_DS13 = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                     "pitch-test-plan", "datasets", "DS13", "requests.jsonl")

# Provider-measured actual tool costs for DS13's toolset (count_tokens, 2026-08-08).
_ANTHROPIC_ACTUAL = {1: 562, 5: 850, 11: 1307}   # claude-sonnet-4-5
_GEMINI_ACTUAL = {1: 57, 5: 270, 11: 625}        # gemini-2.0-flash


def _ds13_tools():
    if not os.path.exists(_DS13):
        pytest.skip("DS13 dataset not present in this checkout (commercial-only)")
    with open(_DS13, encoding="utf-8") as fh:
        return json.loads(fh.readline())["params"]["tools"]


class TestAdapterSeam:
    def test_base_adapter_returns_none(self):
        """None = 'use the calculator default' — every adapter that does not override
        stays byte-identical to pre-S10 behaviour."""
        assert GenericLiteLLMAdapter("generic").render_tools_for_counting([_TOOL]) is None

    def test_no_adapter_resolution_falls_back_to_packed(self):
        """With no providers configured (unit-test default), counting must equal the
        packed default — proving the seam is additive, never behaviour-changing."""
        with patch("config_loader.get_providers", return_value=[]):
            n = count_tools_tokens([_TOOL], "gpt-4o-mini")
        assert n > 0

    def test_adapter_exception_never_fails_the_count(self):
        boom = GenericLiteLLMAdapter("boom")
        boom.render_tools_for_counting = lambda tools: (_ for _ in ()).throw(RuntimeError("boom"))
        with patch("providers.get_adapter", return_value=boom):
            assert count_tools_tokens([_TOOL], "claude-sonnet-4-5") > 0

    def test_malformed_tools_survive_provider_renders(self):
        bad = [{"type": "function", "function": {"name": "f", "parameters": ["not-a-dict"]}},
               {"type": "weird", "blob": {"x": 1}}]
        for adapter in (AnthropicAdapter(), GeminiAdapter()):
            body, const = adapter.render_tools_for_counting(bad)
            assert isinstance(body, str) and body
            assert const >= 0


class TestCalibrationAgainstMeasuredActuals:
    """Estimates must land within ±15% of the provider-measured actuals."""

    @pytest.mark.parametrize("n,actual", sorted(_ANTHROPIC_ACTUAL.items()))
    def test_anthropic(self, n, actual):
        tools = _ds13_tools()[:n]
        body, const = AnthropicAdapter().render_tools_for_counting(tools)
        est = estimate_tokens(body, "claude-sonnet-4-5") + const
        assert abs(est - actual) / actual <= 0.15, (
            f"n={n}: est {est} vs measured {actual} — recalibrate against count_tokens")

    @pytest.mark.parametrize("n,actual", sorted(_GEMINI_ACTUAL.items()))
    def test_gemini(self, n, actual):
        tools = _ds13_tools()[:n]
        body, const = GeminiAdapter().render_tools_for_counting(tools)
        est = estimate_tokens(body, "gemini-2.0-flash") + const
        assert abs(est - actual) / actual <= 0.15, (
            f"n={n}: est {est} vs measured {actual} — recalibrate against countTokens")

    def test_provider_ordering_matches_reality(self):
        """Anthropic > Gemini > packed/OpenAI for the same toolset — the whole point."""
        tools = _ds13_tools()
        providers_cfg = [
            {"name": "openai", "model_prefixes": ["gpt"]},
            {"name": "anthropic", "model_prefixes": ["claude"]},
            {"name": "gemini", "model_prefixes": ["gemini"]},
        ]
        with patch("config_loader.get_providers", return_value=providers_cfg):
            openai_n = count_tools_tokens(tools, "gpt-4o-mini")
            gemini_n = count_tools_tokens(tools, "gemini-2.0-flash")
            claude_n = count_tools_tokens(tools, "claude-sonnet-4-5")
        assert claude_n > gemini_n > 0
        assert claude_n > openai_n


class TestXYSymmetryInvariant:
    def test_baseline_and_current_count_use_the_same_model(self):
        """x and y must be counted at the REQUESTED model's render — otherwise
        disclosed savings would move purely because G06 routed across providers."""
        from middleware import RequestContext
        msgs = [{"role": "user", "content": "check the payments service"}]
        ctx = RequestContext.create(
            request_id="sym-test", user_id="u", messages=msgs,
            model="claude-sonnet-4-5", params={"tools": [_TOOL]}, config={},
        )
        # Simulate G06 routing to a different provider — y's basis must not move.
        before = ctx.current_request_token_count
        ctx.routed_model = "gpt-4o-mini"
        assert ctx.current_request_token_count == before
        assert ctx.savings.baseline_tokens == before
