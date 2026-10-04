"""scripts/review_bypass_candidates.py: an approved bypass rule always says whom it applies to.

Rules are learned from benchmark runs. Written without a tenant scope they applied to every
tenant's matching traffic, so the reviewer now has to name the tenants (--tenants) or say
plainly that the rules are global (--global).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def rb():
    spec = importlib.util.spec_from_file_location(
        "review_bypass_candidates", ROOT / "scripts" / "review_bypass_candidates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CANDIDATE = {"group": "G01", "reason": "token increase", "confidence": 0.9,
             "pattern": {"token_range": {"min": 500}}, "datasets": ["DS3"],
             "models": ["gpt-4o-mini"]}


def test_a_rule_for_named_tenants_carries_them(rb):
    rule = rb.build_bypass_rule(CANDIDATE, scope={"tenants": ["acme"]})
    assert rule["conditions"].get("tenants") == ["acme"] and "global" not in rule


def test_a_global_rule_says_so(rb):
    rule = rb.build_bypass_rule(CANDIDATE, scope={"global": True})
    assert rule["global"] is True and "tenants" not in rule["conditions"]


def _report(tmp_path):
    path = tmp_path / "pattern_report.json"
    path.write_text(json.dumps({"bypass_candidates": [CANDIDATE]}), encoding="utf-8")
    return path


def test_the_review_refuses_to_run_without_a_scope(rb, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["review", "--input", str(_report(tmp_path))])
    try:
        rb.main()
    except SystemExit as exc:
        assert exc.code == 2
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"a missing scope must be a usage error, raised {exc!r}")
    else:
        pytest.fail("the review ran without a scope")


@pytest.mark.parametrize("scope_args, check", [
    (["--tenants", "acme, beta"], lambda r: r["conditions"].get("tenants") == ["acme", "beta"]),
    (["--global"], lambda r: r.get("global") is True),
])
def test_a_non_interactive_review_writes_the_scope_into_every_rule(
        rb, tmp_path, monkeypatch, scope_args, check):
    out = tmp_path / "rules.yaml"
    monkeypatch.setattr(sys, "argv", [
        "review", "--input", str(_report(tmp_path)), "--output", str(out),
        "--non-interactive", "--auto-approve", "0.5", *scope_args])
    rb.main()
    rules = yaml.safe_load(out.read_text(encoding="utf-8"))["adaptive_bypass"]["rules"]
    assert rules and all(check(r) for r in rules)


def test_an_interactive_approval_carries_the_scope(rb, monkeypatch):
    monkeypatch.setattr(rb, "display_candidate", lambda *a, **k: None)
    monkeypatch.setattr(rb, "prompt_decision", lambda: "approve")
    approved, _ = rb.run_interactive_review([CANDIDATE], scope={"tenants": ["acme"]})
    assert approved[0]["conditions"].get("tenants") == ["acme"]
