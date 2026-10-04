"""Deploy and ops scripts keep their scratch files where only their own run can write.

Another user of a shared host can create or symlink a fixed /tmp path first. The GCP deploy
uploaded whatever was at /tmp/config.yaml as the live config (and reused a copy an earlier run
left there), the DSPy step ran /tmp/dspy_optimizer.py, issue-key read its revoke count back
from /tmp/revoke_count, and the deploy-host setup had sudo install binaries it had downloaded
to /tmp/terraform.zip and /tmp/cloud-sql-proxy.<pid>. Each now works in a mktemp file or
directory.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ["scripts/gcp/gcp-deploy.sh", "scripts/issue-key.sh", "ci/dspy-optimize.sh",
           "scripts/gcp/prepare-gcp-deploy-host.sh"]
DEPLOY = (ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")


def _bash():
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


def _code(path: str) -> str:
    """The script without its comment lines."""
    return "\n".join(line for line in (ROOT / path).read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("path", SCRIPTS)
def test_no_fixed_tmp_path_is_used(path):
    code = _code(path)
    assert "/tmp/" not in code  # nosec B108 (asserts the path is absent)
    assert "mktemp" in code


def test_every_config_step_uses_the_private_copy():
    assert 'if [[ ! -f "$CONFIG_LOCAL" ]]; then' in DEPLOY                 # --skip-infra path
    assert "path = '${CONFIG_LOCAL}'" in DEPLOY                             # sidecar URL patch
    assert DEPLOY.count('" "$CONFIG_LOCAL" 2>/dev/null') == 2               # RouteLLM model reads
    assert ('gsutil cp "$CONFIG_LOCAL" "gs://${CONFIG_BUCKET}/config/config.yaml" &>/dev/null'
            in DEPLOY)                                                      # the re-upload


def test_the_downloads_sudo_installs_come_from_a_private_directory():
    host = _code("scripts/gcp/prepare-gcp-deploy-host.sh")
    assert '_csp_dir="$(mktemp -d)"; _csp="${_csp_dir}/cloud-sql-proxy"' in host
    assert '$SUDO mv "$_csp" /usr/local/bin/cloud-sql-proxy' in host
    assert '_tf_dir="$(mktemp -d)"' in host
    assert '$SUDO mv "${_tf_dir}/terraform" /usr/local/bin/terraform' in host


@pytest.mark.skipif(_bash() is None, reason="needs bash")
def test_the_deploy_work_dir_is_private_and_gone_after_exit():
    block = re.search(r'^WORK_DIR="\$\(mktemp -d\)"\n(?:.*\n)*?^trap remove_work_dir EXIT\n',
                      DEPLOY, re.M)
    assert block, "no private work directory with an EXIT trap at the top of the script"
    probe = block.group(0) + (
        'echo "$WORK_DIR"; [[ -d "$WORK_DIR" ]] && echo present; '
        '[[ "$CONFIG_LOCAL" == "$WORK_DIR/config.yaml" ]] && echo inside; '
        'stat -c %a "$WORK_DIR"\n')
    out = subprocess.run([_bash(), "-c", probe], capture_output=True, text=True, check=True)
    work_dir, present, inside, mode = out.stdout.split()
    assert (present, inside) == ("present", "inside")
    if os.name != "nt":
        assert mode == "700"
    after = subprocess.run([_bash(), "-c", f'[[ -e "{work_dir}" ]] && echo left || echo gone'],
                           capture_output=True, text=True, check=True)
    assert after.stdout.strip() == "gone"


@pytest.mark.skipif(_bash() is None, reason="needs bash")
def test_the_dspy_step_runs_the_optimizer_from_its_own_directory():
    script = (ROOT / "ci" / "dspy-optimize.sh").read_text(encoding="utf-8")
    assert re.search(r'^WORK_DIR="\$\(mktemp -d\)"\ntrap \'rm -rf "\$WORK_DIR"\' EXIT\n'
                     r'cat > "\$WORK_DIR/dspy_optimizer\.py" << \'PYTHON_EOF\'$', script, re.M)
    assert 'python3 "$WORK_DIR/dspy_optimizer.py" \\' in script
