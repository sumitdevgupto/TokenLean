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
                    "complexity_tier", "bypassed"]:
            assert col in USAGE_EVENTS_DDL, f"DDL missing column: {col}"

    def test_ddl_has_create_index(self):
        assert "CREATE INDEX" in USAGE_EVENTS_DDL
