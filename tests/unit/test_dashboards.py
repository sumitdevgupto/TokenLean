"""Tests for Grafana dashboard JSON validity (C10-T, C11-T)."""
import json
import os
import pytest
from pathlib import Path


DASHBOARD_DIR = Path(__file__).parent.parent.parent / "dashboard" / "dashboards"


def _load_dashboard(name: str) -> dict:
    path = DASHBOARD_DIR / name
    assert path.exists(), f"Dashboard file not found: {path}"
    with open(path) as f:
        return json.load(f)


def _all_panel_queries(dashboard: dict) -> list[str]:
    queries = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            if "expr" in target:
                queries.append(target["expr"])
            if "rawSql" in target:
                queries.append(target["rawSql"])
    return queries


def _all_dashboard_files() -> list[str]:
    return sorted(p.name for p in DASHBOARD_DIR.glob("*.json"))


# ── C10-T: billing.json validity ─────────────────────────────────────────────

class TestBillingDashboard:
    def test_billing_json_is_valid_json(self):
        _load_dashboard("billing.json")  # would raise if invalid JSON

    def test_billing_dashboard_has_title(self):
        d = _load_dashboard("billing.json")
        assert "title" in d
        assert d["title"]

    def test_billing_dashboard_has_panels(self):
        d = _load_dashboard("billing.json")
        panels = d.get("panels", [])
        assert len(panels) > 0, "billing.json has no panels"

    def test_billing_dashboard_has_templating(self):
        d = _load_dashboard("billing.json")
        assert "templating" in d
        assert "list" in d["templating"]

    def test_billing_dashboard_has_tenant_variable(self):
        d = _load_dashboard("billing.json")
        names = [v.get("name") for v in d["templating"]["list"]]
        assert "tenant" in names, f"$tenant variable missing from billing.json. Got: {names}"

    def test_billing_dashboard_prometheus_panels_filter_by_tenant(self):
        d = _load_dashboard("billing.json")
        prom_exprs = [
            t["expr"]
            for p in d.get("panels", [])
            for t in p.get("targets", [])
            if "expr" in t
        ]
        assert any('tenant_id=~"$tenant"' in e for e in prom_exprs), (
            "No Prometheus panel in billing.json filters by tenant_id"
        )

    def test_billing_dashboard_has_tokens_saved_panel(self):
        d = _load_dashboard("billing.json")
        titles = [p.get("title", "") for p in d.get("panels", [])]
        assert any("token" in t.lower() or "saving" in t.lower() for t in titles), (
            f"Expected a tokens/savings panel. Got panel titles: {titles}"
        )

    def test_billing_dashboard_has_billing_metric_panel(self):
        # Billing paradigm: the billable unit is a served 2xx request count, sourced
        # from the token_opt_http_requests_total counter (with a status filter).
        d = _load_dashboard("billing.json")
        queries = _all_panel_queries(d)
        assert any("token_opt_http_requests_total" in q for q in queries), (
            "billing.json must surface the request-count billing metric "
            "(token_opt_http_requests_total). Queries: "
            f"{queries[:3]}"
        )
        assert any('status=~"2..' in q for q in queries), (
            "billing.json must have a billable (2xx) request panel"
        )

    def test_billing_dashboard_has_real_tokens_panel(self):
        # Row 3 surfaces provider-reported tokens (real input z + real output).
        d = _load_dashboard("billing.json")
        queries = _all_panel_queries(d)
        assert any("provider_prompt_tokens" in q for q in queries), (
            "billing.json must surface real input tokens (provider_prompt_tokens)"
        )
        assert any("response_tokens" in q for q in queries), (
            "billing.json must surface real output tokens (response_tokens)"
        )

    def test_billing_dashboard_is_count_token_only_no_dollars(self):
        # Open-core guardrail: billing-as-a-product (invoicing / $) is commercial-only.
        # The OSS dashboard shows request counts + token counts, never dollar figures.
        d = _load_dashboard("billing.json")
        for panel in d.get("panels", []):
            unit = (
                panel.get("fieldConfig", {}).get("defaults", {}).get("unit", "")
            )
            assert "currency" not in unit.lower(), (
                f"billing.json panel '{panel.get('title')}' uses a currency unit "
                f"({unit!r}); dollar/invoice figures are commercial-only, not OSS."
            )
        for q in _all_panel_queries(d):
            assert "cost_saved_usd" not in q and "cost_actual" not in q, (
                f"billing.json must not query dollar cost columns. Query: {q}"
            )

    def test_billing_dashboard_schema_version_present(self):
        d = _load_dashboard("billing.json")
        assert "schemaVersion" in d
        assert isinstance(d["schemaVersion"], int)

    def test_billing_dashboard_uid_present(self):
        d = _load_dashboard("billing.json")
        assert "uid" in d
        assert d["uid"]


# ── C11-T: all dashboards have $tenant variable ───────────────────────────────

class TestAllDashboardsHaveTenantVariable:
    @pytest.mark.parametrize("fname", _all_dashboard_files())
    def test_dashboard_is_valid_json(self, fname):
        _load_dashboard(fname)

    @pytest.mark.parametrize("fname", _all_dashboard_files())
    def test_dashboard_has_tenant_variable_in_templating(self, fname):
        d = _load_dashboard(fname)
        tl = d.get("templating", {}).get("list", [])
        names = [v.get("name") for v in tl]
        assert "tenant" in names, (
            f"{fname}: $tenant variable missing from templating. Got: {names}"
        )

    @pytest.mark.parametrize("fname", _all_dashboard_files())
    def test_dashboard_has_tenant_id_in_at_least_one_query(self, fname):
        d = _load_dashboard(fname)
        queries = _all_panel_queries(d)
        assert queries, f"{fname}: no panel targets found"
        has_tenant_filter = any("tenant_id" in q for q in queries)
        assert has_tenant_filter, (
            f"{fname}: no panel query references tenant_id. "
            f"Sample queries: {queries[:2]}"
        )

    @pytest.mark.parametrize("fname", ["live.json", "billing.json"])
    def test_prometheus_dashboards_use_tenant_id_label_filter(self, fname):
        """Dashboards with Prometheus panels must use PromQL tenant filter."""
        d = _load_dashboard(fname)
        prom_exprs = [
            t["expr"]
            for p in d.get("panels", [])
            for t in p.get("targets", [])
            if "expr" in t
        ]
        assert prom_exprs, f"{fname}: expected Prometheus panels but none found"
        assert any('tenant_id=~"$tenant"' in e for e in prom_exprs), (
            f"{fname}: no Prometheus panel uses {{tenant_id=~\"$tenant\"}}. "
            f"Exprs: {prom_exprs[:3]}"
        )

    @pytest.mark.parametrize("fname", _all_dashboard_files())
    def test_dashboard_has_panels(self, fname):
        d = _load_dashboard(fname)
        assert d.get("panels"), f"{fname}: no panels found"

    @pytest.mark.parametrize("fname", _all_dashboard_files())
    def test_dashboard_has_uid(self, fname):
        d = _load_dashboard(fname)
        assert d.get("uid"), f"{fname}: uid field missing or empty"


# ── E5-T: sla.json validity ───────────────────────────────────────────────────

class TestSLADashboard:
    def test_sla_json_is_valid_json(self):
        _load_dashboard("sla.json")

    def test_sla_dashboard_has_title(self):
        d = _load_dashboard("sla.json")
        assert "title" in d and d["title"]

    def test_sla_dashboard_has_panels(self):
        d = _load_dashboard("sla.json")
        assert len(d.get("panels", [])) > 0

    def test_sla_dashboard_has_tenant_variable(self):
        d = _load_dashboard("sla.json")
        names = [v.get("name") for v in d.get("templating", {}).get("list", [])]
        assert "tenant" in names

    def test_sla_dashboard_has_latency_panel(self):
        d = _load_dashboard("sla.json")
        titles = [p.get("title", "").lower() for p in d.get("panels", [])]
        assert any("latency" in t or "p99" in t or "p95" in t for t in titles), (
            f"SLA dashboard missing latency panel. Titles: {titles}"
        )

    def test_sla_dashboard_has_error_rate_panel(self):
        d = _load_dashboard("sla.json")
        titles = [p.get("title", "").lower() for p in d.get("panels", [])]
        assert any("error" in t for t in titles), (
            f"SLA dashboard missing error rate panel. Titles: {titles}"
        )

    def test_sla_dashboard_has_uptime_panel(self):
        d = _load_dashboard("sla.json")
        titles = [p.get("title", "").lower() for p in d.get("panels", [])]
        assert any("uptime" in t for t in titles), (
            f"SLA dashboard missing uptime panel. Titles: {titles}"
        )

    def test_sla_dashboard_prometheus_panels_filter_by_tenant(self):
        d = _load_dashboard("sla.json")
        prom_exprs = [
            t["expr"]
            for p in d.get("panels", [])
            for t in p.get("targets", [])
            if "expr" in t
        ]
        assert any("tenant_id" in e for e in prom_exprs)

    def test_sla_dashboard_uid_present(self):
        d = _load_dashboard("sla.json")
        assert d.get("uid")

    def test_sla_dashboard_has_overall_and_proxy_only_rows(self):
        d = _load_dashboard("sla.json")
        row_titles = [
            p.get("title", "").lower()
            for p in d.get("panels", [])
            if p.get("type") == "row"
        ]
        assert any("overall" in t for t in row_titles), (
            f"SLA dashboard missing 'Overall (end-to-end)' row. Rows: {row_titles}"
        )
        assert any("proxy only" in t for t in row_titles), (
            f"SLA dashboard missing 'Proxy only' row. Rows: {row_titles}"
        )

    def test_sla_dashboard_proxy_row_uses_overhead_metric(self):
        # The 'Proxy only' latency cards must query the proxy-overhead histogram,
        # not the end-to-end one (p99/p95/p50 → three quantile queries).
        d = _load_dashboard("sla.json")
        queries = _all_panel_queries(d)
        overhead_queries = [q for q in queries if "token_opt_proxy_overhead_ms_bucket" in q]
        assert len(overhead_queries) >= 3, (
            f"Expected ≥3 proxy-overhead quantile queries, got: {overhead_queries}"
        )

    def test_sla_dashboard_copies_in_sync(self):
        # A duplicate sla.json lives in the internal pitch-test-plan harness; keep
        # it identical to the canonical root copy (the root dir is what the local
        # stacks actually mount — see the grafana volumes in docker-compose.yml).
        # pitch-test-plan/ is gitignored (internal-only), so it does not exist in
        # the public OSS checkout — skip there instead of breaking OSS pytest
        # (same hazard class as the gitignore-excluded G21/DS8 test).
        root = Path(__file__).parent.parent.parent
        pitch_path = root / "pitch-test-plan" / "dashboard" / "dashboards" / "sla.json"
        if not pitch_path.exists():
            pytest.skip("pitch-test-plan/ (internal, gitignored) absent in this checkout")
        main_copy = (root / "dashboard" / "dashboards" / "sla.json").read_text()
        pitch_copy = pitch_path.read_text()
        assert main_copy == pitch_copy, (
            "dashboard/dashboards/sla.json and pitch-test-plan/dashboard/dashboards/"
            "sla.json have diverged — copy the main one over the pitch-test-plan copy."
        )


# ── E6-T: tenant-overview.json validity ───────────────────────────────────────

class TestTenantOverviewDashboard:
    def test_tenant_overview_is_valid_json(self):
        _load_dashboard("tenant-overview.json")

    def test_tenant_overview_has_title(self):
        d = _load_dashboard("tenant-overview.json")
        assert "title" in d and d["title"]

    def test_tenant_overview_has_panels(self):
        d = _load_dashboard("tenant-overview.json")
        assert len(d.get("panels", [])) > 0

    def test_tenant_overview_has_tenant_variable(self):
        d = _load_dashboard("tenant-overview.json")
        names = [v.get("name") for v in d.get("templating", {}).get("list", [])]
        assert "tenant" in names

    def test_tenant_overview_has_top_tenants_panel(self):
        d = _load_dashboard("tenant-overview.json")
        titles = [p.get("title", "").lower() for p in d.get("panels", [])]
        assert any("tenant" in t for t in titles), (
            f"Tenant overview missing a 'tenants' panel. Titles: {titles}"
        )

    def test_tenant_overview_has_tier_distribution_panel(self):
        d = _load_dashboard("tenant-overview.json")
        titles = [p.get("title", "").lower() for p in d.get("panels", [])]
        assert any("tier" in t for t in titles), (
            f"Tenant overview missing a 'tier' panel. Titles: {titles}"
        )

    def test_tenant_overview_uid_present(self):
        d = _load_dashboard("tenant-overview.json")
        assert d.get("uid")
