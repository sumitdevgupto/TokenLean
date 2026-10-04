"""A step registered with config_loader.register_post_load applies to every reloaded config.

A reload replaces the whole config dict, so anything merged into the live config afterwards
(the managed G30 ruleset) vanished at each reload until its own loop ran again. A post-load
step runs on each freshly loaded config before that config replaces the live one.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest

import config_loader
from config_loader import load_config, register_post_load

MANAGED = [["managed.x", "role_play_jailbreak", 0.9, "x"]]


@pytest.fixture(autouse=True)
def config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config_loader, "_config", {})
    monkeypatch.setattr(config_loader, "_post_load", [])
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    path = tmp_path / "config.yaml"
    path.write_text("groups:\n  G30_guardrails:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_LOCAL_PATH", str(path))
    return path


def _add_rules(config):
    config.setdefault("groups", {}).setdefault("G30_guardrails", {})["extra_rules"] = list(MANAGED)


def _rules():
    return config_loader.get_config().get("groups", {}).get("G30_guardrails", {}).get("extra_rules")


def test_a_step_applies_to_every_reload():
    register_post_load(_add_rules)
    for _ in range(3):
        load_config()
        assert _rules() == MANAGED


def test_the_new_config_goes_live_only_after_the_step():
    load_config()
    old = config_loader.get_config()
    seen = []

    def step(config):
        seen.append((config is not old, config_loader.get_config() is old))
        _add_rules(config)

    register_post_load(step)
    load_config()
    assert seen == [(True, True)], "the step works on the new config while the old one is live"
    assert _rules() == MANAGED


def test_a_failing_step_is_logged_and_the_others_still_apply(caplog):
    def broken(config):
        raise RuntimeError("feed unavailable")

    register_post_load(broken)
    register_post_load(_add_rules)
    with caplog.at_level("WARNING", logger=config_loader.logger.name):
        load_config()
    assert _rules() == MANAGED
    assert "feed unavailable" in caplog.text


def test_registering_a_step_twice_runs_it_once():
    calls = []
    register_post_load(calls.append)
    register_post_load(calls.append)
    load_config()
    assert len(calls) == 1


def test_a_failed_reload_keeps_the_last_good_config_with_the_step_applied(config_file):
    register_post_load(_add_rules)
    load_config()
    config_file.write_text("", encoding="utf-8")   # an empty file is a failed load
    load_config()
    assert _rules() == MANAGED
