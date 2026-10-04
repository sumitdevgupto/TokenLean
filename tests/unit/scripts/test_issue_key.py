"""issue-key.sh must not report a revoke it did not make, or wipe the store on a bad read.

Each test runs a copy of the script in a sandbox with a FAKE `gcloud` first on PATH (and
an empty CLOUDSDK_CONFIG, so a real gcloud would have no credentials anyway). The fake's
behaviour comes from FAKE_* env vars; it logs every call and records what was written.

  * The commercial proxy validates keys against Postgres (PROXY_KEYS_BACKEND=postgres),
    which this script cannot write — so it must refuse, not print "Revoked N key(s)".
  * A failed secret read used to become "{}", so the next write kept only the new key.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "issue-key.sh"

FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "config get-value") echo "fake-test-project" ;;
  "run services")
    case "$FAKE_BACKEND" in
      postgres) echo '{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"PROXY_KEYS_BACKEND","value":"postgres"}]}]}}}}' ;;
      blob)     echo '{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"LOG_LEVEL","value":"INFO"}]}]}}}}' ;;
      *)        echo "ERROR: (gcloud.run.services.describe) Cannot find service [token-proxy]" >&2; exit 1 ;;
    esac ;;
  "secrets versions")
    case "$3" in
      access)
        case "$FAKE_ACCESS" in
          ok)       cat "$FAKE_STORE" ;;
          notfound) echo "ERROR: (gcloud.secrets.versions.access) NOT_FOUND: Secret [projects/p/secrets/s] not found or has no versions." >&2; exit 1 ;;
          *)        echo "ERROR: (gcloud.secrets.versions.access) UNAVAILABLE: The service is currently unavailable." >&2; exit 1 ;;
        esac ;;
      add) cat > "$FAKE_WRITTEN" ;;
      *) exit 2 ;;
    esac ;;
  "secrets describe") [[ "$FAKE_ACCESS" == ok ]] ;;
  "secrets create") cat > "$FAKE_WRITTEN" ;;
  *) echo "fake gcloud: unhandled: $*" >&2; exit 2 ;;
esac
'''


def _bash():
    """Git's bash on Windows (a PATH `bash` may be the WSL launcher), else PATH bash."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(
    _bash() is None or shutil.which("python3") is None or shutil.which("openssl") is None,
    reason="needs bash, python3 and openssl")


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "issue-key.sh")  # no .env beside this copy
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "gcloud").write_text(FAKE_GCLOUD, encoding="utf-8", newline="\n")
    os.chmod(fakebin / "gcloud", 0o700)
    return tmp_path


def _run(root, *args, backend="blob", access="ok", store=None):
    store_file, written_file, log_file = root / "store.json", root / "written.json", root / "gcloud.log"
    store_file.write_text(json.dumps(store or {}), encoding="utf-8")
    written_file.unlink(missing_ok=True)
    env = {
        **os.environ,
        "PATH": str(root / "fakebin") + os.pathsep + os.environ.get("PATH", ""),
        "CLOUDSDK_CONFIG": (root / "empty-gcloud-config").as_posix(),
        "FAKE_BACKEND": backend, "FAKE_ACCESS": access,
        "FAKE_STORE": store_file.as_posix(), "FAKE_WRITTEN": written_file.as_posix(),
        "FAKE_LOG": log_file.as_posix(),
    }
    r = subprocess.run([_bash(), "scripts/issue-key.sh", *args, "--project", "fake-test-project"],
                       cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace")
    assert log_file.exists(), "the fake gcloud was not the one invoked"
    written = json.loads(written_file.read_text(encoding="utf-8")) if written_file.exists() else None
    return r, written


def test_revoke_refuses_on_a_postgres_backend_and_writes_nothing(sandbox):
    r, written = _run(sandbox, "revoke", "--tenant", "ACME", backend="postgres",
                      store={"h1": {"tenant_id": "ACME", "tier": "free"}})
    assert r.returncode != 0
    assert "postgres" in (r.stdout + r.stderr).lower()
    assert written is None


def test_issue_refuses_on_a_postgres_backend_and_writes_nothing(sandbox):
    r, written = _run(sandbox, "issue", "--tenant", "ACME", backend="postgres")
    assert r.returncode != 0
    assert written is None


def test_revoke_on_the_blob_backend_removes_only_that_tenant(sandbox):
    r, written = _run(sandbox, "revoke", "--tenant", "ACME", backend="blob",
                      store={"h1": {"tenant_id": "ACME"}, "h2": {"tenant_id": "OTHER"}})
    assert r.returncode == 0, r.stdout + r.stderr
    assert written == {"h2": {"tenant_id": "OTHER"}}


def test_a_failed_read_aborts_instead_of_overwriting_the_store(sandbox):
    r, written = _run(sandbox, "issue", "--tenant", "NEW", backend="blob", access="unavailable")
    assert r.returncode != 0
    assert written is None


def test_first_issue_proceeds_when_the_secret_has_no_version_yet(sandbox):
    r, written = _run(sandbox, "issue", "--tenant", "NEW", backend="blob", access="notfound")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [meta["tenant_id"] for meta in written.values()] == ["NEW"]


def test_an_undetectable_backend_refuses_unless_told_it_is_blob(sandbox):
    r, written = _run(sandbox, "issue", "--tenant", "NEW", backend="unknown")
    assert r.returncode != 0 and "--backend blob" in (r.stdout + r.stderr)
    assert written is None
    r, written = _run(sandbox, "issue", "--tenant", "NEW", "--backend", "blob", backend="unknown")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [meta["tenant_id"] for meta in written.values()] == ["NEW"]


# The public .env.template set GCP_PROJECT_ID to the maintainer's project, and the script
# prefers it over gcloud's, so a user who copied the template acted on that project.
def _run_with_env_file(root, env_text):
    (root / ".env").write_text(env_text, encoding="utf-8", newline="\n")
    store, log = root / "store.json", root / "gcloud.log"
    store.write_text("{}", encoding="utf-8")
    env = {**os.environ, "PATH": str(root / "fakebin") + os.pathsep + os.environ.get("PATH", ""),
           "CLOUDSDK_CONFIG": (root / "empty-gcloud-config").as_posix(), "FAKE_BACKEND": "blob",
           "FAKE_ACCESS": "ok", "FAKE_STORE": store.as_posix(), "FAKE_LOG": log.as_posix(),
           "FAKE_WRITTEN": (root / "written.json").as_posix()}
    env.pop("GCP_PROJECT_ID", None)
    r = subprocess.run([_bash(), "scripts/issue-key.sh", "list"], cwd=root, env=env,
                       capture_output=True, encoding="utf-8", errors="replace")
    return r, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_the_copied_env_template_leaves_the_project_to_gcloud(sandbox):
    template = (SCRIPT.parents[1] / ".env.template").read_text(encoding="utf-8")
    r, calls = _run_with_env_file(sandbox, template)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--project=fake-test-project" in calls
    assert "token-optimisation" not in calls


def test_the_template_placeholder_is_refused_before_any_call(sandbox):
    r, calls = _run_with_env_file(sandbox, "GCP_PROJECT_ID=your-gcp-project-id\n")
    assert r.returncode != 0 and "placeholder" in r.stdout + r.stderr
    assert "--project" not in calls
