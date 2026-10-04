"""The G03 jobs reach Qdrant on GCP, and their images install what they import.

On GCP, Qdrant runs on Cloud Run behind IAM plus its own API key. Both jobs (doc-pipeline,
finetune-pipeline) built `QdrantClient(url=...)` with neither, and on the client's default port
6333, which Cloud Run does not serve, so ingestion never landed. Each job now carries a copy of
the proxy's client settings (`ml_models.qdrant_client_kwargs`), held to it here, and the deploy
mounts the Qdrant key on both. doc-pipeline also imported `tiktoken` without installing it.
"""
import ast
import importlib.util
import itertools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ml_models

_ROOT = Path(__file__).resolve().parents[2]
_JOBS = {"doc-pipeline": _ROOT / "src" / "doc-pipeline",
         "finetune-pipeline": _ROOT / "src" / "finetune-pipeline"}


def _load(job: str):
    spec = importlib.util.spec_from_file_location(
        f"_{job.replace('-', '_')}_under_test", _JOBS[job] / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=sorted(_JOBS))
def job(request):
    return _load(request.param)


# ── the settings match the proxy's ────────────────────────────────────────────
URLS = ["https://token-opt-qdrant-abc123-el.a.run.app", "https://qdrant.example.com:8443",
        "http://localhost:6333", "http://qdrant-svc:6333"]


@pytest.mark.parametrize("url, api_key, on_gcp, noauth", list(itertools.product(
    URLS, ["q-key", None], [True, False], ["1", None])))
def test_the_client_settings_are_the_proxys(job, monkeypatch, url, api_key, on_gcp, noauth):
    for name, value in (("QDRANT_API_KEY", api_key), ("QDRANT_LOCAL_NOAUTH", noauth)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    for mod in (job, ml_models):
        monkeypatch.setattr(mod, "_on_gcp", lambda: on_gcp)
        monkeypatch.setattr(mod, "_gcp_identity_token", lambda audience: f"token-for:{audience}")
    ours = job._qdrant_client_kwargs(url)
    proxy = ml_models.qdrant_client_kwargs(url=url)
    our_token, proxy_token = ours.pop("auth_token_provider", None), proxy.pop("auth_token_provider", None)
    assert ours == proxy
    assert (our_token is None) == (proxy_token is None)
    if our_token is not None:
        assert our_token() == proxy_token() == f"token-for:{url}"


def test_on_gcp_the_cloud_run_url_gets_the_key_the_token_and_port_443(job, monkeypatch):
    monkeypatch.setenv("QDRANT_API_KEY", "q-key")
    monkeypatch.delenv("QDRANT_LOCAL_NOAUTH", raising=False)
    monkeypatch.setattr(job, "_on_gcp", lambda: True)
    monkeypatch.setattr(job, "_gcp_identity_token", lambda audience: f"token-for:{audience}")
    kwargs = job._qdrant_client_kwargs(URLS[0])
    assert kwargs["api_key"] == "q-key" and kwargs["port"] == 443
    assert kwargs["auth_token_provider"]() == f"token-for:{URLS[0]}"


def test_the_identity_token_is_fetched_once_and_reused(job, monkeypatch):
    import urllib.request
    calls = []

    class _Resp:
        def read(self):
            return b"id-token\n"

    def _urlopen(req, timeout):
        calls.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    assert job._gcp_identity_token("https://q.a.run.app") == "id-token"
    assert job._gcp_identity_token("https://q.a.run.app") == "id-token"
    assert len(calls) == 1 and calls[0].endswith("identity?audience=https://q.a.run.app")


# ── whether this is GCP (the proxy and both jobs) ─────────────────────────────
# One slow metadata probe cached "not GCP" for the life of the process: no identity token,
# so every IAM-protected call (Qdrant, the sidecars) was refused until a restart.
@pytest.fixture(params=["proxy", *sorted(_JOBS)])
def prober(request, monkeypatch):
    mod = ml_models if request.param == "proxy" else _load(request.param)
    monkeypatch.setattr(mod, "_gcp_metadata_available", None)
    monkeypatch.setattr(mod, "_gcp_checked_at", 0.0)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)
    return mod


def _metadata_server(monkeypatch, answers):
    """Each probe takes the next answer: True = the metadata server replied."""
    import urllib.request
    calls = []

    class _Resp:
        def read(self):
            return b"1234"

    def _urlopen(req, timeout):
        calls.append(req.full_url)
        if not answers.pop(0):
            raise OSError("metadata server unreachable")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return calls


@pytest.mark.parametrize("hint", ["K_SERVICE", "CLOUD_RUN_JOB"])
def test_cloud_run_says_so_without_a_probe(prober, monkeypatch, hint):
    calls = _metadata_server(monkeypatch, [])
    monkeypatch.setenv(hint, "token-proxy")
    assert prober._on_gcp() is True and calls == []


def test_a_failed_probe_is_tried_again_after_a_while(prober, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(prober, "_clock", lambda: now[0])
    calls = _metadata_server(monkeypatch, [False, True])
    assert prober._on_gcp() is False
    now[0] = 100.0 + prober._GCP_NO_TTL_S - 1
    assert prober._on_gcp() is False and len(calls) == 1
    now[0] = 100.0 + prober._GCP_NO_TTL_S
    assert prober._on_gcp() is True and len(calls) == 2
    now[0] += 10 * prober._GCP_NO_TTL_S
    assert prober._on_gcp() is True and len(calls) == 2      # a yes is kept


# ── the jobs use them ─────────────────────────────────────────────────────────
class _Stop(Exception):
    pass


def _capture_client(monkeypatch):
    import qdrant_client
    seen = {}

    class _Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            raise _Stop

    monkeypatch.setattr(qdrant_client, "QdrantClient", _Client)
    return seen


@pytest.mark.parametrize("name", sorted(_JOBS))
def test_each_job_connects_with_them(monkeypatch, name):
    monkeypatch.setenv("QDRANT_URL", URLS[0])
    monkeypatch.setenv("QDRANT_API_KEY", "q-key")
    mod = _load(name)
    monkeypatch.setattr(mod, "_on_gcp", lambda: False)
    seen = _capture_client(monkeypatch)
    with pytest.raises(_Stop):
        if name == "doc-pipeline":
            mod.upsert_to_qdrant(["chunk"], [[0.1]], [None], "gs://b/o")
        else:
            mod.TrainingDataBuilder(URLS[0], "support", "acme", "rag_acme").fetch_documents()
    assert (seen.get("url"), seen.get("api_key"), seen.get("port")) == (URLS[0], "q-key", 443)


# ── the deploy gives both jobs the key ────────────────────────────────────────
DEPLOY = (_ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")


def _bash():
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


def _function(script: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.M | re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _one_liner(script: str, name: str) -> str:
    m = re.search(rf"^{name}\(\)\s*\{{.*\}}[ \t]*$", script, re.M)
    assert m, f"{name}() not found"
    return m.group(0)


def _job_deploys(tmp_path, *assignments):
    """Run deploy_jobs against a fake gcloud; return its `gcloud run jobs deploy` lines."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "gcloud").write_text('#!/usr/bin/env bash\necho "gcloud $*" >> "$FAKE_LOG"\n',
                                    encoding="utf-8", newline="\n")
    os.chmod(fakebin / "gcloud", 0o700)
    body = "\n".join([_one_liner(DEPLOY, "info"), _one_liner(DEPLOY, "success"),
                      _function(DEPLOY, "deploy_jobs"),
                      "REGISTRY_URL=reg.example/repo; REGION=asia-south1; PROJECT_ID=p",
                      "PROXY_SA=proxy-sa@p.iam.gserviceaccount.com; QDRANT_URL=https://q.a.run.app",
                      "REDIS_URL=redis://10.0.0.3:6379; CONFIG_BUCKET=cfg",
                      *assignments, "deploy_jobs"])
    harness = tmp_path / "harness.sh"
    harness.write_text("set -euo pipefail\nRED= GREEN= YELLOW= BLUE= NC=\n" + body + "\n",
                       encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    r = subprocess.run([_bash(), harness.as_posix()], capture_output=True, encoding="utf-8",
                       errors="replace",
                       env={**os.environ, "PATH": fakebin.as_posix() + os.pathsep + os.environ["PATH"],
                            "FAKE_LOG": log.as_posix()})
    assert r.returncode == 0, r.stdout + r.stderr
    deploys = [c for c in log.read_text(encoding="utf-8").splitlines()
               if c.startswith("gcloud run jobs deploy ")]
    assert [d.split()[4] for d in deploys] == ["doc-pipeline-job", "finetune-pipeline-job"]
    return deploys


@pytest.mark.skipif(_bash() is None, reason="needs bash")
@pytest.mark.parametrize("flag", ["--set-secrets=QDRANT_API_KEY=qdrant-api-key:latest", ""],
                         ids=["secret-exists", "no-secret"])
def test_both_jobs_are_deployed_with_the_qdrant_key(tmp_path, flag):
    for d in _job_deploys(tmp_path, f"QDRANT_KEY_SECRET_FLAG='{flag}'"):
        assert ("--set-secrets=QDRANT_API_KEY=qdrant-api-key:latest" in d) is bool(flag)


@pytest.mark.skipif(_bash() is None, reason="needs bash")
def test_the_ingestion_job_is_given_the_tika_url_when_tika_is_deployed(tmp_path):
    doc, finetune = _job_deploys(tmp_path, "QDRANT_KEY_SECRET_FLAG=''",
                                 "TIKA_URL=https://tika-svc-abc-el.a.run.app")
    assert "TIKA_SIDECAR_URL=https://tika-svc-abc-el.a.run.app" in doc
    assert "TIKA_SIDECAR_URL" not in finetune


@pytest.mark.skipif(_bash() is None, reason="needs bash")
def test_without_tika_the_ingestion_job_gets_no_tika_url(tmp_path):
    doc, _ = _job_deploys(tmp_path, "QDRANT_KEY_SECRET_FLAG=''", "TIKA_URL=''")
    assert "TIKA_SIDECAR_URL" not in doc


def test_the_key_flag_is_set_before_the_jobs_deploy():
    main = _function(DEPLOY, "main")
    assert main.index("  deploy_services\n") < main.index("  deploy_jobs\n")
    services = _function(DEPLOY, "deploy_services")
    assert 'QDRANT_KEY_SECRET_FLAG="--set-secrets=QDRANT_API_KEY=qdrant-api-key:latest"' in services


# ── each image installs what its job imports ──────────────────────────────────
# Import name -> requirement it comes from. Anything a job imports must map here AND the
# requirement must be in that job's requirements.txt, or be listed as an exception below.
_PROVIDES = {
    "fastembed": "fastembed", "google.cloud.storage": "google-cloud-storage",
    "google.cloud.aiplatform": "google-cloud-aiplatform",
    "google.cloud.secretmanager": "google-cloud-secret-manager",
    "langchain_text_splitters": "langchain-text-splitters", "openai": "openai",
    "qdrant_client": "qdrant-client", "redis": "redis", "tiktoken": "tiktoken",
    "unstructured": "unstructured",
}
_EXCEPTIONS = {
    "doc-pipeline": {
        "guardrails": "staged into the build context (scripts/ci/stage-doc-pipeline-guardrails.sh)",
        "httpx": "a dependency of qdrant-client",
    },
    "finetune-pipeline": {},
}


def _imports(path: Path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "google.cloud":  # a namespace package: the name is the package
                out |= {f"google.cloud.{a.name}" for a in node.names}
            else:
                out.add(node.module)
    return {m for m in out if m.split(".")[0] not in sys.stdlib_module_names}


def _requirement_names(path: Path):
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].lower())
    return names


@pytest.mark.parametrize("name", sorted(_JOBS))
def test_every_import_is_installed_in_the_image(name):
    installed = _requirement_names(_JOBS[name] / "requirements.txt")
    missing = []
    for module in sorted(_imports(_JOBS[name] / "pipeline.py")):
        if any(module == e or module.startswith(e + ".") for e in _EXCEPTIONS[name]):
            continue
        provider = next((req for imp, req in _PROVIDES.items()
                         if module == imp or module.startswith(imp + ".")), None)
        if provider not in installed:
            missing.append(f"{module} ({provider or 'unmapped'})")
    assert missing == []
