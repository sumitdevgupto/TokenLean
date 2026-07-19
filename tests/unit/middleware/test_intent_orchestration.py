"""Unit tests for F2 Intent-Based Multi-Agent Orchestration (OSS-core engine)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from middleware import RequestContext
from middleware.intent_orchestration import (
    IntentOrchestration,
    _orchestration_cfg,
    classify_intent,
)
from savings.models import SavingsRecord


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
                               {"id": "billing", "url": "http://billing/v1",
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
    assert kwargs["base_url"] == "http://billing/v1"
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
        "enabled": True, "agents": [{"id": "acme-billing", "url": "http://a/v1", "match": ["refund"]}]}}})
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
    cfg = _cfg(agents=[{"id": "billing", "url": "http://b/v1", "match": ["refund"], "max_tokens": 256}])
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
