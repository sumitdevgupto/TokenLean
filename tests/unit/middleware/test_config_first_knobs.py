"""Item 83(a) — config-first knob resolution for G08/G02 infra knobs.

The env-derived module constants are the *fallback defaults*; a value set under
`groups.G8_tools.*` / `groups.G2_template_registry.*` in the hot-reloaded proxy
config wins (resolved via `config_loader.get_proxy_config()`). Existing
TOOL_*/MCP_*/TEMPLATE_* env deployments keep working. These are infra knobs
(cache TTLs / timeouts / pruning), so resolution is global, not per-tenant.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import config_loader
from middleware import g02_template_registry as g2
from middleware import g08_tool_loading as g8


def _set_config(monkeypatch, cfg):
    monkeypatch.setattr(config_loader, "get_proxy_config", lambda: cfg)


class TestG08ConfigFirstKnobs:
    def test_env_fallback_defaults(self, monkeypatch):
        _set_config(monkeypatch, {})  # no groups → env/default fallback
        assert g8._mcp_http_timeout() == 10.0
        assert g8._registry_cache_ttl() == 300
        assert g8._mcp_manifest_ttl() == 300
        assert g8._tool_usage_ttl_days() == 90
        assert g8._inactivity_threshold_days() == 30

    def test_config_first_overrides(self, monkeypatch):
        _set_config(monkeypatch, {"groups": {"G8_tools": {
            "mcp_http_timeout_seconds": 3.5,
            "registry_cache_ttl_seconds": 999,
            "mcp_manifest_cache_ttl_seconds": 55,
            "tool_usage_ttl_days": 7,
            "pruning": {"inactivity_threshold_days": 5},
        }}})
        assert g8._mcp_http_timeout() == 3.5
        assert g8._registry_cache_ttl() == 999
        assert g8._mcp_manifest_ttl() == 55
        assert g8._tool_usage_ttl_days() == 7
        assert g8._inactivity_threshold_days() == 5

    def test_resolver_never_raises_when_config_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("config not loaded yet")
        monkeypatch.setattr(config_loader, "get_proxy_config", _boom)
        # Must fall back to env defaults, never propagate the error.
        assert g8._mcp_http_timeout() == 10.0
        assert g8._inactivity_threshold_days() == 30


class TestG02ConfigFirstKnobs:
    def test_env_fallback_defaults(self, monkeypatch):
        _set_config(monkeypatch, {})
        assert g2._deprecation_warn_days() == 30
        assert g2._template_history_ttl_seconds() == 90 * 86400
        assert g2._template_max_history() == 1000

    def test_config_first_overrides(self, monkeypatch):
        _set_config(monkeypatch, {"groups": {"G2_template_registry": {
            "deprecation_warn_days": 14,
            "template_history_ttl_days": 30,
            "max_history_per_version": 42,
        }}})
        assert g2._deprecation_warn_days() == 14
        assert g2._template_history_ttl_seconds() == 30 * 86400
        assert g2._template_max_history() == 42

    def test_resolver_never_raises_when_config_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("config not loaded yet")
        monkeypatch.setattr(config_loader, "get_proxy_config", _boom)
        assert g2._deprecation_warn_days() == 30
        assert g2._template_max_history() == 1000
