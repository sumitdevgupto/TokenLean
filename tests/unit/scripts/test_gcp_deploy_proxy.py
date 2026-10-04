"""A wrapper that deploys token-proxy itself must not have the base deploy roll it first.

gcp-deploy.sh used to redeploy token-proxy on the OSS image with replace-all env vars and
secrets, even when a wrapper (the commercial deploy) runs its own image with its own
settings. Production then ran without them until the wrapper's last step, or for good if a
step in between failed. With SKIP_PROXY_DEPLOY=true an existing service keeps its image and
settings and only this script's env vars and secrets are merged in. The Cloud Build deploy
step likewise refuses to replace the commercial image. Both run in bash against a FAKE
`gcloud`, as in test_gcp_sidecar_lockdown.py.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = (_ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
CLOUDBUILD = yaml.safe_load((_ROOT / "ci" / "cloudbuild.yaml").read_text(encoding="utf-8"))


def _function(script: str, name: str) -> str:
    m = re.search(rf"^([ ]*){name}\(\) \{{\n.*?^\1\}}\n", script, re.M | re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _one_liner(script: str, name: str) -> str:
    m = re.search(rf"^{name}\(\)\s*\{{.*\}}[ \t]*$", script, re.M)
    assert m, f"{name}() not found"
    return m.group(0)


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

FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "gcloud $*" >> "$FAKE_LOG"
case "$1 $2 $3" in
  "run services describe")
    [[ "${FAKE_PROXY_EXISTS:-0}" == 1 ]] || exit 1
    [[ -n "${FAKE_IMAGE:-}" ]] && echo "$FAKE_IMAGE"
    exit 0 ;;
  "run services update"|"run deploy token-proxy"|"run jobs deploy") exit 0 ;;
  "secrets versions access")
    # FAKE_SECRETS names the secrets that have a version; else FAKE_SECRET_EXISTS says for all.
    secret=""; for a in "$@"; do [[ "$a" == --secret=* ]] && secret="${a#--secret=}"; done
    if [[ -n "${FAKE_SECRETS+set}" ]]; then [[ " $FAKE_SECRETS " == *" $secret "* ]]
    else [[ "${FAKE_SECRET_EXISTS:-0}" == 1 ]]; fi ;;
  "secrets versions describe")
    [[ -n "${FAKE_TLS_VERSION:-}" ]] || exit 1
    echo "projects/123/secrets/redis-tls-server/versions/${FAKE_TLS_VERSION}" ;;
  "compute instances describe") [[ "${FAKE_REDIS_VM:-0}" == 1 ]] ;;
  "compute instances get-guest-attributes")
    # What the VM reports, per attribute: a file in FAKE_STATE_DIR that a reset replaces
    # with its after-<attribute> file, when one is given.
    path=""; for a in "$@"; do [[ "$a" == --query-path=* ]] && path="${a#--query-path=}"; done
    cat "$FAKE_STATE_DIR/${path##*/}" 2>/dev/null || true ;;
  "compute instances reset")
    for f in "$FAKE_STATE_DIR"/after-*; do
      [[ -e "$f" ]] || continue
      name="$(basename "$f")"; cp "$f" "$FAKE_STATE_DIR/${name#after-}"
    done ;;
  *) echo "fake gcloud: unhandled: $*" >&2; exit 2 ;;
esac
'''


def _run(tmp_path, body: str, **env):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    (fakebin / "gcloud").write_text(FAKE_GCLOUD, encoding="utf-8", newline="\n")
    os.chmod(fakebin / "gcloud", 0o700)
    harness = tmp_path / "harness.sh"
    harness.write_text("set -euo pipefail\nRED= GREEN= YELLOW= BLUE= NC=\n" + body
                       + '\necho "[DONE]"\n', encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    r = subprocess.run([_bash(), harness.as_posix()], capture_output=True, encoding="utf-8",
                       errors="replace",
                       env={**os.environ, "PATH": fakebin.as_posix() + os.pathsep + os.environ["PATH"],
                            "CLOUDSDK_CONFIG": (tmp_path / "no-gcloud-config").as_posix(),
                            "FAKE_LOG": log.as_posix(), **env})
    return r, [line for line in log.read_text(encoding="utf-8").splitlines() if line]


# ── gcp-deploy.sh: token-proxy ────────────────────────────────────────────────
_SETUP = """PROJECT_ID=fake-proj; REGION=asia-south1; REGISTRY_URL=reg.example/repo
PROXY_SA=proxy-sa@fake-proj.iam.gserviceaccount.com; DB_CONNECTION=fake-proj:asia-south1:db
VPC_EGRESS_FLAGS="--vpc-connector=conn"; CONFIG_BUCKET=cfg-bucket; REDIS_URL=rediss://10.0.0.3:6379/0
QDRANT_URL=https://qdrant.example; LANGFUSE_URL=https://langfuse.example; PROXY_URL=https://proxy.example
QDRANT_SNAPSHOT_BUCKET=tl-qdrant-snapshots
LANGFUSE_SECRETS_FLAG="--set-secrets=LANGFUSE_PUBLIC_KEY=langfuse-public-key:latest,LANGFUSE_SECRET_KEY=langfuse-secret-key:latest"
METRICS_SECRET_FLAG=""; AWS_SECRETS_FLAG=""
ALERT_WEBHOOK_SECRET_FLAG="--set-secrets=ALERT_WEBHOOK_TOKEN=token-opt-alert-webhook-token:latest"
QDRANT_KEY_SECRET_FLAG="--set-secrets=QDRANT_API_KEY=qdrant-api-key:latest"
REDIS_SECRET_FLAG="--set-secrets=REDIS_PASSWORD=redis-auth:latest,REDIS_CA_CERT=redis-ca:latest"
"""


def _proxy_deploy(extra: str = ""):
    return "\n".join([_one_liner(DEPLOY, "info"), _function(DEPLOY, "private_network_flags"),
                      _function(DEPLOY, "deploy_token_proxy"), _SETUP, extra, "deploy_token_proxy"])


def _flag(call: str, name: str) -> str:
    m = re.search(rf"(?:^| ){name}=(\S+)", call)
    assert m, f"{name} not in {call}"
    return m.group(1)


@needs_bash
class TestTheBaseDeployOfTokenProxy:

    def test_a_plain_deploy_is_unchanged(self, tmp_path):
        r, calls = _run(tmp_path, _proxy_deploy())
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        [deploy] = calls
        assert deploy.startswith("gcloud run deploy token-proxy --image=reg.example/repo/proxy:latest ")
        assert "--set-secrets=DB_PASSWORD=token-opt-db-password:latest" in deploy
        assert _flag(deploy, "--set-env-vars").startswith("GCP_PROJECT_ID=fake-proj,")

    def test_with_the_flag_a_running_proxy_keeps_its_image_and_settings(self, tmp_path):
        r, calls = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="1")
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert calls[0].startswith("gcloud run services describe token-proxy ")
        [update] = calls[1:]
        assert update.startswith("gcloud run services update token-proxy ")
        for absent in ("--image", "--set-env-vars", "--set-secrets", "DB_PASSWORD",
                       "--service-account", "--vpc-connector", "--add-cloudsql-instances",
                       "--allow-unauthenticated", "--memory"):
            assert absent not in update, absent
        assert ("--update-secrets=LANGFUSE_PUBLIC_KEY=langfuse-public-key:latest,"
                "LANGFUSE_SECRET_KEY=langfuse-secret-key:latest") in update
        assert "--update-secrets=QDRANT_API_KEY=qdrant-api-key:latest" in update

    def test_both_paths_send_the_same_env_vars(self, tmp_path):
        _, plain = _run(tmp_path, _proxy_deploy())
        _, merged = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="1")
        env = _flag(plain[0], "--set-env-vars")
        assert _flag(merged[-1], "--update-env-vars") == env
        assert "LANGFUSE_HOST=https://langfuse.example" in env

    def test_with_the_flag_a_first_deploy_still_creates_the_service(self, tmp_path):
        r, calls = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="0")
        assert r.returncode == 0, r.stdout + r.stderr
        assert calls[0].startswith("gcloud run services describe token-proxy ")
        assert calls[1].startswith("gcloud run deploy token-proxy --image=reg.example/repo/proxy:latest ")
        assert len(calls) == 2

    def test_the_alert_webhook_token_reaches_the_proxy_on_both_paths(self, tmp_path):
        # Alertmanager's token for /admin/alert-webhook (Terraform's token-opt-alert-webhook-token).
        _, plain = _run(tmp_path, _proxy_deploy())
        _, merged = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="1")
        assert "--set-secrets=ALERT_WEBHOOK_TOKEN=token-opt-alert-webhook-token:latest" in plain[0]
        assert "--update-secrets=ALERT_WEBHOOK_TOKEN=token-opt-alert-webhook-token:latest" in merged[-1]


def test_the_service_step_deploys_the_proxy_only_through_the_function():
    services = _function(DEPLOY, "deploy_services")
    assert "\n  deploy_token_proxy\n" in services
    assert DEPLOY.count("gcloud run deploy token-proxy") == 1


# ── ci/cloudbuild.yaml: the deploy-proxy step ─────────────────────────────────
def _deploy_step():
    [step] = [s for s in CLOUDBUILD["steps"] if s.get("id") == "deploy-proxy"]
    assert step["entrypoint"] == "bash" and step["args"][0] == "-c"
    return step["args"][1]


_SUBSTITUTIONS = {"${_REGION}": "asia-south1", "${_REPO}": "token-opt",
                  "$PROJECT_ID": "fake-proj", "$COMMIT_SHA": "abc123"}


def test_every_shell_dollar_in_the_step_is_escaped_for_cloud_build():
    """Cloud Build reads `$name` as a substitution and rejects unknown ones: shell
    variables must be written `$$name`."""
    script = _deploy_step().replace("$$", "")
    for name in _SUBSTITUTIONS:
        script = script.replace(name, "")
    assert "$" not in script


def _local(script: str) -> str:
    script = script.replace("$$", "\0")
    for name, value in _SUBSTITUTIONS.items():
        script = script.replace(name, value)
    return script.replace("\0", "$")


@needs_bash
class TestTheCloudBuildDeployStep:

    def test_a_commercial_image_is_not_replaced(self, tmp_path):
        r, calls = _run(tmp_path, _local(_deploy_step()), FAKE_PROXY_EXISTS="1",
                        FAKE_IMAGE="asia-south1-docker.pkg.dev/p/token-opt/proxy-commercial:latest")
        assert r.returncode == 1 and "[DONE]" not in r.stdout
        assert "commercial" in r.stderr
        assert not [c for c in calls if c.startswith("gcloud run deploy")]

    @pytest.mark.parametrize("exists, image", [
        ("1", "asia-south1-docker.pkg.dev/p/token-opt/proxy:old123"),
        ("0", ""),
    ], ids=["oss-image", "no-service-yet"])
    def test_otherwise_it_deploys_the_new_image(self, tmp_path, exists, image):
        r, calls = _run(tmp_path, _local(_deploy_step()), FAKE_PROXY_EXISTS=exists, FAKE_IMAGE=image)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert calls[-1] == ("gcloud run deploy token-proxy "
                             "--image=asia-south1-docker.pkg.dev/fake-proj/token-opt/proxy:abc123 "
                             "--region=asia-south1 --platform=managed --quiet")


# ── gcp-deploy.sh: the Redis password and CA ──────────────────────────────────
# Redis had no password and no TLS. The deploy mounts the password (secret redis-auth) into its
# clients as REDIS_PASSWORD, never inside the plain REDIS_URL, and the CA that signs Redis's
# certificate (secret redis-ca) as REDIS_CA_CERT, for the Redis Terraform provisioned only.
_REDIS_FLAG = "--set-secrets=REDIS_PASSWORD=redis-auth:latest"
_REDIS_TLS_FLAG = "--set-secrets=REDIS_PASSWORD=redis-auth:latest,REDIS_CA_CERT=redis-ca:latest"
_ACCESS = "gcloud secrets versions access latest --secret={} --project=fake-proj"


def _redis_flag(tmp_path, **env):
    body = "\n".join([_function(DEPLOY, "redis_secret_flag"), "PROJECT_ID=fake-proj",
                      'echo "FLAG=[$(redis_secret_flag)]"'])
    r, calls = _run(tmp_path, body, **env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.split("FLAG=[", 1)[1].split("]", 1)[0], calls


@needs_bash
class TestTheRedisPasswordReachesItsClients:

    def test_a_provisioned_redis_with_tls_mounts_its_password_and_ca(self, tmp_path):
        flag, calls = _redis_flag(tmp_path, REDIS_HOST="10.0.0.3", FAKE_SECRETS="redis-auth redis-ca")
        assert flag == _REDIS_TLS_FLAG
        assert calls == [_ACCESS.format("redis-auth"), _ACCESS.format("redis-ca")]

    def test_without_tls_only_the_password(self, tmp_path):
        flag, _ = _redis_flag(tmp_path, REDIS_HOST="10.0.0.3", FAKE_SECRETS="redis-auth")
        assert flag == _REDIS_FLAG

    def test_tls_without_a_password_yet_mounts_the_ca(self, tmp_path):
        flag, _ = _redis_flag(tmp_path, REDIS_HOST="10.0.0.3", FAKE_SECRETS="redis-ca")
        assert flag == "--set-secrets=REDIS_CA_CERT=redis-ca:latest"

    def test_neither_mounts_nothing(self, tmp_path):
        flag, _ = _redis_flag(tmp_path, REDIS_HOST="10.0.0.3", FAKE_SECRETS="")
        assert flag == ""

    def test_an_external_redis_keeps_the_credentials_in_its_url(self, tmp_path):
        flag, calls = _redis_flag(tmp_path, REDIS_HOST="", FAKE_SECRET_EXISTS="1")
        assert flag == "" and calls == []

    def test_the_url_comes_from_terraform_with_its_scheme_and_port(self):
        # rediss:// on 6378 once Memorystore has TLS: a URL built here as redis://host:6379
        # would reach neither.
        assert "redis://${REDIS_HOST}:6379" not in DEPLOY
        assert DEPLOY.count('REDIS_URL="$(terraform output -raw redis_url)"') == 2

    def test_the_proxy_gets_it_as_a_secret_on_both_paths(self, tmp_path):
        _, plain = _run(tmp_path, _proxy_deploy())
        _, merged = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="1")
        assert _REDIS_TLS_FLAG in plain[0]
        assert "--update-secrets=REDIS_PASSWORD=redis-auth:latest,REDIS_CA_CERT=redis-ca:latest" in merged[-1]
        assert "REDIS_URL=rediss://10.0.0.3:6379/0," in _flag(plain[0], "--set-env-vars")

    def test_the_finetune_job_gets_it_and_the_doc_pipeline_job_needs_none(self, tmp_path):
        r, calls = _run(tmp_path, _jobs_deploy())
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        [doc, finetune] = [c for c in calls if c.startswith("gcloud run jobs deploy")]
        assert doc.startswith("gcloud run jobs deploy doc-pipeline-job ") and "REDIS_PASSWORD" not in doc
        assert finetune.startswith("gcloud run jobs deploy finetune-pipeline-job ")
        assert _REDIS_TLS_FLAG in finetune and "--set-secrets=QDRANT_API_KEY=qdrant-api-key:latest" in finetune

    def test_both_switches_reach_terraform_on_by_default(self):
        assert '\nexport TF_VAR_redis_auth_enforced="${REDIS_AUTH_ENFORCE:-true}"\n' in DEPLOY
        assert '\nexport TF_VAR_redis_tls="${REDIS_TLS:-true}"\n' in DEPLOY
        for name in ("TF_VAR_redis_auth_enforced", "TF_VAR_redis_tls"):
            assert DEPLOY.index(f"export {name}") < DEPLOY.index("\nmain() {")


def _jobs_deploy(extra: str = ""):
    return "\n".join([_one_liner(DEPLOY, "info"), _one_liner(DEPLOY, "success"),
                      _function(DEPLOY, "private_network_flags"), _function(DEPLOY, "deploy_jobs"),
                      _SETUP, extra, "deploy_jobs"])


# ── gcp-deploy.sh: a deploy without Qdrant ────────────────────────────────────
# enable_qdrant=false (the pgvector deploy, and the commercial deploy's default) leaves
# Terraform's qdrant_service_url empty, and the deploy stopped on "QDRANT_URL is empty" after
# the sidecars and Langfuse but before token-proxy.
_NO_QDRANT = 'QDRANT_URL=""; QDRANT_SNAPSHOT_BUCKET=""'


@needs_bash
class TestWithoutQdrant:

    def test_only_a_qdrant_deploy_requires_its_url(self, tmp_path):
        check = "\n".join([_one_liner(DEPLOY, "info"), _one_liner(DEPLOY, "error"),
                           _function(DEPLOY, "check_qdrant_url"), _NO_QDRANT, "check_qdrant_url"])
        off, _ = _run(tmp_path, check, ENABLE_QDRANT="false")
        assert off.returncode == 0 and "[DONE]" in off.stdout, off.stdout + off.stderr
        on, _ = _run(tmp_path, check)
        assert on.returncode == 1 and "QDRANT_URL is empty" in on.stdout
        found, _ = _run(tmp_path, check.replace(_NO_QDRANT, "QDRANT_URL=https://qdrant.example"))
        assert found.returncode == 0 and "[DONE]" in found.stdout
        assert "\n  check_qdrant_url\n" in _function(DEPLOY, "deploy_services")

    def test_the_proxy_gets_no_qdrant_url(self, tmp_path):
        r, [deploy] = _run(tmp_path, _proxy_deploy(_NO_QDRANT))
        assert r.returncode == 0, r.stdout + r.stderr
        env = _flag(deploy, "--set-env-vars")
        assert "QDRANT_URL" not in env and env.startswith("GCP_PROJECT_ID=fake-proj,")
        assert "REDIS_URL=rediss://10.0.0.3:6379/0,GCP_REGION=asia-south1," in env

    def test_an_update_in_place_drops_a_stale_qdrant_url(self, tmp_path):
        r, calls = _run(tmp_path, _proxy_deploy(_NO_QDRANT), SKIP_PROXY_DEPLOY="true",
                        FAKE_PROXY_EXISTS="1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "QDRANT_" not in _flag(calls[-1], "--update-env-vars")
        assert "--remove-env-vars=QDRANT_URL,QDRANT_SNAPSHOT_BUCKET" in calls[-1]
        _, with_qdrant = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="1")
        assert "--remove-env-vars" not in with_qdrant[-1]

    def test_the_jobs_get_no_qdrant_url(self, tmp_path):
        r, calls = _run(tmp_path, _jobs_deploy(_NO_QDRANT))
        assert r.returncode == 0, r.stdout + r.stderr
        jobs = [c for c in calls if c.startswith("gcloud run jobs deploy")]
        assert len(jobs) == 2
        for job in jobs:
            env = _flag(job, "--set-env-vars")
            assert "QDRANT_" not in env and "GCP_PROJECT_ID=fake-proj" in env


# ── gcp-deploy.sh: Qdrant's snapshot bucket ───────────────────────────────────
# Qdrant on Cloud Run started empty after a new revision or a restart. The ingest job now
# writes each changed collection's snapshot to a bucket Qdrant restores from, and an erase in
# token-proxy deletes the tenant's snapshots: both get the bucket's name.
@needs_bash
class TestTheQdrantSnapshotBucketReachesItsUsers:

    def test_the_proxy_gets_it_on_both_paths(self, tmp_path):
        _, plain = _run(tmp_path, _proxy_deploy())
        _, merged = _run(tmp_path, _proxy_deploy(), SKIP_PROXY_DEPLOY="true", FAKE_PROXY_EXISTS="1")
        assert "QDRANT_SNAPSHOT_BUCKET=tl-qdrant-snapshots," in _flag(plain[0], "--set-env-vars")
        assert "QDRANT_SNAPSHOT_BUCKET=tl-qdrant-snapshots," in _flag(merged[-1], "--update-env-vars")

    def test_the_ingest_job_gets_it_and_the_finetune_job_needs_none(self, tmp_path):
        r, calls = _run(tmp_path, _jobs_deploy())
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        [doc, finetune] = [c for c in calls if c.startswith("gcloud run jobs deploy")]
        assert doc.startswith("gcloud run jobs deploy doc-pipeline-job ")
        assert "QDRANT_SNAPSHOT_BUCKET=tl-qdrant-snapshots," in _flag(doc, "--set-env-vars")
        assert "QDRANT_SNAPSHOT_BUCKET" not in finetune

    def test_the_name_comes_from_terraform_on_both_paths(self):
        assert DEPLOY.count("QDRANT_SNAPSHOT_BUCKET=$(terraform output -raw qdrant_snapshot_bucket") == 2


# ── gcp-deploy.sh: the private network ────────────────────────────────────────
# The Redis Terraform provisions (VM or Memorystore) has only a private address. Cloud Run got
# the private network only with private Cloud SQL, so on the open-source default the proxy could
# not reach Redis (G00's limits, the cache and sessions quietly stopped working), and the
# fine-tune job, which keeps its job records in Redis, never could.
_PRIVATE = "--network=default --subnet=default --vpc-egress=private-ranges-only"


@needs_bash
class TestTheRedisClientsReachThePrivateNetwork:

    @pytest.mark.parametrize("skip, exists", [("false", "0"), ("true", "1")],
                             ids=["deploy", "update in place"])
    def test_a_provisioned_redis_gives_the_proxy_the_private_network(self, tmp_path, skip, exists):
        r, calls = _run(tmp_path, _proxy_deploy(), REDIS_HOST="10.0.0.3", PRIVATE_SQL="false",
                        SKIP_PROXY_DEPLOY=skip, FAKE_PROXY_EXISTS=exists)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert _PRIVATE in calls[-1]

    def test_private_cloud_sql_alone_still_gives_it(self, tmp_path):
        r, calls = _run(tmp_path, _proxy_deploy(), REDIS_HOST="", PRIVATE_SQL="true")
        assert r.returncode == 0, r.stdout + r.stderr
        assert _PRIVATE in calls[-1]

    def test_with_neither_the_proxy_goes_without(self, tmp_path):
        r, calls = _run(tmp_path, _proxy_deploy(), REDIS_HOST="", PRIVATE_SQL="false")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "--vpc-egress" not in calls[-1] and "--network" not in calls[-1]

    def test_the_finetune_job_gets_it_and_the_doc_pipeline_job_goes_without(self, tmp_path):
        r, calls = _run(tmp_path, _jobs_deploy(), REDIS_HOST="10.0.0.3", PRIVATE_SQL="false")
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        [doc, finetune] = [c for c in calls if c.startswith("gcloud run jobs deploy")]
        assert _PRIVATE in finetune and "--vpc-egress" not in doc


# ── gcp-deploy.sh: the Redis VM runs what the deploy configured ───────────────
# The VM reads its password and TLS key only at boot (infra/redis-vm-startup.sh), and reports
# how Redis runs in guest attributes. A deploy whose settings differ restarts it and waits until
# it runs them, before the clients get the new URL and secrets: a client switched to TLS cannot
# talk to a Redis still serving plaintext.
_RESET = ("gcloud compute instances reset token-opt-redis-vm --zone=asia-south1-a "
          "--project=fake-proj --quiet")


def _reconcile(tmp_path, now, after=None, auth="true", tls="true", **env):
    state = tmp_path / "vm"
    state.mkdir(exist_ok=True)
    for name, value in now.items():
        (state / name).write_text(value, encoding="utf-8")
    for name, value in (after or {}).items():
        (state / f"after-{name}").write_text(value, encoding="utf-8")
    body = "\n".join([_one_liner(DEPLOY, "warn"), _one_liner(DEPLOY, "error"),
                      _one_liner(DEPLOY, "success"), _function(DEPLOY, "redis_vm_reports"),
                      _function(DEPLOY, "reconcile_redis_vm"),
                      "PROJECT_ID=fake-proj; REGION=asia-south1", "reconcile_redis_vm"])
    return _run(tmp_path, body, FAKE_STATE_DIR=state.as_posix(), REDIS_VM_WAIT_SECONDS="0",
                TF_VAR_redis_auth_enforced=auth, TF_VAR_redis_tls=tls,
                **{"FAKE_REDIS_VM": "1", "FAKE_TLS_VERSION": "7", **env})


_RUNNING = {"redis-auth": "enforced", "redis-tls": "on:7"}


@needs_bash
class TestTheRedisVmRunsWhatTheDeployConfigured:

    def test_a_vm_already_running_it_is_left_alone(self, tmp_path):
        r, calls = _reconcile(tmp_path, _RUNNING)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert _RESET not in calls

    @pytest.mark.parametrize("now", [
        {"redis-auth": "open", "redis-tls": "off"},             # before this change
        {"redis-auth": "enforced", "redis-tls": "off"},
        {"redis-auth": "enforced", "redis-tls": "on:6"},        # a rotated certificate
        {},                                                     # a VM from before the attributes
    ], ids=["open", "no tls", "old certificate", "nothing reported"])
    def test_a_vm_running_anything_else_is_restarted_until_it_does(self, tmp_path, now):
        r, calls = _reconcile(tmp_path, now, after=_RUNNING)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert calls.count(_RESET) == 1
        assert calls.index(_RESET) < len(calls) - 1     # and it waited for the new state

    def test_a_vm_that_never_comes_back_so_stops_the_deploy(self, tmp_path):
        r, calls = _reconcile(tmp_path, {"redis-auth": "open", "redis-tls": "off"},
                              after={"redis-auth": "enforced", "redis-tls": "failed"})
        assert r.returncode == 1 and "[DONE]" not in r.stdout
        assert _RESET in calls and "failed" in r.stdout

    def test_switched_off_the_vm_is_brought_back_to_plaintext(self, tmp_path):
        r, calls = _reconcile(tmp_path, _RUNNING, auth="false", tls="false",
                              after={"redis-auth": "open", "redis-tls": "off"})
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert _RESET in calls
        assert not any("redis-tls-server" in c for c in calls)   # no certificate wanted

    def test_tls_without_a_certificate_stops_the_deploy(self, tmp_path):
        r, calls = _reconcile(tmp_path, _RUNNING, FAKE_TLS_VERSION="")
        assert r.returncode == 1 and "redis-tls-server" in r.stdout
        assert _RESET not in calls

    def test_without_a_vm_there_is_nothing_to_restart(self, tmp_path):
        r, calls = _reconcile(tmp_path, {}, FAKE_REDIS_VM="0")   # Memorystore or external
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert len(calls) == 1 and calls[0].startswith("gcloud compute instances describe")

    def test_it_runs_after_terraform_right_before_the_proxy_is_deployed(self):
        # After the other services, so the running proxy is cut off from Redis only while its
        # new revision rolls out; the fine-tune job (deploy_jobs) follows.
        main = _function(DEPLOY, "main")
        assert main.index("    provision_infra\n") < main.index("\n  deploy_services\n")
        services = _function(DEPLOY, "deploy_services")
        assert services.count("\n  reconcile_redis_vm\n") == 1 and "reconcile_redis_vm" not in main
        reconcile = services.index("\n  reconcile_redis_vm\n")
        assert services.index('REDIS_SECRET_FLAG="$(redis_secret_flag)"') < reconcile
        assert reconcile < services.index("\n  deploy_token_proxy\n")
        assert "remind_redis_restart" not in DEPLOY


# ── The deploy summary names the Grafana secret; it does not print the password ──
# The summary is terminal output that ends up in scrollback and CI logs, so it shows the
# command that fetches the password instead.

def test_the_deploy_summary_does_not_print_the_grafana_password():
    assert not re.search(r"\$\(gcloud secrets versions access[^)]*grafana-admin-password", DEPLOY)
    assert "${GRAFANA_PASSWORD}" not in DEPLOY
    summary = [line for line in DEPLOY.splitlines() if "Grafana:" in line and "echo" in line]
    assert len(summary) == 1
    assert "gcloud secrets versions access latest --secret=grafana-admin-password" in summary[0]
