"""`off` is our tier vocabulary and must never reach a provider (backlog #42).

G25 writes `ctx.params["reasoning_effort"]` and G12 consumes it. When the tier is `off`,
G12 clears the key — but the two groups are independently switchable, and the ablation
harness disables G12 while leaving G25 on. In that combination nothing cleared the key and
the literal string `"off"` would have been forwarded: litellm either rejects it or expands
it into a thinking budget, i.e. turns reasoning ON for a request that asked for none.

Found by reasoning about what enabling G25 in the harness's all-off arm would do, before
running it — the arm that would have exhibited it bills real money.
"""
import pytest

from providers import REASONING_OFF, outgoing_params_for
from providers.anthropic_adapter import AnthropicAdapter
from providers.openai_adapter import OpenAIAdapter


class _Ctx:
    def __init__(self, params):
        self.params = params
        self.tenant_id = "t1"


def _out(params, adapter, model):
    return outgoing_params_for(_Ctx(params), adapter, model, {}, "req-1")


class TestTheSentinelIsStripped:
    def test_anthropic_never_receives_it(self):
        """The combination that matters: a reasoning-capable model, so the
        does-not-support-reasoning strip above does NOT fire and cannot save us."""
        a = AnthropicAdapter()
        assert a.supports_reasoning("claude-sonnet-4-5") is True, "premise of this test"
        out = _out({"reasoning_effort": REASONING_OFF, "max_tokens": 4096},
                   a, "claude-sonnet-4-5")
        assert "reasoning_effort" not in out

    def test_openai_o_series_never_receives_it(self):
        out = _out({"reasoning_effort": REASONING_OFF}, OpenAIAdapter(), "o4-mini")
        assert "reasoning_effort" not in out

    def test_no_thinking_budget_is_conjured_in_its_place(self):
        """Stripping must not be replaced by an enabling param — the whole point is that
        this request asked for no reasoning."""
        out = _out({"reasoning_effort": REASONING_OFF, "max_tokens": 4096},
                   AnthropicAdapter(), "claude-sonnet-4-5")
        assert "thinking" not in out and "thinking_config" not in out


class TestRealTiersAreUntouched:
    @pytest.mark.parametrize("tier", ["low", "medium", "high"])
    def test_a_genuine_effort_still_reaches_the_provider(self, tier):
        out = _out({"reasoning_effort": tier}, OpenAIAdapter(), "o4-mini")
        assert out["reasoning_effort"] == tier

    def test_an_explicit_thinking_param_is_preserved(self):
        out = _out({"thinking": {"type": "enabled", "budget_tokens": 2000},
                    "max_tokens": 4096}, AnthropicAdapter(), "claude-sonnet-4-5")
        assert out["thinking"]["budget_tokens"] == 2000

    def test_other_params_are_not_disturbed(self):
        out = _out({"reasoning_effort": REASONING_OFF, "max_tokens": 4096, "top_p": 0.9},
                   OpenAIAdapter(), "o4-mini")
        assert out["max_tokens"] == 4096 and out["top_p"] == 0.9
