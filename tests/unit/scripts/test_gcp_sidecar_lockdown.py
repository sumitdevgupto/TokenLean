"""The GCP deploy keeps the LLMLingua and Tika sidecars private.

They used to deploy with --allow-unauthenticated, running as the proxy's service account:
anyone could flood them or feed Tika crafted files, as an identity that can read the
project's secrets. The deploy commands are checked as text; the helper functions of
gcp-deploy.sh and post-deploy-check.sh run in bash against a FAKE `gcloud` (and `curl`)
first on PATH, as in test_issue_key.py.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = (_ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
CHECK = (_ROOT / "scripts" / "gcp" / "post-deploy-check.sh").read_text(encoding="utf-8")


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


# ── The deploy commands ──────────────────────────────────────────────────────

@pytest.mark.parametrize("service, sa_var, sa_name", [
    ("llmlingua-svc", "LLMLINGUA_SA", "llmlingua-sidecar-sa"),
    ("tika-svc", "TIKA_SA", "tika-sidecar-sa"),
])
def test_each_sidecar_deploys_private_as_its_own_account(service, sa_var, sa_name):
    cmd = _deploy_command(service)
    assert "--no-allow-unauthenticated" in cmd
    assert "--allow-unauthenticated" not in cmd.replace("--no-allow-unauthenticated", "")
    assert f'--service-account="${sa_var}"' in cmd and "PROXY_SA" not in cmd
    # IAM keeps them private; an internal-only ingress would be unreachable from the proxy.
    assert "--ingress=all" in cmd
    assert re.search(rf'{sa_var}="{sa_name}@\$\{{PROJECT_ID\}}\.iam\.gserviceaccount\.com"\n'
                     rf'\s*ensure_sidecar_sa {sa_name} ', DEPLOY)
    after = DEPLOY[DEPLOY.index(cmd) + len(cmd):]
    assert after.lstrip().startswith(f"require_private_service {service}\n")


def test_routellm_deploys_private_and_reachable_from_the_proxy():
    # It was --ingress=internal: the proxy's calls do not travel the VPC, so Cloud Run
    # refused every one (as well as having no token). IAM keeps anonymous callers out.
    cmd = _deploy_command("routellm-svc")
    assert "--no-allow-unauthenticated" in cmd
    assert "--allow-unauthenticated" not in cmd.replace("--no-allow-unauthenticated", "")
    assert "--ingress=all" in cmd and "--ingress=internal" not in cmd
    after = DEPLOY[DEPLOY.index(cmd) + len(cmd):]
    assert after.lstrip().startswith("require_private_service routellm-svc\n")


def test_the_post_deploy_check_treats_routellm_as_a_private_sidecar():
    assert "\ncheck_private_sidecar routellm-svc /health\n" in CHECK
    assert "200|403|404|405)" not in CHECK      # the old probe called a public 200 healthy


# ── The helpers, in bash, against a fake gcloud ─────────────────────────────

FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "gcloud $*" >> "$FAKE_LOG"
case "$1 $2 $3" in
  "iam service-accounts describe") [[ "${FAKE_SA_EXISTS:-0}" == 1 ]] ;;
  "iam service-accounts create")   [[ "${FAKE_SA_CREATE:-ok}" == ok ]] ;;
  "run services remove-iam-policy-binding") exit 0 ;;  # FAKE_POLICY is what remains after it
  "run services get-iam-policy")
    [[ "$FAKE_POLICY" == unreadable ]] && { echo "ERROR: permission denied" >&2; exit 1; }
    echo "$FAKE_POLICY" ;;
  "run services describe") [[ -n "${FAKE_URL:-}" ]] && echo "$FAKE_URL" || exit 1 ;;
  "auth print-identity-token ") echo "user-token" ;;
  *) echo "fake gcloud: unhandled: $*" >&2; exit 2 ;;
esac
'''

# Prints the HTTP status for an anonymous probe, or for one carrying a bearer token.
FAKE_CURL = r'''#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
for a in "$@"; do
  [[ "$a" == "Authorization: Bearer "* ]] && { echo "$FAKE_AUTHED_STATUS"; exit 0; }
done
echo "$FAKE_ANON_STATUS"
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


def _run(tmp_path, body: str, **fake):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    for name, text in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        (fakebin / name).write_text(text, encoding="utf-8", newline="\n")
        os.chmod(fakebin / name, 0o700)
    harness = tmp_path / "harness.sh"
    harness.write_text("set -euo pipefail\nPROJECT_ID=fake-proj; REGION=asia-south1\n"
                       "RED= GREEN= YELLOW= BLUE= NC=\n" + body + '\necho "[DONE]"\n',
                       encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    env = {**os.environ,
           "PATH": fakebin.as_posix() + os.pathsep + os.environ.get("PATH", ""),
           "CLOUDSDK_CONFIG": (tmp_path / "empty-gcloud-config").as_posix(),
           "FAKE_LOG": log.as_posix(), **{f"FAKE_{k.upper()}": v for k, v in fake.items()}}
    r = subprocess.run([_bash(), harness.as_posix()], env=env, capture_output=True,
                       encoding="utf-8", errors="replace")
    return r, log.read_text(encoding="utf-8")


def _deploy_helpers(call: str) -> str:
    return "\n".join([_one_liner(DEPLOY, "error"), _one_liner(DEPLOY, "success"),
                      _function(DEPLOY, "ensure_sidecar_sa"),
                      _function(DEPLOY, "require_private_service"), call])


@needs_bash
class TestServiceAccount:

    def test_a_missing_account_is_created_and_granted_nothing(self, tmp_path):
        r, log = _run(tmp_path, _deploy_helpers('ensure_sidecar_sa tika-sidecar-sa "Tika sidecar"'),
                      sa_exists="0")
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert "gcloud iam service-accounts create tika-sidecar-sa --project=fake-proj" in log
        assert "add-iam-policy-binding" not in log

    def test_an_existing_account_is_left_alone(self, tmp_path):
        r, log = _run(tmp_path, _deploy_helpers('ensure_sidecar_sa tika-sidecar-sa "Tika sidecar"'),
                      sa_exists="1")
        assert r.returncode == 0 and "create" not in log

    def test_a_failed_create_stops_the_deploy(self, tmp_path):
        r, _ = _run(tmp_path, _deploy_helpers('ensure_sidecar_sa tika-sidecar-sa "Tika sidecar"'),
                    sa_exists="0", sa_create="fail")
        assert r.returncode == 1 and "[DONE]" not in r.stdout


@needs_bash
class TestRequirePrivateService:

    def test_a_private_service_passes_after_both_public_grants_are_removed(self, tmp_path):
        r, log = _run(tmp_path, _deploy_helpers("require_private_service tika-svc"),
                      policy="serviceAccount:token-opt-proxy-sa@fake-proj.iam.gserviceaccount.com")
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        for member in ("allUsers", "allAuthenticatedUsers"):
            assert f"remove-iam-policy-binding tika-svc --region=asia-south1 --project=fake-proj " \
                   f"--member={member} --role=roles/run.invoker" in log

    @pytest.mark.parametrize("policy", [
        "allUsers",
        "serviceAccount:token-opt-proxy-sa@fake-proj.iam.gserviceaccount.com;allUsers",
        "allAuthenticatedUsers",
    ])
    def test_a_public_grant_that_survives_stops_the_deploy(self, tmp_path, policy):
        r, _ = _run(tmp_path, _deploy_helpers("require_private_service tika-svc"), policy=policy)
        assert r.returncode == 1 and "still public" in r.stdout and "[DONE]" not in r.stdout

    def test_an_unreadable_policy_stops_the_deploy(self, tmp_path):
        r, _ = _run(tmp_path, _deploy_helpers("require_private_service tika-svc"), policy="unreadable")
        assert r.returncode == 1 and "[DONE]" not in r.stdout


def _check_helper() -> str:
    return "\n".join(["HEALTHY=0; WARNINGS=0; UNHEALTHY=0",
                      _one_liner(CHECK, "success"), _one_liner(CHECK, "warn"),
                      _one_liner(CHECK, "error"), _function(CHECK, "check_private_sidecar"),
                      "check_private_sidecar llmlingua-svc /health",
                      'echo "H=$HEALTHY W=$WARNINGS U=$UNHEALTHY"'])


@needs_bash
class TestPostDeployCheck:
    URL = "https://llmlingua-svc-abc123-el.a.run.app"

    def _check(self, tmp_path, **fake):
        r, log = _run(tmp_path, _check_helper(), url=self.URL, **fake)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout, log

    def test_a_sidecar_that_answers_anonymously_fails_the_check(self, tmp_path):
        out, _ = self._check(tmp_path, anon_status="200", authed_status="200")
        assert "H=0 W=0 U=1" in out and "answers anonymous calls" in out

    @pytest.mark.parametrize("anon", ["401", "403"])
    def test_private_and_up_passes(self, tmp_path, anon):
        out, log = self._check(tmp_path, anon_status=anon, authed_status="200")
        assert "H=1 W=0 U=0" in out
        assert f"-H Authorization: Bearer user-token {self.URL}/health" in log

    @pytest.mark.parametrize("anon, authed", [("403", "403"), ("000", "200"), ("503", "200")])
    def test_anything_else_is_a_warning(self, tmp_path, anon, authed):
        out, _ = self._check(tmp_path, anon_status=anon, authed_status=authed)
        assert "H=0 W=1 U=0" in out

    def test_a_sidecar_that_is_not_deployed_is_skipped(self, tmp_path):
        r, log = _run(tmp_path, _check_helper(), url="", anon_status="200", authed_status="200")
        assert r.returncode == 0 and "H=0 W=0 U=0" in r.stdout and "curl" not in log
