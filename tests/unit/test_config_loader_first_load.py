"""The first config load must succeed or stop the process; hot reload keeps the last good one.

A fresh process whose config could not be read used to log "using last known good" and
serve on `{}` — no rate card (invoices $0), no spend cap, no provider tiers. Startup now
loads with required=True: retried, then ConfigLoadError. Hot reload keeps its old
semantics — a failed reload never raises and never replaces a good config."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import ast
from pathlib import Path

import pytest

import config_loader
from config_loader import ConfigLoadError, load_config

MAIN_PY = Path(__file__).resolve().parents[2] / "src" / "proxy" / "main.py"


@pytest.fixture(autouse=True)
def config_file(monkeypatch, tmp_path):
    """A fresh loader pointed at a local file that does not exist yet; no real sleeps."""
    monkeypatch.setattr(config_loader, "_config", {})
    monkeypatch.setattr(config_loader, "_sleep", lambda seconds: None)
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("CONFIG_LOCAL_PATH", str(path))
    return path


def test_required_first_load_that_keeps_failing_raises(config_file):
    with pytest.raises(ConfigLoadError) as err:
        load_config(required=True)
    assert str(config_file) in str(err.value)
    assert config_loader.get_config() == {}


def test_required_load_rides_out_a_transient_failure(monkeypatch, config_file):
    config_file.write_text("proxy:\n  port: 4000\n", encoding="utf-8")
    real, calls = config_loader._load_from_file, []

    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("transient read error")
        return real(path)

    monkeypatch.setattr(config_loader, "_load_from_file", flaky)
    assert load_config(required=True)["proxy"]["port"] == 4000
    assert len(calls) == 2


def test_an_empty_config_file_is_a_failed_load(config_file):
    config_file.write_text("", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(required=True)


def test_failed_hot_reload_keeps_the_last_good_config(config_file):
    config_file.write_text("proxy:\n  port: 4000\n", encoding="utf-8")
    load_config(required=True)
    config_file.unlink()
    assert load_config()["proxy"]["port"] == 4000  # reload semantics: no raise
    assert config_loader.get_config()["proxy"]["port"] == 4000


def test_successful_load_still_expands_env_placeholders(monkeypatch, config_file):
    monkeypatch.setenv("TL_TEST_PORT", "4100")
    config_file.write_text("proxy:\n  port: ${TL_TEST_PORT}\n", encoding="utf-8")
    assert load_config(required=True)["proxy"]["port"] == "4100"


# A partly written config parses to a mapping missing its later sections. A reload swapped
# it in: rate limits and spend caps went off, providers went missing, with no error.
_FULL = "proxy:\n  port: 4000\nrate_limit:\n  enabled: true\nproviders: []\n"


def _reload_failures() -> float:
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value("token_opt_config_reload_failures_total") or 0.0


def test_a_reload_that_drops_a_section_keeps_the_running_config(config_file, caplog):
    config_file.write_text(_FULL, encoding="utf-8")
    running = load_config(required=True)
    config_file.write_text("proxy:\n  port: 4001\n", encoding="utf-8")   # cut short
    before = _reload_failures()
    assert load_config() is running
    assert config_loader.get_config() == {"proxy": {"port": 4000},
                                          "rate_limit": {"enabled": True}, "providers": []}
    assert "providers, rate_limit" in caplog.text
    assert _reload_failures() == before + 1


def test_a_reload_with_every_section_is_applied(config_file):
    config_file.write_text(_FULL, encoding="utf-8")
    load_config(required=True)
    config_file.write_text(_FULL.replace("4000", "4001") + "groups: {}\n", encoding="utf-8")
    before = _reload_failures()
    assert load_config()["proxy"]["port"] == 4001
    assert "groups" in config_loader.get_config()
    assert _reload_failures() == before


def test_startup_takes_whatever_mapping_it_reads(config_file, monkeypatch):
    # The check is the reload's: startup has no running config to compare with.
    monkeypatch.setattr(config_loader, "_config", {"proxy": {}, "groups": {}})
    config_file.write_text("proxy:\n  port: 4000\n", encoding="utf-8")
    assert load_config(required=True) == {"proxy": {"port": 4000}}


def test_any_failed_reload_is_counted(config_file):
    config_file.write_text(_FULL, encoding="utf-8")
    load_config(required=True)
    config_file.unlink()
    before = _reload_failures()
    load_config()
    assert _reload_failures() == before + 1


def test_startup_loads_config_with_required_true():
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    lifespan = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    calls = [n for n in ast.walk(lifespan) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "load_config"]
    assert calls, "lifespan no longer calls load_config"
    for call in calls:
        assert any(k.arg == "required" and getattr(k.value, "value", None) is True
                   for k in call.keywords), "startup must load config with required=True"
