"""The doc-pipeline image builds on every build path.

Its Dockerfile copies the proxy's guardrails engine from src/doc-pipeline/guardrails/, a
gitignored folder. Only ci/cloudbuild.yaml used to create it, so gcp-deploy.sh (which the
managed deploy also runs) and ci/cloudbuild-images-only.yaml stopped at that build, after
other images had been pushed. One script now stages it for every path. The bash parts run
against a FAKE docker and gcloud, as in test_gcp_deploy_proxy.py.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
STAGE = _ROOT / "scripts" / "ci" / "stage-doc-pipeline-guardrails.sh"
DEPLOY = (_ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
PUBLIC = ["__init__.py", "injection.py", "pii.py"]


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


def _staged_list() -> list:
    m = re.search(r"^FILES=\(([^)]*)\)$", STAGE.read_text(encoding="utf-8"), re.M)
    assert m, "FILES=(...) not found"
    return m.group(1).split()


def _fake_repo(tmp_path, real_engine: bool = False) -> Path:
    """A repo root with the real staging script and a proxy guardrails folder: stand-in
    files by default, or a copy of the real one."""
    root = tmp_path / "repo"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(STAGE, root / "scripts" / "ci" / STAGE.name)
    src = root / "src" / "proxy" / "guardrails"
    if real_engine:
        shutil.copytree(_ROOT / "src" / "proxy" / "guardrails", src,
                        ignore=shutil.ignore_patterns("__pycache__"))
    else:
        src.mkdir(parents=True)
        for name in PUBLIC + ["tool_policy.py", "not_for_the_image.py"]:
            (src / name).write_text(f"# {name}\n", encoding="utf-8", newline="\n")
    (root / "src" / "doc-pipeline").mkdir(parents=True)
    return root


def _stage(root: Path, *args):
    return subprocess.run([_bash(), (root / "scripts" / "ci" / STAGE.name).as_posix(), *args],
                          capture_output=True, encoding="utf-8", errors="replace")


def _staged(root: Path) -> list:
    dest = root / "src" / "doc-pipeline" / "guardrails"
    return sorted(p.name for p in dest.iterdir()) if dest.is_dir() else []


# ── the staging script ────────────────────────────────────────────────────────
@needs_bash
class TestTheStagingScript:

    def test_it_stages_exactly_the_listed_files(self, tmp_path):
        root = _fake_repo(tmp_path)
        dest = root / "src" / "doc-pipeline" / "guardrails"
        dest.mkdir()
        (dest / "stale.py").write_text("left by an old run", encoding="utf-8")
        (dest / "pii.py").write_text("an old pii.py", encoding="utf-8")
        r = _stage(root, root.as_posix())
        assert r.returncode == 0, r.stdout + r.stderr
        assert _staged(root) == PUBLIC
        for name in PUBLIC:
            assert (dest / name).read_text(encoding="utf-8") == f"# {name}\n"

    def test_by_default_it_stages_the_repo_it_is_in(self, tmp_path):
        root = _fake_repo(tmp_path)
        r = _stage(root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _staged(root) == PUBLIC

    def test_clean_removes_the_staged_copy(self, tmp_path):
        root = _fake_repo(tmp_path)
        assert _stage(root, root.as_posix()).returncode == 0
        r = _stage(root, "--clean", root.as_posix())
        assert r.returncode == 0, r.stdout + r.stderr
        assert not (root / "src" / "doc-pipeline" / "guardrails").exists()

    def test_a_missing_file_fails_the_staging(self, tmp_path):
        root = _fake_repo(tmp_path)
        (root / "src" / "proxy" / "guardrails" / "pii.py").unlink()
        assert _stage(root, root.as_posix()).returncode != 0

    def test_the_staged_engine_imports_with_the_standard_library_alone(self, tmp_path):
        """The image installs src/doc-pipeline/requirements.txt, not the proxy's: a staged
        module that needs anything else must have it added there (and this test updated)."""
        root = _fake_repo(tmp_path, real_engine=True)
        assert _stage(root, root.as_posix()).returncode == 0
        assert _staged(root) == PUBLIC
        code = ("import guardrails.pii as p; "
                "assert all(hasattr(p, n) for n in "
                "('PiiDetector', 'mask_matches', 'DEFAULT_ENTITIES', 'PHI_ENTITIES'))")
        r = subprocess.run([sys.executable, "-S", "-E", "-c", code],
                           cwd=root / "src" / "doc-pipeline", capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_the_list_is_exactly_what_the_doc_pipeline_imports():
    """pipeline.py's guardrails imports, the package __init__ and whatever those import from
    the package: nothing missing (the Job would lose PII masking) and nothing more."""
    pipeline = (_ROOT / "src" / "doc-pipeline" / "pipeline.py").read_text(encoding="utf-8")
    todo = ["__init__"] + re.findall(r"from guardrails\.(\w+) import", pipeline)
    assert len(todo) > 1, "pipeline.py no longer imports the guardrails engine"
    needed = set()
    while todo:
        module = todo.pop()
        if module in needed:
            continue
        needed.add(module)
        text = (_ROOT / "src" / "proxy" / "guardrails" / f"{module}.py").read_text(encoding="utf-8")
        # Package-relative or `guardrails.x` imports; stdlib and third-party names drop out below.
        todo += re.findall(r"from (?:guardrails)?\.?(\w+) import", text)
        todo = [m for m in todo if (_ROOT / "src" / "proxy" / "guardrails" / f"{m}.py").exists()]
    assert sorted(f"{m}.py" for m in needed) == _staged_list() == PUBLIC


# ── gcp-deploy.sh ─────────────────────────────────────────────────────────────
FAKE_DOCKER = r'''#!/usr/bin/env bash
if [[ "$1" == "build" ]]; then
  ctx="${@: -1}"
  staged="-"
  if [[ -d "$ctx/guardrails" ]]; then staged="$(ls "$ctx/guardrails" | tr '\n' ' ')"; fi
  echo "build $(basename "$ctx") staged=[${staged% }]" >> "$FAKE_LOG"
else
  echo "docker $1" >> "$FAKE_LOG"
fi
'''
FAKE_GCLOUD = '#!/usr/bin/env bash\necho "gcloud $*" >> "$FAKE_LOG"\n'


def _build_and_push(tmp_path, root: Path):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("gcloud", FAKE_GCLOUD)):
        (fakebin / name).write_text(body, encoding="utf-8", newline="\n")
        os.chmod(fakebin / name, 0o700)
    for ctx in ("proxy", "llmlingua-sidecar", "finetune-pipeline"):
        (root / "src" / ctx).mkdir(parents=True, exist_ok=True)
    body = "\n".join([_one_liner(DEPLOY, f) for f in ("info", "success", "warn", "error")]
                     + [_function(DEPLOY, "build_and_push"),
                        f"REPO_ROOT='{root.as_posix()}'; REGION=asia-south1; "
                        "REGISTRY_URL=reg.example/repo",
                        "build_and_push"])
    harness = tmp_path / "harness.sh"
    harness.write_text("set -euo pipefail\nRED= GREEN= YELLOW= BLUE= NC=\n" + body
                       + '\necho "[DONE]"\n', encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    r = subprocess.run([_bash(), harness.as_posix()], capture_output=True, encoding="utf-8",
                       errors="replace",
                       env={**os.environ, "PATH": fakebin.as_posix() + os.pathsep + os.environ["PATH"],
                            "FAKE_LOG": log.as_posix()})
    return r, [line for line in log.read_text(encoding="utf-8").splitlines()
               if line.startswith("build ")]


@needs_bash
class TestTheGcpDeployBuild:

    def test_doc_pipeline_builds_with_the_engine_staged_and_it_is_removed_after(self, tmp_path):
        root = _fake_repo(tmp_path)
        r, builds = _build_and_push(tmp_path, root)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert "build doc-pipeline staged=[__init__.py injection.py pii.py]" in builds
        assert "build finetune-pipeline staged=[-]" in builds
        assert not (root / "src" / "doc-pipeline" / "guardrails").exists()

    def test_a_failed_staging_stops_before_the_doc_pipeline_build(self, tmp_path):
        root = _fake_repo(tmp_path)
        (root / "src" / "proxy" / "guardrails" / "injection.py").unlink()
        r, builds = _build_and_push(tmp_path, root)
        assert r.returncode != 0 and "[DONE]" not in r.stdout
        assert "could not stage the guardrails engine" in r.stdout
        assert not [b for b in builds if b.startswith("build doc-pipeline")]


# ── Cloud Build ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["cloudbuild.yaml", "cloudbuild-images-only.yaml"])
def test_each_cloud_build_stages_before_building_doc_pipeline(name):
    text = (_ROOT / "ci" / name).read_text(encoding="utf-8")
    steps = yaml.safe_load(text)["steps"]
    stages = [s for s in steps if s.get("args") == ["scripts/ci/stage-doc-pipeline-guardrails.sh"]]
    builds = [s for s in steps if s.get("id") == "build-doc-pipeline"]
    assert len(stages) == 1 and len(builds) == 1
    stage, build = stages[0], builds[0]
    assert stage["entrypoint"] == "bash"
    assert build["args"][-1] == "src/doc-pipeline"
    assert stage["id"] in build["waitFor"]
    # The file list lives in the script only.
    assert "src/proxy/guardrails" not in text
