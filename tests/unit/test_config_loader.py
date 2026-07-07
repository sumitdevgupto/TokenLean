"""Unit tests for config_loader.py (including A7-T params_dir merge tests)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import tempfile
import yaml
import pytest
from unittest.mock import patch


_SAMPLE_CONFIG = {
    "proxy": {"port": 4000, "log_level": "INFO"},
    "groups": {
        "G1_compression": {"enabled": True, "min_tokens_to_compress": 200},
        "G5_cache": {"enabled": True, "l1_ttl_seconds": 3600},
    },
}


def _write_temp_config(data: dict) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(data, f)
    f.flush()
    f.close()
    return f.name


class TestConfigLoader:
    def setup_method(self):
        import config_loader
        config_loader._config = {}

    def test_load_from_local_file(self):
        path = _write_temp_config(_SAMPLE_CONFIG)
        try:
            with patch.dict(os.environ, {"CONFIG_LOCAL_PATH": path, "CONFIG_GCS_BUCKET": ""}):
                import config_loader
                config_loader._config = {}
                config_loader.load_config()
                cfg = config_loader.get_config()
                assert cfg["proxy"]["port"] == 4000
        finally:
            os.unlink(path)

    def test_get_config_returns_empty_before_load(self):
        import config_loader
        config_loader._config = {}
        result = config_loader.get_config()
        assert isinstance(result, dict)

    def test_get_config_section_accessor(self):
        import config_loader
        config_loader._config = _SAMPLE_CONFIG
        # get_config() returns the full dict; access sections via dict
        section = config_loader.get_config().get("groups")
        assert "G1_compression" in section

    def test_get_config_nested_key(self):
        import config_loader
        config_loader._config = _SAMPLE_CONFIG
        # get_group_config() accesses groups sub-keys
        section = config_loader.get_group_config("G1_compression")
        assert section["enabled"] is True

    def test_get_config_missing_key_returns_none(self):
        import config_loader
        config_loader._config = _SAMPLE_CONFIG
        result = config_loader.get_config().get("nonexistent")
        assert result is None


class TestEnvVarExpansion:
    """${VAR} / ${VAR:-default} expansion — keeps tunable defaults in config.yaml + .env,
    never hardcoded in the proxy (e.g. G6_routing.judge_model = ${G06_JUDGE_MODEL:-})."""

    def test_expands_set_var(self):
        import config_loader
        with patch.dict(os.environ, {"G06_JUDGE_MODEL": "gpt-4o-mini"}):
            out = config_loader.expand_env_vars({"judge_model": "${G06_JUDGE_MODEL:-}"})
        assert out["judge_model"] == "gpt-4o-mini"

    def test_unset_var_with_inline_default(self):
        import config_loader
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("G06_JUDGE_MODEL", None)
            out = config_loader.expand_env_vars({"judge_model": "${G06_JUDGE_MODEL:-gpt-4o}"})
        assert out["judge_model"] == "gpt-4o"

    def test_unset_var_empty_default_resolves_to_empty_string(self):
        import config_loader
        os.environ.pop("G06_JUDGE_MODEL", None)
        out = config_loader.expand_env_vars({"judge_model": "${G06_JUDGE_MODEL:-}"})
        # Never leaves the literal ${...} behind — empty means "disabled / use heuristic".
        assert out["judge_model"] == ""

    def test_unset_var_no_default_resolves_to_empty_string(self):
        import config_loader
        os.environ.pop("MISSING_VAR_XYZ", None)
        out = config_loader.expand_env_vars("${MISSING_VAR_XYZ}")
        assert out == ""

    def test_embedded_substitution_in_larger_string(self):
        import config_loader
        with patch.dict(os.environ, {"CONFIG_GCS_BUCKET": "my-bucket"}):
            out = config_loader.expand_env_vars(
                "gs://${CONFIG_GCS_BUCKET}/config/tool-registry.yaml"
            )
        assert out == "gs://my-bucket/config/tool-registry.yaml"

    def test_recurses_into_nested_dicts_and_lists(self):
        import config_loader
        with patch.dict(os.environ, {"G06_JUDGE_MODEL": "gpt-4o"}):
            out = config_loader.expand_env_vars(
                {"groups": {"G6_routing": {"judge_model": "${G06_JUDGE_MODEL:-}",
                                            "tiers": ["${G06_JUDGE_MODEL:-}"]}}}
            )
        assert out["groups"]["G6_routing"]["judge_model"] == "gpt-4o"
        assert out["groups"]["G6_routing"]["tiers"] == ["gpt-4o"]

    def test_non_string_scalars_untouched(self):
        import config_loader
        out = config_loader.expand_env_vars({"judge_timeout_ms": 2000, "enabled": True, "x": 0.7})
        assert out == {"judge_timeout_ms": 2000, "enabled": True, "x": 0.7}

    def test_format_style_braces_not_expanded(self):
        import config_loader
        # str.format templates (single braces, no $) must survive untouched.
        out = config_loader.expand_env_vars({"redis_prefix_template": "t:{tenant_id}:"})
        assert out["redis_prefix_template"] == "t:{tenant_id}:"

    def test_load_config_expands_env_from_yaml_file(self):
        import config_loader
        config_loader._config = {}
        data = {"groups": {"G6_routing": {"judge_model": "${G06_JUDGE_MODEL:-}"}}}
        path = _write_temp_config(data)
        try:
            with patch.dict(os.environ, {
                "CONFIG_LOCAL_PATH": path, "CONFIG_GCS_BUCKET": "",
                "G06_JUDGE_MODEL": "gpt-4o-mini",
            }):
                config_loader._config = {}
                config_loader.load_config()
                cfg = config_loader.get_config()
            assert cfg["groups"]["G6_routing"]["judge_model"] == "gpt-4o-mini"
        finally:
            os.unlink(path)


class TestParamsDirMerge:
    """A7-T: params_dir auto-merge tests."""

    def _write_yaml(self, data: dict, suffix: str = ".yaml") -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
        yaml.dump(data, f)
        f.flush()
        f.close()
        return f.name

    def test_params_dir_merges_top_level_keys(self):
        """Files in params_dir are merged into the root config under their keys."""
        import config_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a tenancy param file
            tenancy_path = os.path.join(tmpdir, "tenancy.yaml")
            with open(tenancy_path, "w") as f:
                yaml.dump({"tenancy": {"enabled": True, "default_tier": "enterprise"}}, f)

            base = {"proxy": {"port": 4000}}
            merged = config_loader.merge_params_dir(base, tmpdir)

            assert merged["proxy"]["port"] == 4000
            assert merged["tenancy"]["enabled"] is True
            assert merged["tenancy"]["default_tier"] == "enterprise"

    def test_params_dir_missing_logs_warning_no_crash(self):
        """Non-existent params_dir logs a warning and returns config unchanged."""
        import config_loader

        base = {"proxy": {"port": 4000}}
        result = config_loader.merge_params_dir(base, "/nonexistent/path/params")
        assert result == base

    def test_params_dir_merges_multiple_files_alphabetically(self):
        """Later files (z-prefix) win on key conflicts."""
        import config_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "a_first.yaml"), "w") as f:
                yaml.dump({"shared_key": "from_a"}, f)
            with open(os.path.join(tmpdir, "z_last.yaml"), "w") as f:
                yaml.dump({"shared_key": "from_z"}, f)

            merged = config_loader.merge_params_dir({}, tmpdir)
            assert merged["shared_key"] == "from_z"

    def test_non_yaml_files_in_params_dir_are_skipped(self):
        """Non-.yaml files in params_dir do not cause errors."""
        import config_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "notes.txt"), "w") as f:
                f.write("this is not yaml")
            with open(os.path.join(tmpdir, "real.yaml"), "w") as f:
                yaml.dump({"ok": True}, f)

            merged = config_loader.merge_params_dir({}, tmpdir)
            assert merged["ok"] is True


class TestGroupDConfigKeys:
    """D8-T: Assert G23, G05 warm_patterns, and G12 o-model keys load correctly."""

    def _merge_from_yaml_str(self, yaml_content: str) -> dict:
        import config_loader
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "params.yaml")
            with open(path, "w") as f:
                f.write(yaml_content)
            return config_loader.merge_params_dir({}, tmpdir)

    def test_g23_streaming_compression_key_present(self):
        merged = self._merge_from_yaml_str(
            "G23_streaming_compression:\n  enabled: false\n  min_repeat: 3\n  ngram_size: 5\n"
        )
        assert "G23_streaming_compression" in merged
        assert merged["G23_streaming_compression"]["enabled"] is False

    def test_g23_min_repeat_loaded(self):
        merged = self._merge_from_yaml_str(
            "G23_streaming_compression:\n  enabled: true\n  min_repeat: 4\n  ngram_size: 6\n"
        )
        assert merged["G23_streaming_compression"]["min_repeat"] == 4

    def test_g05_warm_patterns_key_present(self):
        merged = self._merge_from_yaml_str(
            "G05_cache:\n  enabled: true\n  warm_patterns:\n    - 'hello world'\n    - 'checkout error'\n"
        )
        assert "G05_cache" in merged
        assert "warm_patterns" in merged["G05_cache"]
        assert "hello world" in merged["G05_cache"]["warm_patterns"]

    def test_g05_empty_warm_patterns_is_list(self):
        merged = self._merge_from_yaml_str(
            "G05_cache:\n  enabled: true\n  warm_patterns: []\n"
        )
        assert merged["G05_cache"]["warm_patterns"] == []

    def test_g12_o_model_effort_key_present(self):
        merged = self._merge_from_yaml_str(
            "G12_reasoning:\n  enabled: false\n  o_model_effort: medium\n  thinking_budget_tokens: 8000\n"
        )
        assert "G12_reasoning" in merged
        assert merged["G12_reasoning"]["o_model_effort"] == "medium"

    def test_g12_thinking_budget_tokens_key_present(self):
        merged = self._merge_from_yaml_str(
            "G12_reasoning:\n  enabled: false\n  o_model_effort: low\n  thinking_budget_tokens: 12000\n"
        )
        assert merged["G12_reasoning"]["thinking_budget_tokens"] == 12000

    def test_all_three_groups_accessible_after_merge(self):
        import config_loader
        with tempfile.TemporaryDirectory() as tmpdir:
            for filename, content in [
                ("g05.yaml", "G05_cache:\n  warm_patterns: []\n"),
                ("g12.yaml", "G12_reasoning:\n  o_model_effort: medium\n  thinking_budget_tokens: 8000\n"),
                ("g23.yaml", "G23_streaming_compression:\n  enabled: false\n  min_repeat: 3\n"),
            ]:
                with open(os.path.join(tmpdir, filename), "w") as f:
                    f.write(content)
            merged = config_loader.merge_params_dir({}, tmpdir)

        assert "G05_cache" in merged
        assert "G12_reasoning" in merged
        assert "G23_streaming_compression" in merged


class TestTenancyConfigKeys:
    """E8-T: Assert per_tenant_config_enabled and config_cache_ttl_seconds are present
    after merging the tenancy params template content."""

    def _merge_tenancy(self, extra: str = "") -> dict:
        import config_loader
        content = (
            "tenancy:\n"
            "  enabled: true\n"
            "  per_tenant_config_enabled: true\n"
            "  tenant_configs_table: tenant_configs\n"
            "  config_cache_ttl_seconds: 60\n"
            "  default_tenant_id: default\n"
            "  default_tier: free\n"
            "  redis_prefix_template: 't:{tenant_id}:'\n"
            "  qdrant_collection_template: 'rag_{tenant_id}'\n"
            + extra
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "tenancy.yaml"), "w") as f:
                f.write(content)
            return config_loader.merge_params_dir({}, tmpdir)

    def test_per_tenant_config_enabled_key_present(self):
        merged = self._merge_tenancy()
        assert "tenancy" in merged
        assert "per_tenant_config_enabled" in merged["tenancy"]

    def test_per_tenant_config_enabled_is_true_by_default(self):
        merged = self._merge_tenancy()
        assert merged["tenancy"]["per_tenant_config_enabled"] is True

    def test_config_cache_ttl_seconds_key_present(self):
        merged = self._merge_tenancy()
        assert "config_cache_ttl_seconds" in merged["tenancy"]

    def test_config_cache_ttl_seconds_default_value(self):
        merged = self._merge_tenancy()
        assert merged["tenancy"]["config_cache_ttl_seconds"] == 60

    def test_tenant_configs_table_key_present(self):
        merged = self._merge_tenancy()
        assert merged["tenancy"]["tenant_configs_table"] == "tenant_configs"

    def test_default_tier_key_present(self):
        merged = self._merge_tenancy()
        assert merged["tenancy"]["default_tier"] == "free"


class TestAuditAndPortalConfigKeys:
    """F9-T: Assert audit and portal config keys are present after params merge."""

    def _merge_yaml(self, content: str) -> dict:
        import config_loader
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "params.yaml"), "w") as f:
                f.write(content)
            return config_loader.merge_params_dir({}, tmpdir)

    def test_audit_enabled_key_present(self):
        merged = self._merge_yaml(
            "audit:\n"
            "  enabled: true\n"
            "  retention_days: 365\n"
            "  table_name: audit_events\n"
        )
        assert "audit" in merged
        assert "enabled" in merged["audit"]

    def test_audit_enabled_is_true_by_default(self):
        merged = self._merge_yaml(
            "audit:\n  enabled: true\n  retention_days: 365\n  table_name: audit_events\n"
        )
        assert merged["audit"]["enabled"] is True

    def test_audit_retention_days_key_present(self):
        merged = self._merge_yaml(
            "audit:\n  enabled: true\n  retention_days: 365\n  table_name: audit_events\n"
        )
        assert "retention_days" in merged["audit"]
        assert merged["audit"]["retention_days"] == 365

    def test_portal_enabled_key_present(self):
        merged = self._merge_yaml(
            "portal:\n"
            "  enabled: true\n"
            "  cors_origins:\n"
            "    - 'http://localhost:3000'\n"
            "  static_dir: src/portal/dist\n"
            "  prefix: /portal\n"
        )
        assert "portal" in merged
        assert "enabled" in merged["portal"]

    def test_portal_cors_origins_key_present(self):
        merged = self._merge_yaml(
            "portal:\n"
            "  enabled: true\n"
            "  cors_origins:\n"
            "    - 'http://localhost:3000'\n"
            "    - 'https://portal.your-domain.com'\n"
            "  static_dir: src/portal/dist\n"
            "  prefix: /portal\n"
        )
        assert "cors_origins" in merged["portal"]
        assert isinstance(merged["portal"]["cors_origins"], list)
        assert len(merged["portal"]["cors_origins"]) >= 1

    def test_audit_and_portal_coexist_after_merge(self):
        import config_loader
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "audit.yaml"), "w") as f:
                f.write("audit:\n  enabled: true\n  retention_days: 365\n  table_name: audit_events\n")
            with open(os.path.join(tmpdir, "portal.yaml"), "w") as f:
                f.write("portal:\n  enabled: true\n  cors_origins: []\n  static_dir: src/portal/dist\n  prefix: /portal\n")
            merged = config_loader.merge_params_dir({}, tmpdir)
        assert "audit" in merged
        assert "portal" in merged
        assert merged["audit"]["enabled"] is True
        assert merged["portal"]["enabled"] is True
