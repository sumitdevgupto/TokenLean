"""C1-T: Tests for UsageEvent model and Postgres DDL."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from datetime import datetime, timezone

from billing.models import UsageEvent, USAGE_EVENTS_DDL


class TestUsageEvent:
    def _make_event(self, **kw) -> UsageEvent:
        defaults = dict(
            tenant_id="acme",
            request_id="req-001",
            timestamp=datetime.now(timezone.utc),
            baseline_tokens=500,
            optimised_tokens=300,
            tokens_saved=200,
            cost_saved_usd=0.004,
            groups_applied=["G01", "G05"],
            pricing_tier="enterprise",
            model="gpt-4o",
            routed_model="gpt-4o-mini",
        )
        defaults.update(kw)
        return UsageEvent(**defaults)

    def test_required_fields_present(self):
        e = self._make_event()
        assert e.tenant_id == "acme"
        assert e.request_id == "req-001"
        assert e.tokens_saved == 200
        assert e.pricing_tier == "enterprise"

    def test_to_dict_contains_all_fields(self):
        e = self._make_event()
        d = e.to_dict()
        for field in [
            "tenant_id", "request_id", "timestamp", "baseline_tokens",
            "optimised_tokens", "tokens_saved", "cost_saved_usd",
            "groups_applied", "pricing_tier", "model", "routed_model",
        ]:
            assert field in d, f"Missing field: {field}"

    def test_to_dict_serialises_timestamp_as_string(self):
        e = self._make_event()
        d = e.to_dict()
        assert isinstance(d["timestamp"], str)
        assert "T" in d["timestamp"]  # ISO 8601

    def test_groups_applied_is_list(self):
        e = self._make_event(groups_applied=["G01", "G22"])
        assert isinstance(e.groups_applied, list)
        assert "G22" in e.groups_applied

    def test_otel_trace_id_defaults_empty(self):
        e = self._make_event()
        assert e.otel_trace_id == ""

    def test_otel_trace_id_can_be_set(self):
        e = self._make_event(otel_trace_id="abcd1234" * 4)
        assert len(e.otel_trace_id) == 32

    def test_response_tokens_defaults_zero_and_settable(self):
        # Real output tokens (observability); 0 on defer / no-usage paths.
        assert self._make_event().response_tokens == 0
        assert self._make_event(response_tokens=145).response_tokens == 145

    def test_explorer_filter_fields_default_and_settable(self):
        # Requests Explorer filter columns (never billed) — safe defaults + settable.
        e = self._make_event()
        assert e.user_id == ""
        assert e.cache_hit is False
        assert e.cache_level == ""
        assert e.complexity_tier == ""
        assert e.bypassed is False
        e2 = self._make_event(
            user_id="u-42", cache_hit=True, cache_level="L2",
            complexity_tier="complex", bypassed=True,
        )
        assert e2.user_id == "u-42"
        assert e2.cache_hit is True
        assert e2.cache_level == "L2"
        assert e2.complexity_tier == "complex"
        assert e2.bypassed is True

    def test_c1_group_savings_default_and_settable(self):
        # C1 — per-G-group realised savings blob; empty dict by default.
        e = self._make_event()
        assert e.group_savings == {}
        e2 = self._make_event(group_savings={"G01": 200, "G05": 3400})
        assert e2.group_savings == {"G01": 200, "G05": 3400}
        assert e2.to_dict()["group_savings"] == {"G01": 200, "G05": 3400}

    def test_c2_reliability_fields_default_and_settable(self):
        # C2 — status_code / latency / billable observability.
        e = self._make_event()
        assert e.status_code == 0
        assert e.billable is True
        assert e.total_duration_ms == 0
        assert e.llm_duration_ms == 0
        e2 = self._make_event(status_code=502, billable=False,
                              total_duration_ms=1200, llm_duration_ms=800)
        assert e2.status_code == 502
        assert e2.billable is False
        assert e2.total_duration_ms == 1200
        assert e2.llm_duration_ms == 800


class TestUsageEventsDDL:
    def test_ddl_is_string(self):
        assert isinstance(USAGE_EVENTS_DDL, str)
        assert len(USAGE_EVENTS_DDL) > 50

    def test_ddl_contains_table_name(self):
        assert "usage_events" in USAGE_EVENTS_DDL

    def test_ddl_contains_required_columns(self):
        for col in ["tenant_id", "request_id", "tokens_saved", "cost_saved_usd",
                    "groups_applied", "pricing_tier", "otel_trace_id",
                    "response_tokens", "user_id", "cache_hit", "cache_level",
                    "complexity_tier", "bypassed",
                    # C1/C2
                    "group_savings", "status_code", "billable",
                    "total_duration_ms", "llm_duration_ms"]:
            assert col in USAGE_EVENTS_DDL, f"DDL missing column: {col}"

    def test_ddl_c1c2_columns_have_idempotent_migration(self):
        # New columns must ALSO appear in the ADD COLUMN IF NOT EXISTS block so existing
        # tables get them non-destructively.
        for col in ["group_savings", "status_code", "billable",
                    "total_duration_ms", "llm_duration_ms"]:
            assert f"ADD COLUMN IF NOT EXISTS {col}" in USAGE_EVENTS_DDL, (
                f"DDL missing idempotent migration for: {col}")

    def test_ddl_has_create_index(self):
        assert "CREATE INDEX" in USAGE_EVENTS_DDL


class TestDDLMatchesShippedMigration:
    """The app self-heals its own DDL at startup, but the in-VPC deploy runs
    ``infra/migrations/billing.sql`` instead. Nothing compared the two, which is exactly
    how ``agent_id``/``trial``/``protocol`` came to be missing from the migration file
    while the app DDL had them: a fresh migrated database would be missing columns the
    metering INSERT writes, and every usage row would fail to persist.
    """

    @staticmethod
    def _self_heal_columns(sql: str):
        import re
        return set(re.findall(r"ADD COLUMN IF NOT EXISTS\s+(\w+)", sql))

    def test_every_app_ddl_column_is_in_the_migration_file(self):
        from pathlib import Path
        migration = Path(__file__).resolve().parents[3] / "infra" / "migrations" / "billing.sql"
        assert migration.exists(), "infra/migrations/billing.sql not found"
        app_cols = self._self_heal_columns(USAGE_EVENTS_DDL)
        mig_sql = migration.read_text(encoding="utf-8")
        mig_cols = self._self_heal_columns(mig_sql)
        # A column may legitimately live in the migration's CREATE TABLE instead.
        missing = {c for c in app_cols if c not in mig_cols and c not in mig_sql}
        assert not missing, (
            f"columns self-healed by the app but absent from infra/migrations/billing.sql: "
            f"{sorted(missing)} — a migrated database would 500 the metering INSERT"
        )

    def test_cache_columns_are_nullable_not_defaulted(self):
        """None (provider said nothing) must stay distinguishable from 0 (said zero)."""
        for col in ("cache_read_tokens", "cache_write_tokens",
                    "cost_cache_read_usd", "cost_cache_write_usd"):
            line = [l for l in USAGE_EVENTS_DDL.splitlines()
                    if f"ADD COLUMN IF NOT EXISTS {col}" in l]
            assert line, f"{col} missing from the self-heal block"
            assert "DEFAULT" not in line[0].upper(), (
                f"{col} must not carry a DEFAULT — a defaulted 0 would assert 'no cache "
                f"activity' for every provider that reports nothing"
            )


class TestCacheAccountingFields:
    def _make_event(self, **kw) -> UsageEvent:
        from datetime import datetime, timezone
        defaults = dict(
            tenant_id="acme", request_id="req-cache-001",
            timestamp=datetime.now(timezone.utc),
            baseline_tokens=500, optimised_tokens=300, tokens_saved=200,
            cost_saved_usd=0.004, groups_applied=["G01"], pricing_tier="enterprise",
        )
        defaults.update(kw)
        return UsageEvent(**defaults)

    def test_cache_fields_default_to_none(self):
        e = self._make_event()
        assert e.cache_read_tokens is None
        assert e.cache_write_tokens is None
        assert e.cost_cache_read_usd is None
        assert e.cost_cache_write_usd is None

    def test_cache_fields_are_settable_and_serialised(self):
        e = self._make_event()
        e.cache_read_tokens, e.cache_write_tokens = 800, 200
        e.cost_cache_read_usd, e.cost_cache_write_usd = 0.001, 0.004
        d = e.to_dict()
        assert d["cache_read_tokens"] == 800
        assert d["cache_write_tokens"] == 200
        assert d["cost_cache_write_usd"] == 0.004


class TestMeteringInsertArity:
    """Column list, placeholders and the execute() args must move together — they live in
    three separate places in _persist_postgres and a mismatch is a runtime-only failure."""

    def test_insert_columns_placeholders_and_args_agree(self):
        import inspect, re
        from billing.metering import UsageMeter
        src = inspect.getsource(UsageMeter._persist_postgres)
        m = re.search(r"INSERT INTO usage_events\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*ON CONFLICT",
                      src, re.S)
        assert m, "could not locate the usage_events INSERT"
        flat = m.group(1).replace(chr(10), " ")
        cols = [c.strip() for c in flat.split(",") if c.strip()]
        placeholders = re.findall(r"\$\d+", m.group(2))
        assert len(cols) == len(placeholders), (
            f"{len(cols)} columns vs {len(placeholders)} placeholders")
        assert max(int(p[1:]) for p in placeholders) == len(cols)

