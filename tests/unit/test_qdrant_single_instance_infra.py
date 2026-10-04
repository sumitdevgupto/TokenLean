"""Qdrant on GCP runs as exactly one instance, always running.

Its collections live in the container's own filesystem. Scaling to zero whenever it sat idle
erased every tenant's ingested documents, and a second instance under load held documents the
first did not. A new revision or a Cloud Run restart restores the collections from their
snapshots (test_qdrant_snapshots_infra.py).
"""
import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2] / "infra"
MAIN_TF = (INFRA / "main.tf").read_text(encoding="utf-8")


def _qdrant() -> str:
    m = re.search(r'^resource "google_cloud_run_v2_service" "qdrant" \{\n(.*?)^\}', MAIN_TF,
                  re.M | re.S)
    assert m, "the Qdrant service is not declared"
    return m.group(1)


def test_qdrant_runs_as_exactly_one_instance_that_never_scales_to_zero():
    template = re.search(r"^  template \{\n(.*?)^  \}", _qdrant(), re.M | re.S)
    assert template, "the Qdrant service has no template"
    scaling = re.search(r"^    scaling \{\n(.*?)^    \}", template.group(1), re.M | re.S)
    assert scaling, "the Qdrant template sets no scaling"
    assert re.search(r"^\s*min_instance_count\s*=\s*1\s*$", scaling.group(1), re.M)
    assert re.search(r"^\s*max_instance_count\s*=\s*1\s*$", scaling.group(1), re.M)
