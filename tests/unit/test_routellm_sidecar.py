"""Unit tests for the RouteLLM sidecar (src/routellm-sidecar/app.py).

The sidecar must DECIDE, never complete: RouteLLM's `chat.completions.create` routes and then
calls the chosen model, a billed completion carrying the tenant's conversation. And it must
answer with the model names G06 sent, since G06 maps the answer by exact name. RouteLLM itself
is replaced by a fake that fails the test if a completion is requested.
"""
import importlib.util
import os
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
pytest.importorskip("uvicorn")
from fastapi.testclient import TestClient  # noqa: E402

_SIDECAR_APP = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "routellm-sidecar", "app.py"))


class _FakeController:
    """routellm.controller.Controller: `route()` returns one name of its model pair."""
    built = []

    def __init__(self, routers, strong_model, weak_model, **kwargs):
        self.routers, self.strong, self.weak = list(routers), strong_model, weak_model
        self.routed = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._completion))
        _FakeController.built.append(self)

    def _completion(self, **kwargs):
        raise AssertionError("the sidecar made a completion call")

    def route(self, prompt, router, threshold):
        self.routed.append((prompt, router, threshold))
        return self.strong if "prove" in prompt else self.weak


@pytest.fixture
def client(monkeypatch):
    _FakeController.built = []
    package = types.ModuleType("routellm")
    controller = types.ModuleType("routellm.controller")
    controller.Controller = _FakeController
    monkeypatch.setitem(sys.modules, "routellm", package)
    monkeypatch.setitem(sys.modules, "routellm.controller", controller)
    spec = importlib.util.spec_from_file_location("routellm_sidecar_app", _SIDECAR_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app)


def _route(client, content, router="mf", **extra):
    body = {"messages": [{"role": "system", "content": "be brief"},
                         {"role": "user", "content": content}],
            "router": router, "threshold": 0.2,
            "strong_model": "tenant-strong", "weak_model": "tenant-weak", **extra}
    return client.post("/route", json=body)


class TestRoute:
    def test_a_weak_decision_answers_with_the_weak_name_g06_sent(self, client):
        r = _route(client, "hi there")
        assert r.status_code == 200, r.text
        assert r.json()["routed_model"] == "tenant-weak"
        assert r.json()["reason"] == "below_threshold"

    def test_a_strong_decision_answers_with_the_strong_name_g06_sent(self, client):
        r = _route(client, "prove the theorem")
        assert r.status_code == 200, r.text
        assert r.json()["routed_model"] == "tenant-strong"
        assert r.json()["reason"] == "above_threshold"

    def test_the_last_turn_and_the_threshold_are_what_is_routed(self, client):
        _route(client, [{"type": "text", "text": "prove it"}, {"type": "image_url"}])
        assert _FakeController.built[0].routed == [("prove it", "mf", 0.2)]

    def test_env_names_when_the_request_names_none(self, client, monkeypatch):
        monkeypatch.setenv("ROUTELLM_WEAK_MODEL", "env-weak")
        r = client.post("/route", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["routed_model"] == "env-weak"

    def test_empty_messages_are_refused(self, client):
        assert client.post("/route", json={"messages": []}).status_code == 400


class TestDefaults:
    # bert is the default router (Apache-2.0 checkpoint); each router's default threshold is
    # RouteLLM's calibration for ~50% strong-model calls, the same table G06 uses.
    def test_a_request_naming_no_router_uses_bert_at_its_threshold(self, client):
        r = client.post("/route", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["router_used"] == "bert"
        assert _FakeController.built[0].routed == [("hi", "bert", 0.4066)]

    def test_a_named_router_without_a_threshold_gets_its_own_calibration(self, client):
        client.post("/route", json={"messages": [{"role": "user", "content": "hi"}],
                                    "router": "sw_ranking"})
        assert _FakeController.built[0].routed == [("hi", "sw_ranking", 0.21647)]


class TestControllers:
    def test_only_the_requested_router_is_built_once(self, client):
        # Building a router loads its checkpoint; causal_llm's is an 8B model.
        _route(client, "hi")
        _route(client, "hello")
        _route(client, "hi", router="sw_ranking")
        assert [c.routers for c in _FakeController.built] == [["mf"], ["sw_ranking"]]
