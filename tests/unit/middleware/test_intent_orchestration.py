"""Unit tests for F2 Intent-Based Multi-Agent Orchestration (OSS-core engine)."""
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from middleware import RequestContext
from middleware.intent_orchestration import (
    MAX_AGENT_TIMEOUT_SECONDS,
    IntentOrchestration,
    _orchestration_cfg,
    classify_intent,
    validate_outbound_url,
)
from savings.models import SavingsRecord



@pytest.fixture(autouse=True)
def _agent_hosts_resolve_publicly(monkeypatch):
    """A tenant's agent host is resolved before it is called; here every name resolves to a
    public address, so no test depends on real DNS."""
    async def resolve(host):
        return ["93.184.216.34"]
    monkeypatch.setattr("middleware.intent_orchestration._host_addresses", resolve)

def _ctx(*, config, tenant_id="default", messages=None, model="gpt-4o-mini", **flags):
    msgs = messages or [{"role": "user", "content": "please process my refund"}]
    ctx = RequestContext(
        request_id="req-1", user_id="u@x.test",
        original_messages=list(msgs), messages=list(msgs),
        model=model, routed_model=model, params={}, config=config,
        savings=SavingsRecord(request_id="req-1", user_id="u@x.test",
                              timestamp=datetime.now(timezone.utc),
                              model_requested=model, routed_model=model, baseline_tokens=10),
        tenant_id=tenant_id,
    )
    for k, v in flags.items():
        setattr(ctx, k, v)
    return ctx


def _cfg(*, enabled=True, agents=None, threshold=1, tenants=None):
    c = {"orchestration": {"enabled": enabled, "confidence_threshold": threshold,
                           "agents": agents if agents is not None else [
                               {"id": "billing", "url": "https://billing.agents.example/v1",
                                "match": ["refund", "invoice", "billing"]}]}}
    if tenants:
        c["tenants"] = tenants
    return c


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def model_dump(self):
        return self._p


def _openai_response(text="agent answer"):
    return {"id": "cmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}


# ── classify_intent (pure) ────────────────────────────────────────────────────────────
def test_classify_matches_best_agent():
    agents = [{"id": "billing", "match": ["refund", "invoice"]},
              {"id": "sre", "match": ["server", "outage"]}]
    agent, score = classify_intent("please process my refund and invoice", agents, 1)
    assert agent["id"] == "billing" and score == 2


def test_classify_no_match_returns_none():
    agents = [{"id": "billing", "match": ["refund"]}]
    assert classify_intent("what is the weather today", agents, 1) == (None, 0)


def test_classify_threshold_not_met():
    agents = [{"id": "billing", "match": ["refund", "invoice"]}]
    a, s = classify_intent("refund please", agents, threshold=2)  # only 1 hit, needs 2
    assert a is None and s == 1


def test_classify_tie_breaks_on_registry_order():
    agents = [{"id": "first", "match": ["help"]}, {"id": "second", "match": ["help"]}]
    agent, _ = classify_intent("i need help", agents, 1)
    assert agent["id"] == "first"


def test_classify_agent_without_match_not_selectable():
    agents = [{"id": "desc-only", "description": "billing refunds"}]  # no `match`
    assert classify_intent("refund", agents, 1) == (None, 0)


def test_classify_empty_text():
    assert classify_intent("", [{"id": "b", "match": ["x"]}], 1) == (None, 0)


def test_classify_word_boundary_no_substring_false_positive():
    # "bill" must not match inside "billboard"
    agents = [{"id": "billing", "match": ["bill"]}]
    assert classify_intent("look at that billboard", agents, 1) == (None, 0)


# ── _orchestration_cfg per-tenant override (Gate 2) ───────────────────────────────────
def test_tenant_agents_replace_global_never_merge():
    cfg = _cfg(agents=[{"id": "global", "match": ["x"]}],
               tenants={"ACME": {"orchestration": {"agents": [{"id": "acme", "match": ["y"]}]}}})
    assert [a["id"] for a in _orchestration_cfg(cfg, "ACME")["agents"]] == ["acme"]
    assert [a["id"] for a in _orchestration_cfg(cfg, "OTHER")["agents"]] == ["global"]


# ── api_key_env is operator-only ──────────────────────────────────────────────────────
# `api_key_env` names a SERVER environment variable. An agent that reached ctx.config from
# a tenant override (the portal) must never make the proxy read one and send it to the
# tenant's url. An agent defined in the operator's own config still gets its key.
_PLATFORM_SECRET = "sk-platform-secret-must-not-leave"


def _agent(**over):
    agent = {"id": "billing", "url": "https://tenant-agent.example/v1",
             "match": ["refund"], "api_key_env": "LLM_KEY_OPENAI"}
    agent.update(over)
    return agent


async def _dispatched_api_key(ctx, operator_config):
    with patch("config_loader.get_config", return_value=operator_config), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is True
    return m.call_args.kwargs["api_key"]


async def test_tenant_agent_cannot_read_a_server_env_var(monkeypatch):
    monkeypatch.setenv("LLM_KEY_OPENAI", _PLATFORM_SECRET)
    ctx = _ctx(config=_cfg(agents=[_agent()]), tenant_id="ACME")
    assert await _dispatched_api_key(ctx, operator_config=_cfg(agents=[])) == "no-key"


async def test_operator_agent_still_gets_its_key(monkeypatch):
    monkeypatch.setenv("BILLING_AGENT_KEY", "sk-agent-key")
    agent = _agent(url="https://billing.agents.example/v1", api_key_env="BILLING_AGENT_KEY")
    ctx = _ctx(config=_cfg(agents=[agent]))
    assert await _dispatched_api_key(ctx, operator_config=_cfg(agents=[agent])) == "sk-agent-key"


async def test_static_per_tenant_operator_agent_gets_its_key(monkeypatch):
    monkeypatch.setenv("ACME_AGENT_KEY", "sk-acme")
    agent = _agent(url="http://acme-agent/v1", api_key_env="ACME_AGENT_KEY")
    operator = _cfg(agents=[], tenants={"ACME": {"orchestration": {"agents": [agent]}}})
    ctx = _ctx(config=operator, tenant_id="ACME")
    assert await _dispatched_api_key(ctx, operator_config=operator) == "sk-acme"


async def test_tenant_cannot_reuse_an_operator_agent_key_at_its_own_url(monkeypatch):
    monkeypatch.setenv("BILLING_AGENT_KEY", "sk-agent-key")
    operator_agent = _agent(url="https://billing.agents.example/v1", api_key_env="BILLING_AGENT_KEY")
    hijacked = dict(operator_agent, url="https://tenant-agent.example/v1")
    ctx = _ctx(config=_cfg(agents=[hijacked]), tenant_id="ACME")
    assert await _dispatched_api_key(ctx, operator_config=_cfg(agents=[operator_agent])) == "no-key"


async def test_another_tenants_static_agent_key_is_not_reachable(monkeypatch):
    monkeypatch.setenv("OTHER_AGENT_KEY", "sk-other")
    other_agent = _agent(url="https://other-agent.example/v1", api_key_env="OTHER_AGENT_KEY")
    operator = _cfg(agents=[], tenants={"OTHER": {"orchestration": {"agents": [other_agent]}}})
    ctx = _ctx(config=_cfg(agents=[other_agent]), tenant_id="ACME")  # ACME copied it verbatim
    assert await _dispatched_api_key(ctx, operator_config=operator) == "no-key"


async def test_refused_key_env_is_logged_by_name_never_by_value(monkeypatch, caplog):
    monkeypatch.setenv("LLM_KEY_OPENAI", _PLATFORM_SECRET)
    ctx = _ctx(config=_cfg(agents=[_agent(id="billing-log-check")]), tenant_id="ACME")
    with caplog.at_level(logging.WARNING, logger="middleware.intent_orchestration"):
        await _dispatched_api_key(ctx, operator_config=_cfg(agents=[]))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "LLM_KEY_OPENAI" in text and _PLATFORM_SECRET not in text


# ── dispatch behaviour ────────────────────────────────────────────────────────────────
async def test_dispatch_on_intent_match():
    ctx = _ctx(config=_cfg())
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is True
    assert out.agent_id == "billing"
    assert out.agent_response["choices"][0]["message"]["content"] == "agent answer"
    assert out.llm_elapsed_ms >= 0.0
    # forwarded to the agent's URL via OpenAI-compatible transport
    _, kwargs = m.call_args
    assert kwargs["base_url"] == "https://billing.agents.example/v1"
    assert kwargs["custom_llm_provider"] == "openai"


async def test_no_match_falls_back_to_llm():
    ctx = _ctx(config=_cfg(), messages=[{"role": "user", "content": "what is the weather"}])
    with patch("litellm.acompletion", new=AsyncMock()) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    m.assert_not_called()


async def test_disabled_is_noop():
    ctx = _ctx(config=_cfg(enabled=False))
    out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False


async def test_no_agents_is_noop():
    ctx = _ctx(config=_cfg(agents=[]))
    out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False


@pytest.mark.parametrize("flag", ["bypassed", "cache_hit", "security_blocked"])
async def test_short_circuit_flags_prevent_dispatch(flag):
    ctx = _ctx(config=_cfg(), **{flag: True})
    with patch("litellm.acompletion", new=AsyncMock()) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    m.assert_not_called()


async def test_cascade_response_prevents_dispatch():
    ctx = _ctx(config=_cfg(), cascade_response={"already": "answered"})
    with patch("litellm.acompletion", new=AsyncMock()) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    m.assert_not_called()


async def test_tenant_isolation_agent_not_visible_to_other_tenant():
    cfg = _cfg(agents=[], tenants={"ACME": {"orchestration": {
        "enabled": True, "agents": [{"id": "acme-billing", "url": "https://a.agents.example/v1", "match": ["refund"]}]}}})
    # ACME dispatches to its own agent...
    acme = _ctx(config=cfg, tenant_id="ACME")
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))):
        out = await IntentOrchestration().process_request(acme)
    assert out.agent_dispatched is True and out.agent_id == "acme-billing"
    # ...but another tenant (no agents) does not.
    other = _ctx(config=cfg, tenant_id="OTHER")
    with patch("litellm.acompletion", new=AsyncMock()) as m:
        out2 = await IntentOrchestration().process_request(other)
    assert out2.agent_dispatched is False
    m.assert_not_called()


async def test_per_agent_max_tokens_budget_passed():
    cfg = _cfg(agents=[{"id": "billing", "url": "https://b.agents.example/v1", "match": ["refund"], "max_tokens": 256}])
    ctx = _ctx(config=cfg)
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        await IntentOrchestration().process_request(ctx)
    assert m.call_args.kwargs["max_tokens"] == 256


async def test_dispatch_error_falls_back_gracefully():
    ctx = _ctx(config=_cfg())
    with patch("litellm.acompletion", new=AsyncMock(side_effect=RuntimeError("agent down"))):
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False       # fell back, did not crash
    assert out.agent_response is None


# ── SSRF guard (validate_outbound_url) ─────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
    "http://127.0.0.1:8080/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://0.0.0.0/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata/computeMetadata/v1/",
    "http://localhost:9000/",
])
def test_validate_outbound_url_rejects_ssrf_targets(url):
    with pytest.raises(ValueError):
        validate_outbound_url(url)


@pytest.mark.parametrize("url", [
    "http://billing.internal.example.com/v1",
    "https://agent.acme.example.com:8443/v1",
    "http://8.8.8.8/v1",  # public IP literal — allowed
])
def test_validate_outbound_url_allows_normal_hosts(url):
    validate_outbound_url(url)  # must not raise


def test_validate_outbound_url_rejects_bad_scheme():
    with pytest.raises(ValueError):
        validate_outbound_url("ftp://example.com/v1")


def test_validate_outbound_url_rejects_empty_host():
    with pytest.raises(ValueError):
        validate_outbound_url("http:///no-host")


async def test_dispatch_ssrf_url_falls_back_to_llm():
    """An agent registered with a metadata/private-IP url never reaches litellm — the
    dispatch fails validation and falls back to the normal LLM, same as any other
    dispatch error."""
    cfg = _cfg(agents=[{"id": "evil", "url": "http://169.254.169.254/latest/meta-data/",
                         "match": ["refund"]}])
    ctx = _ctx(config=cfg)
    with patch("litellm.acompletion", new=AsyncMock()) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    m.assert_not_called()


# ── timeout cap ──────────────────────────────────────────────────────────────────────
async def test_dispatch_timeout_capped():
    cfg = _cfg(agents=[{"id": "billing", "url": "https://billing.agents.example/v1", "match": ["refund"],
                         "timeout_seconds": 999999}])
    ctx = _ctx(config=cfg)
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        await IntentOrchestration().process_request(ctx)
    assert m.call_args.kwargs["timeout"] == MAX_AGENT_TIMEOUT_SECONDS


# ── savings/billing attribution on dispatch ─────────────────────────────────────────────
async def test_dispatch_sets_final_tokens_sent():
    """Regression: the F2 short-circuit skips pipeline.py's own B1 token-accounting step —
    without an equivalent here, an agent response lacking a usage block would report the
    request as ~100% savings regardless of what the agent actually consumed."""
    ctx = _ctx(config=_cfg())
    assert ctx.savings.final_tokens_sent == 0  # dataclass default, pre-dispatch
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))):
        out = await IntentOrchestration().process_request(ctx)
    assert out.savings.final_tokens_sent > 0
    assert out.savings.proxy_optimised_tokens == out.savings.final_tokens_sent


async def test_dispatch_updates_routed_model():
    """Regression: routed_model must reflect the agent's own model, not whatever G06 last
    picked for the now-skipped main LLM call — otherwise billing/cost pricing (G18) and the
    x-tokenlean-routed-model header mislabel the request."""
    cfg = _cfg(agents=[{"id": "billing", "url": "https://billing.agents.example/v1", "match": ["refund"],
                         "model": "internal-billing-llm-v2"}])
    ctx = _ctx(config=cfg, model="gpt-4o-mini")
    ctx.routed_model = "gpt-4o-mini"  # simulate G06 having already routed
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))):
        out = await IntentOrchestration().process_request(ctx)
    assert out.routed_model == "internal-billing-llm-v2"
    assert out.savings.routed_model == "internal-billing-llm-v2"


async def test_dispatch_failure_does_not_corrupt_routed_model():
    """A failed dispatch must fall back to the LLM using whatever routed_model G06 already
    picked — it must not be overwritten with the (never-called) agent's model."""
    ctx = _ctx(config=_cfg(), model="gpt-4o-mini")
    ctx.routed_model = "gpt-4o-mini"
    with patch("litellm.acompletion", new=AsyncMock(side_effect=RuntimeError("agent down"))):
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    assert out.routed_model == "gpt-4o-mini"


async def test_openai_only_no_provider_specific_fields():
    """Gate 3: with the engine active, the outbound agent call carries no Anthropic/Gemini
    provider-specific fields — only the OpenAI-compatible transport."""
    ctx = _ctx(config=_cfg())
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        await IntentOrchestration().process_request(ctx)
    kwargs = m.call_args.kwargs
    blob = str(kwargs)
    for forbidden in ("cache_control", "thinking", "budget_tokens", "response_schema"):
        assert forbidden not in blob


async def test_an_agent_call_carries_none_of_the_callers_cache_markers():
    """An agent is an OpenAI-compatible URL, not a provider that caches by marker, and
    litellm hands a custom endpoint the markers as they are. The caller's prompt-cache
    markers are removed before the call."""
    mark = {"type": "ephemeral"}
    ctx = _ctx(config=_cfg(), messages=[
        {"role": "system", "content": [{"type": "text", "text": "Rules.", "cache_control": mark}]},
        {"role": "user", "content": "please process my refund", "cache_control": mark}])
    with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is True
    assert m.call_args.kwargs["messages"] == [
        {"role": "system", "content": [{"type": "text", "text": "Rules."}]},
        {"role": "user", "content": "please process my refund"}]


# ── internal targets: names, number spellings, resolution ─────────────────────────────
@pytest.mark.parametrize("url", [
    "http://metadata.google.internal./computeMetadata/v1/",    # trailing dot, same name
    "http://METADATA.GOOGLE.INTERNAL/computeMetadata/v1/",
    "http://langfuse:3000/api/public/traces",                   # a compose service
    "http://qdrant:6333/collections",
    "http://routellm:8080/route",
    "http://2852039166/latest/meta-data/",                      # 169.254.169.254, decimal
    "http://0xa9fea9fe/latest/meta-data/",                      # ... hex
    "http://0251.0376.0251.0376/latest/meta-data/",             # ... octal
    "http://127.1:8080/",                                       # short form of 127.0.0.1
    "http://[::ffff:169.254.169.254]/latest/meta-data/",        # IPv4-mapped IPv6
    "http://[::1]:8080/",
    "http://vault.internal/v1",
    "http://printer.local/",
    "http://api.localhost/",
    "http://billing.default.svc/v1",
    "http://100.64.0.1/",                                       # shared address space
    "http://224.0.0.1/",                                        # multicast
])
def test_validate_outbound_url_rejects_internal_targets(url):
    with pytest.raises(ValueError):
        validate_outbound_url(url)


@pytest.mark.parametrize("url", [
    "https://agent.example.com./v1",                            # a public name, trailing dot
    "http://billing.internal.example.com/v1",
])
def test_validate_outbound_url_still_allows_public_names(url):
    validate_outbound_url(url)


@pytest.mark.parametrize("url", [
    "http://billing-agent:8000/v1",                             # the template's own example
    "http://vault.internal/v1",
])
def test_an_operator_agent_may_name_its_own_network(url):
    validate_outbound_url(url, internal_names_ok=True)


@pytest.mark.parametrize("url", [
    "http://metadata.google.internal./computeMetadata/v1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://2852039166/latest/meta-data/",
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
    "http://localhost:8000/v1",
    "http://api.localhost/v1",
])
def test_an_operator_agent_still_cannot_reach_metadata_or_loopback(url):
    with pytest.raises(ValueError):
        validate_outbound_url(url, internal_names_ok=True)


def _resolving_to(monkeypatch, *addresses):
    looked_up = []

    async def resolve(host):
        looked_up.append(host)
        return list(addresses)
    monkeypatch.setattr("middleware.intent_orchestration._host_addresses", resolve)
    return looked_up


@pytest.mark.parametrize("address", ["10.0.0.7", "169.254.169.254", "127.0.0.1", "fd00::1",
                                     "fe80::1%eth0"])
async def test_a_tenant_agent_whose_name_resolves_inside_is_not_called(monkeypatch, address):
    _resolving_to(monkeypatch, "93.184.216.34", address)
    ctx = _ctx(config=_cfg(agents=[{"id": "x", "url": "https://agent.attacker.example/v1",
                                    "match": ["refund"]}]))
    with patch("litellm.acompletion", new=AsyncMock()) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    m.assert_not_called()


async def test_a_tenant_agent_that_resolves_publicly_is_called(monkeypatch):
    looked_up = _resolving_to(monkeypatch, "93.184.216.34")
    ctx = _ctx(config=_cfg(agents=[{"id": "x", "url": "https://agent.example.com/v1",
                                    "match": ["refund"]}]))
    with patch("litellm.acompletion",
               new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is True and m.called
    assert looked_up == ["agent.example.com"]


async def test_an_operator_agent_on_the_operator_network_is_called(monkeypatch):
    looked_up = _resolving_to(monkeypatch, "10.0.0.7")
    agent = {"id": "billing", "url": "http://billing-agent:8000/v1", "match": ["refund"]}
    ctx = _ctx(config=_cfg(agents=[agent]))
    with patch("config_loader.get_config", return_value=_cfg(agents=[dict(agent)])), \
            patch("litellm.acompletion",
                  new=AsyncMock(return_value=_FakeResp(_openai_response()))) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is True and m.called
    assert looked_up == []                                      # the operator's own: not resolved


async def test_a_tenant_agent_borrowing_the_operator_s_internal_url_is_refused(monkeypatch):
    operator = {"id": "billing", "url": "http://billing-agent:8000/v1", "match": ["refund"]}
    tenant = {"id": "mine", "url": "http://billing-agent:8000/v1", "match": ["refund"]}
    ctx = _ctx(config=_cfg(agents=[tenant]))
    with patch("config_loader.get_config", return_value=_cfg(agents=[operator])), \
            patch("litellm.acompletion", new=AsyncMock()) as m:
        out = await IntentOrchestration().process_request(ctx)
    assert out.agent_dispatched is False
    m.assert_not_called()
