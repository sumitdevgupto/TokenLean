"""Every metric and label the Prometheus rules use is one the proxy exports.

A rule over a label its metric lacks does not fail: a matcher on it selects nothing (an
error-rate alert on ``status=~"5.."`` over a counter with no ``status`` never fires) and a
grouping on it folds every series into one (``sum by (user_id)`` recorded a single
meaningless series). infra/main.tf merges ``prometheus-alerts.yml`` and any ``*rules.yaml``
beside it into the one rules file Prometheus loads; this checks all of them.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "proxy"))

from prometheus_client import REGISTRY  # noqa: E402

import middleware.g18_observability  # noqa: E402,F401  (registers the metrics)

_INFRA = ROOT / "infra"
_SELECTOR = re.compile(r"\b(token_opt[\w:]*)\s*(\{[^}]*\})?")
_MATCHER = re.compile(r"(\w+)\s*(?:=~|!~|!=|=)")
_BY = re.compile(r"\bby\s*\(([^)]*)\)")


def _rule_files():
    return [_INFRA / "prometheus-alerts.yml", *sorted(_INFRA.glob("*rules.yaml"))]


def _rules():
    for path in _rule_files():
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            for rule in group["rules"]:
                yield path.name, rule


def _exported_labels(series):
    """Labels of an exported series (``_bucket`` adds ``le``), or None if none exports it."""
    collector = REGISTRY._names_to_collectors.get(series)
    if collector is None:
        return None
    labels = set(getattr(collector, "_labelnames", ()))
    return labels | {"le"} if series.endswith("_bucket") else labels


def _recorded():
    """Series the recording rules define, with the labels their grouping keeps."""
    return {rule["record"]: {lab.strip() for m in _BY.finditer(rule["expr"])
                             for lab in m.group(1).split(",") if lab.strip()}
            for _name, rule in _rules() if "record" in rule}


def _labels(series, recorded):
    return recorded[series] if series in recorded else _exported_labels(series)


_RULES = list(_rules())


@pytest.mark.parametrize("where,rule", _RULES,
                         ids=[r.get("alert") or r.get("record") for _w, r in _RULES])
def test_every_series_and_label_a_rule_uses_exists(where, rule):
    recorded = _recorded()
    expr = rule["expr"]
    series = [(m.group(1), m.group(2) or "") for m in _SELECTOR.finditer(expr)]
    assert series, (where, "no token_opt series in", expr)
    for name, matchers in series:
        labels = _labels(name, recorded)
        assert labels is not None, (where, f"{name} is neither exported nor recorded")
        for label in _MATCHER.findall(matchers):
            assert label in labels, (where, f"{name} has no label {label!r}")
    grouped = {lab.strip() for m in _BY.finditer(expr) for lab in m.group(1).split(",")
               if lab.strip()}
    for label in grouped:
        for name, _matchers in series:
            assert label in _labels(name, recorded), (where, f"by ({label}) over {name}")
    # An alert's annotations can only name the labels its result keeps.
    kept = grouped or set().union(*(_labels(n, recorded) for n, _m in series))
    text = " ".join(str(v) for v in (rule.get("annotations") or {}).values())
    for label in re.findall(r"\$labels\.(\w+)", text):
        assert label in kept, (where, f"annotation names {label!r}, which the result drops")


def test_main_tf_loads_every_rule_file_this_checks():
    main_tf = (_INFRA / "main.tf").read_text(encoding="utf-8")
    assert 'fileset(path.module, "*rules.yaml")' in main_tf
    assert 'yamldecode(file("${path.module}/prometheus-alerts.yml")).groups' in main_tf
    assert "secret_data = yamlencode({ groups = local.prometheus_alert_groups })" in main_tf
    template = (_INFRA / "prometheus.yml.tmpl").read_text(encoding="utf-8")
    assert "- /secrets/alerts/prometheus-alerts.yml" in template
