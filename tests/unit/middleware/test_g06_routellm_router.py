"""
G06's RouteLLM classifier: which router, at which threshold, reaches the sidecar.

The default router is `bert`: its checkpoint is Apache-2.0 on an MIT base model, while `mf`'s
was published with no licence and `causal_llm`'s is a Meta Llama 3 derivative. `mf` and
`sw_ranking` embed the prompt with OpenAI inside the sidecar, so they run only when an OpenAI
key is configured; without one G06 logs the switch and uses `bert` at bert's own threshold,
because a threshold calibrated for one router means nothing to another. Each default threshold
is RouteLLM's calibration for ~50% strong-model calls on its published Chatbot Arena scores
(`routellm.calibrate_threshold --strong-model-pct 0.5`; it gives mf the README's 0.11593).
"""
import logging

import pytest

import middleware.g06_routing as g06

MESSAGES = [{"role": "user", "content": "hi"}]
BASE = {"url": "http://routellm-svc:8081", "weak_model": "weak-m", "strong_model": "strong-m"}


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"routed_model": "weak-m", "confidence": 0.9}


@pytest.fixture
def sent(monkeypatch):
    """The JSON bodies G06 posts to the sidecar."""
    bodies = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None, headers=None):
            bodies.append(json)
            return _Resp()

    monkeypatch.setattr(g06.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("ml_models.cloud_run_auth_headers", lambda url: {})
    return bodies


def _openai_key(monkeypatch, present):
    monkeypatch.setattr(g06, "_openai_key_available", lambda: present)


async def _route(cfg):
    return await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, **cfg}})


@pytest.mark.parametrize("key", [True, False])   # with a key, an mf default would show
async def test_an_unset_router_is_bert_at_its_calibrated_threshold(sent, monkeypatch, key):
    _openai_key(monkeypatch, key)
    assert await _route({}) == "simple"
    assert (sent[0]["router"], sent[0]["threshold"]) == ("bert", 0.4066)


@pytest.mark.parametrize("router, threshold", [
    ("bert", 0.4066), ("mf", 0.11593), ("sw_ranking", 0.21647), ("causal_llm", 0.0962)])
async def test_a_named_router_without_a_threshold_gets_its_own_calibration(
        sent, monkeypatch, router, threshold):
    _openai_key(monkeypatch, True)
    await _route({"router": router})
    assert (sent[0]["router"], sent[0]["threshold"]) == (router, threshold)


async def test_an_operator_threshold_is_sent_as_configured(sent, monkeypatch):
    _openai_key(monkeypatch, True)
    await _route({"router": "mf", "threshold": 0.2})
    assert (sent[0]["router"], sent[0]["threshold"]) == ("mf", 0.2)


@pytest.mark.parametrize("router", ["mf", "sw_ranking"])
async def test_an_embedding_router_without_an_openai_key_falls_back_to_bert_and_logs_it(
        sent, monkeypatch, caplog, router):
    _openai_key(monkeypatch, False)
    with caplog.at_level(logging.WARNING, logger=g06.logger.name):
        await _route({"router": router, "threshold": 0.2})
    # bert at bert's calibration: the configured 0.2 was tuned for the other router.
    assert (sent[0]["router"], sent[0]["threshold"]) == ("bert", 0.4066)
    assert [r for r in caplog.records
            if r.levelno == logging.WARNING and router in r.getMessage() and "bert" in r.getMessage()]


@pytest.mark.parametrize("router", ["bert", "causal_llm"])
async def test_a_local_router_needs_no_key_and_is_used_as_configured(sent, monkeypatch, router):
    _openai_key(monkeypatch, False)
    await _route({"router": router})
    assert sent[0]["router"] == router
