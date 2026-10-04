"""Grafana and Langfuse deploy as service accounts of their own, which Terraform creates.

Both ran as the proxy's account, which can read the proxy's provider keys and decrypt tenants'
stored keys. The deploy reads each account from Terraform's outputs, on a full deploy and on
--skip-infra, and stops with instructions when an older Terraform state has none. The deploy
commands are checked as text; the helpers run in bash against a FAKE `gcloud` and `terraform`
first on PATH, as in test_gcp_sidecar_lockdown.py.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = (_ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
TEARDOWN = (_ROOT / "scripts" / "gcp" / "teardown-gcp.sh").read_text(encoding="utf-8")
MAIN_TF = (_ROOT / "infra" / "main.tf").read_text(encoding="utf-8")


def _function(script: str, name: str) -> str:
    """The text of a (possibly indented) multi-line bash function."""
    m = re.search(rf"^([ ]*){name}\(\) \{{\n.*?^\1\}}\n", script, re.M | re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _one_liner(script: str, name: str) -> str:
    m = re.search(rf"^{name}\(\)\s*\{{.*\}}[ \t]*$", script, re.M)
    assert m, f"{name}() not found"
    return m.group(0)


def _deploy_command(service: str) -> str:
    m = re.search(rf"gcloud run deploy {service} \\\n.*?--quiet\n", DEPLOY, re.S)
    assert m, f"no deploy command for {service}"
    return m.group(0)


@pytest.mark.parametrize("service, sa_var", [("langfuse-svc", "LANGFUSE_SA"),
                                             ("grafana-svc", "GRAFANA_SA")])
def test_grafana_and_langfuse_deploy_as_their_own_accounts(service, sa_var):
    cmd = _deploy_command(service)
    assert f'--service-account="${sa_var}"' in cmd and "PROXY_SA" not in cmd


@pytest.mark.parametrize("step", ["provision_infra", "load_infra_outputs"])
def test_both_paths_read_the_accounts_from_terraform(step):
    body = _function(DEPLOY, step)
    for var, output in (("GRAFANA_SA", "grafana_service_account_email"),
                        ("LANGFUSE_SA", "langfuse_service_account_email")):
        assert f"{var}=$(terraform output -raw {output}" in body


def test_langfuse_s_account_is_granted_its_secrets_before_langfuse_deploys():
    services = _function(DEPLOY, "deploy_services")
    for secret in ("langfuse-salt", "langfuse-database-url"):
        call = f"\n  grant_langfuse_secret {secret}\n"
        assert call in services, secret
        assert services.index(call) < services.index("gcloud run deploy langfuse-svc")


def test_the_teardown_deletes_every_account_terraform_creates():
    created = set(re.findall(r'account_id\s*=\s*"([^"]+)"', MAIN_TF))
    listed = set(re.findall(r'^\s+"([a-z0-9-]+)"$',
                            re.search(r"^SERVICE_ACCOUNTS=\(\n(.*?)^\)", TEARDOWN,
                                      re.M | re.S).group(1), re.M))
    assert created and created <= listed, sorted(created - listed)


# ── The helpers, in bash, against a fake gcloud and terraform ────────────────

FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "gcloud $*" >> "$FAKE_LOG"
case "$1 $2 $3" in
  "secrets add-iam-policy-binding langfuse-salt"|"secrets add-iam-policy-binding langfuse-database-url")
    [[ "${FAKE_BIND:-ok}" == ok ]] ;;
  *) echo "fake gcloud: unhandled: $*" >&2; exit 2 ;;
esac
'''

# `terraform output -raw NAME` prints FAKE_TF_<NAME>, or fails as terraform does for an output
# the state does not have.
FAKE_TERRAFORM = r'''#!/usr/bin/env bash
echo "terraform $*" >> "$FAKE_LOG"
case "$1" in
  init) exit 0 ;;
  output)
    name="${@: -1}"; var="FAKE_TF_${name^^}"
    if [[ -n "${!var:-}" ]]; then printf '%s' "${!var}"; exit 0; fi
    echo "Error: Output \"${name}\" not found" >&2; exit 1 ;;
  *) echo "fake terraform: unhandled: $*" >&2; exit 2 ;;
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


needs_bash = pytest.mark.skipif(_bash() is None, reason="needs bash")


def _run(tmp_path, body: str, **env):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    for name, text in (("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM)):
        (fakebin / name).write_text(text, encoding="utf-8", newline="\n")
        os.chmod(fakebin / name, 0o700)
    (tmp_path / "repo" / "infra").mkdir(parents=True, exist_ok=True)
    harness = tmp_path / "harness.sh"
    harness.write_text("set -euo pipefail\nPROJECT_ID=fake-proj; REGION=asia-south1\n"
                       f'REPO_ROOT="{(tmp_path / "repo").as_posix()}"\n'
                       "RED= GREEN= YELLOW= BLUE= NC=\n" + body + '\necho "[DONE]"\n',
                       encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    r = subprocess.run([_bash(), harness.as_posix()], capture_output=True, encoding="utf-8",
                       errors="replace",
                       env={**os.environ,
                            "PATH": fakebin.as_posix() + os.pathsep + os.environ.get("PATH", ""),
                            "CLOUDSDK_CONFIG": (tmp_path / "empty-gcloud-config").as_posix(),
                            "FAKE_LOG": log.as_posix(), **env})
    return r, log.read_text(encoding="utf-8")


def _helpers(*functions: str) -> str:
    return "\n".join([_one_liner(DEPLOY, "info"), _one_liner(DEPLOY, "success"),
                      _one_liner(DEPLOY, "warn"), _one_liner(DEPLOY, "error"),
                      *(_function(DEPLOY, f) for f in functions)])


_OUTPUTS = {"FAKE_TF_ARTIFACT_REGISTRY_URL": "asia-south1-docker.pkg.dev/fake-proj/token-opt",
            "FAKE_TF_CONFIG_BUCKET_NAME": "token-opt-config-fake",
            "FAKE_TF_DB_INSTANCE_CONNECTION_NAME": "fake-proj:asia-south1:token-opt-pg",
            "FAKE_TF_PROXY_SERVICE_ACCOUNT_EMAIL": "token-opt-proxy-sa@fake-proj.iam.gserviceaccount.com"}
_ACCOUNTS = {"FAKE_TF_GRAFANA_SERVICE_ACCOUNT_EMAIL": "grafana@fake-proj.iam.gserviceaccount.com",
             "FAKE_TF_LANGFUSE_SERVICE_ACCOUNT_EMAIL": "langfuse@fake-proj.iam.gserviceaccount.com"}


@needs_bash
class TestSkipInfraReadsTheAccounts:

    def test_the_accounts_come_from_the_terraform_state(self, tmp_path):
        r, _ = _run(tmp_path, _helpers("load_infra_outputs")
                    + '\nload_infra_outputs\necho "G=$GRAFANA_SA L=$LANGFUSE_SA"',
                    **_OUTPUTS, **_ACCOUNTS)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert ("G=grafana@fake-proj.iam.gserviceaccount.com "
                "L=langfuse@fake-proj.iam.gserviceaccount.com") in r.stdout

    def test_a_state_from_before_the_accounts_stops_the_deploy_and_says_what_to_run(self, tmp_path):
        r, _ = _run(tmp_path, _helpers("load_infra_outputs") + "\nload_infra_outputs", **_OUTPUTS)
        assert r.returncode == 1 and "[DONE]" not in r.stdout
        assert "without --skip-infra" in r.stdout


@needs_bash
@pytest.mark.parametrize("secret", ["langfuse-salt", "langfuse-database-url"])
class TestTheSecretsLangfuseMounts:
    SA = "langfuse@fake-proj.iam.gserviceaccount.com"

    @pytest.mark.parametrize("least_privilege", ["true", "false", ""])
    def test_langfuse_s_account_is_always_bound_on_it(self, tmp_path, secret, least_privilege):
        r, log = _run(tmp_path, _helpers("grant_langfuse_secret") + f"\ngrant_langfuse_secret {secret}",
                      LANGFUSE_SA=self.SA, TF_VAR_least_privilege_secret_iam=least_privilege)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert (f"gcloud secrets add-iam-policy-binding {secret} "
                f"--member=serviceAccount:{self.SA} --role=roles/secretmanager.secretAccessor "
                f"--project=fake-proj") in log

    def test_a_failed_binding_is_reported_and_the_deploy_goes_on(self, tmp_path, secret):
        r, _ = _run(tmp_path, _helpers("grant_langfuse_secret") + f"\ngrant_langfuse_secret {secret}",
                    LANGFUSE_SA=self.SA, FAKE_BIND="fail")
        assert r.returncode == 0 and "[DONE]" in r.stdout
        assert f"gcloud secrets add-iam-policy-binding {secret}" in r.stdout
