"""outgoing_params_for never forwards a litellm call argument, whatever reached params.

The OpenAI ingress allowlist is the first line of defence. This is the second: it also
covers failover targets and G06 cascade tiers, which share outgoing_params_for, and it
holds even if an operator lists a call argument in ingress.extra_allowed_params."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

from providers import LITELLM_CONTROL_KEYS, outgoing_params_for
from providers.anthropic_adapter import AnthropicAdapter
from providers.openai_adapter import OpenAIAdapter


class _Ctx:
    def __init__(self, params):
        self.params = params
        self.tenant_id = "t1"


def _out(params, adapter, model, cfg=None):
    return outgoing_params_for(_Ctx(params), adapter, model, cfg or {}, "req-1")


@pytest.mark.parametrize("key", sorted(LITELLM_CONTROL_KEYS))
def test_control_key_is_never_forwarded(key):
    out = _out({"temperature": 0.3, key: "https://attacker.example"}, OpenAIAdapter(), "gpt-4o-mini")
    assert key not in out
    assert out.get("temperature") == 0.3


@pytest.mark.parametrize("key", ["aws_secret_access_key", "azure_ad_token", "litellm_metadata",
                                 "mock_response", "mock_tool_calls", "vertex_credentials",
                                 "watsonx_api_key"])
def test_control_key_families_are_never_forwarded(key):
    assert key not in _out({key: "x"}, OpenAIAdapter(), "gpt-4o-mini")


def test_adapter_supplied_headers_survive_but_client_supplied_do_not():
    # The Anthropic adapter adds its context-editing beta header AFTER the strip, so its
    # own header reaches the call and a client's extra_headers never merges into it.
    cfg = {"groups": {"context_editing": {"enabled": True}}}
    out = _out({"extra_headers": {"x-exfil": "1", "anthropic-beta": "client-beta"}},
               AnthropicAdapter(), "claude-sonnet-4-5", cfg)
    assert out["extra_headers"] == {"anthropic-beta": AnthropicAdapter._CONTEXT_MGMT_BETA}
