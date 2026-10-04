"""No script falls back to the maintainer's GCP project, and none acts on the placeholder.

The public .env.template set GCP_PROJECT_ID to the maintainer's project id, and the GCP
scripts prefer it over gcloud's own project, so a user who copied the template acted on that
project. The deploy-host setup used the same id as its default and set gcloud to it. The
template now leaves it empty, and the scripts refuse .env.gcp.template's placeholder rather
than pass it to gcloud. (issue-key.sh's side runs in test_issue_key.py.)
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = (ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
HOST = (ROOT / "scripts" / "gcp" / "prepare-gcp-deploy-host.sh").read_text(encoding="utf-8")
PLACEHOLDER = "your-gcp-project-id"


def _tracked(pattern: str) -> list:
    out = subprocess.run(["git", "ls-files", pattern], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [ROOT / p for p in out.split()]


def test_no_public_script_or_template_defaults_to_the_maintainers_project():
    offenders = []
    for path in _tracked("*.sh") + [ROOT / ".env.template"]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r":-token-optimisation\}|^GCP_PROJECT_ID=token-optimisation\s*$", text, re.M):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
    assert re.search(r"^GCP_PROJECT_ID=$", (ROOT / ".env.template").read_text(encoding="utf-8"),
                     re.M)


def test_the_deploy_refuses_the_placeholder_once_the_project_is_resolved():
    prereqs = DEPLOY[DEPLOY.index("check_prereqs() {"):]
    resolved = prereqs.index('PROJECT_ID=$(gcloud config get-value project 2>/dev/null)')
    check = f'[[ "$PROJECT_ID" == "{PLACEHOLDER}" ]] && \\\n    error '
    assert check in prereqs, "the deploy no longer refuses the placeholder"
    refusal = prereqs.index(check)
    assert resolved < refusal < prereqs.index('REGION="${GCP_REGION:-asia-south1}"')


def test_the_host_setup_never_sets_a_guessed_project():
    assert 'PROJECT_ID="${GCP_PROJECT_ID:-}"' in HOST
    assert f'if [[ "$PROJECT_ID" == "{PLACEHOLDER}" ]]; then' in HOST
    pinned = HOST[HOST.index("# ── Project pinned"):]
    # With no project of its own it keeps the one gcloud already has.
    assert 'if [[ -z "$PROJECT_ID" ]]; then' in pinned.split("config set project", 1)[0]


def _bash():
    import os
    import shutil
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "config get-value") if [[ -f "$FAKE_STATE" ]]; then cat "$FAKE_STATE"; else echo "${FAKE_CUR:-}"; fi ;;
  "config set") echo "$4" > "$FAKE_STATE" ;;
esac
'''


def _pin(tmp_path, project, current):
    """Run the host setup's project block against a fake gcloud."""
    import os
    import pytest
    if _bash() is None:
        pytest.skip("needs bash")
    block = HOST[HOST.index("  # ── Project pinned"):HOST.index("\nelse\n  bad \"no working gcloud")]
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(FAKE_GCLOUD, encoding="utf-8", newline="\n")
    os.chmod(gcloud, 0o700)
    script = "\n".join([
        'info() { echo "INFO $*"; }; ok() { echo "OK $*"; }; bad() { echo "BAD $*"; }',
        'warn() { echo "WARN $*"; }; FAILURES=(); fail() { FAILURES+=("$1"); }',
        f'GCLOUD="{gcloud.as_posix()}"; NO_AUTH=false; PROJECT_ID="{project}"',
        block, 'echo "PROJECT=$PROJECT_ID FAILS=${#FAILURES[@]}"'])
    env = {**os.environ, "FAKE_LOG": (tmp_path / "log").as_posix(),
           "FAKE_STATE": (tmp_path / "state").as_posix(), "FAKE_CUR": current}
    out = subprocess.run([_bash(), "-c", script], env=env, capture_output=True, text=True)
    calls = (tmp_path / "log").read_text(encoding="utf-8") if (tmp_path / "log").exists() else ""
    return out.stdout, calls


def test_with_no_project_the_host_setup_keeps_gclouds(tmp_path):
    out, calls = _pin(tmp_path, "", "users-own-project")
    assert "PROJECT=users-own-project FAILS=0" in out
    assert "config set" not in calls


def test_with_no_project_anywhere_the_host_setup_fails_and_sets_nothing(tmp_path):
    out, calls = _pin(tmp_path, "", "")
    assert "PROJECT= FAILS=1" in out and "config set" not in calls


def test_a_project_in_env_gcp_is_set(tmp_path):
    out, calls = _pin(tmp_path, "their-project", "some-other-project")
    assert "config set project their-project" in calls
    assert "PROJECT=their-project FAILS=0" in out
