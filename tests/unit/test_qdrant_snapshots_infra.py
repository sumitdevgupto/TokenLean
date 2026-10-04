"""Qdrant's tenant collections survive a new revision or a restart of its Cloud Run service.

The collections live in the container's own filesystem, so a new revision (any change to the
service) or a restart started Qdrant empty and every tenant's ingested documents were gone.
Each ingest now writes the changed collection's snapshot to a private bucket, and the service
mounts that bucket read-only and restores the newest snapshot of each collection before it
serves (infra/qdrant-restore.sh). An erase deletes the tenant's snapshots, and a deleted object
is gone at once (no soft delete), so an erased tenant cannot come back on a restart.
"""
import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2] / "infra"
MAIN_TF = (INFRA / "main.tf").read_text(encoding="utf-8")
OUTPUTS_TF = (INFRA / "outputs.tf").read_text(encoding="utf-8")


def _block(text: str, head: str) -> str:
    m = re.search(rf"^{re.escape(head)} \{{\n(.*?)^\}}", text, re.M | re.S)
    assert m, f"{head} is not declared"
    return m.group(1)


def test_the_snapshot_bucket_is_private_and_a_delete_there_is_final():
    bucket = _block(MAIN_TF, 'resource "google_storage_bucket" "qdrant_snapshots"')
    assert re.search(r"count\s*=\s*var\.enable_qdrant \? 1 : 0", bucket)
    assert re.search(r"uniform_bucket_level_access\s*=\s*true", bucket)
    assert re.search(r'public_access_prevention\s*=\s*"enforced"', bucket)
    soft_delete = re.search(r"soft_delete_policy \{(.*?)\}", bucket, re.S)
    assert soft_delete and re.search(r"retention_duration_seconds\s*=\s*0", soft_delete.group(1))


def test_qdrant_reads_the_snapshots_and_the_proxy_account_writes_them():
    reader = _block(MAIN_TF, 'resource "google_storage_bucket_iam_member" "qdrant_reads_snapshots"')
    assert re.search(r'role\s*=\s*"roles/storage\.objectViewer"', reader)
    assert "google_service_account.qdrant_sa.email" in reader
    # token-proxy (an erase deletes snapshots) and doc-pipeline-job (an ingest writes one) run
    # as the proxy's account.
    writer = _block(MAIN_TF, 'resource "google_storage_bucket_iam_member" "proxy_writes_qdrant_snapshots"')
    assert re.search(r'role\s*=\s*"roles/storage\.objectUser"', writer)
    assert "google_service_account.proxy_sa.email" in writer
    for grant in (reader, writer):
        assert re.search(r"bucket\s*=\s*google_storage_bucket\.qdrant_snapshots\[0\]\.name", grant)


def test_the_service_restores_the_snapshots_before_it_serves():
    qdrant = _block(MAIN_TF, 'resource "google_cloud_run_v2_service" "qdrant"')
    assert re.search(r'execution_environment\s*=\s*"EXECUTION_ENVIRONMENT_GEN2"', qdrant)  # GCS volumes
    gcs = re.search(r"gcs \{(.*?)\}", qdrant, re.S)
    assert gcs and re.search(r"bucket\s*=\s*google_storage_bucket\.qdrant_snapshots\[0\]\.name", gcs.group(1))
    assert re.search(r"read_only\s*=\s*true", gcs.group(1))
    mount = re.search(r"volume_mounts \{(.*?)\}", qdrant, re.S)
    assert mount and re.search(r'mount_path\s*=\s*"/snapshots"', mount.group(1))
    assert re.search(r'command\s*=\s*\["bash", "-c", replace\(file\("\$\{path\.module\}/qdrant-restore\.sh"\), '
                     r'"\\r", ""\)\]', qdrant)
    # Restoring takes as long as the snapshots are big: the startup probe waits for it.
    probe = re.search(r"startup_probe \{(.*?)\n      \}", qdrant, re.S)
    assert probe and re.search(r"port\s*=\s*6333", probe.group(1))
    assert re.search(r"failure_threshold\s*=\s*60", probe.group(1))
    deps = re.search(r"depends_on = \[(.*?)\]", qdrant, re.S).group(1)
    assert "google_storage_bucket_iam_member.qdrant_reads_snapshots" in deps


def test_the_deploy_can_read_the_bucket_name():
    output = _block(OUTPUTS_TF, 'output "qdrant_snapshot_bucket"')
    assert "google_storage_bucket.qdrant_snapshots" in output
