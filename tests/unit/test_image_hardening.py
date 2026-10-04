"""Every product image runs as an unprivileged user, on a base pinned by digest, and CI tests the
Python those images run.

The images ran as root, so code execution in any of them was root inside its container. Their
bases were floating tags (python:3.11-slim, node:20-alpine, ...), so a rebuild silently took a
different OS and Python patch level. And CI tested Python 3.12 while every image runs 3.11, so
code that only 3.12 accepts would pass CI and fail in production. Each Dockerfile under src/ and
infra/ now ends as a non-root USER, every FROM names a digest, CI runs the images' Python, and
Dependabot proposes digest bumps (never a new major or minor tag) so OS patches still arrive,
for review.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted(p for d in ("src", "infra") for p in (ROOT / d).rglob("Dockerfile*")
                     if p.is_file() and not p.name.endswith(".dockerignore"))
IDS = [p.relative_to(ROOT).as_posix() for p in DOCKERFILES]


def _stages(text):
    """[(FROM line, [the stage's other lines])], in order."""
    stages = []
    for line in text.splitlines():
        if re.match(r"FROM\s", line, re.I):
            stages.append((line, []))
        elif stages:
            stages[-1][1].append(line)
    return stages


def test_the_product_images_are_all_checked():
    # Not vacuous: the open-source images are always in the tree.
    assert {"src/proxy/Dockerfile", "src/llmlingua-sidecar/Dockerfile", "src/tika-sidecar/Dockerfile",
            "infra/migrations/Dockerfile.migrate"} <= set(IDS)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=IDS)
def test_every_base_is_pinned_by_digest(dockerfile):
    stages, earlier = _stages(dockerfile.read_text(encoding="utf-8")), set()
    for from_line, _ in stages:
        parts = from_line.split()
        if parts[1] not in earlier:           # FROM an earlier stage of the same file
            assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", parts[1]), from_line
        if len(parts) >= 4 and parts[2].upper() == "AS":
            earlier.add(parts[3])


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=IDS)
def test_every_image_ends_as_an_unprivileged_user(dockerfile):
    _, lines = _stages(dockerfile.read_text(encoding="utf-8"))[-1]
    users = [line.split(None, 1)[1].strip() for line in lines if re.match(r"USER\s", line, re.I)]
    assert users, "the final stage never sets USER, so it runs as root"
    assert users[-1].split(":")[0] not in ("root", "0"), users[-1]


def test_ci_tests_the_python_the_images_run():
    images = set()
    for p in DOCKERFILES:
        images |= set(re.findall(r"^FROM python:(\d+\.\d+)", p.read_text(encoding="utf-8"), re.M))
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = set(re.findall(r'python-version:\s*"?(\d+\.\d+)"?', ci))
    assert images and versions == images, (versions, images)


def test_every_python_file_parses():
    # Run under CI's Python, the images' own: syntax only a newer Python accepts fails here,
    # in a script no test imports too, not in production.
    bad = []
    for d in ("src", "tests", "scripts"):
        for p in (ROOT / d).rglob("*.py"):
            if "node_modules" in p.parts:
                continue
            try:
                compile(p.read_bytes(), str(p), "exec")
            except SyntaxError as exc:
                bad.append(f"{p.relative_to(ROOT).as_posix()}:{exc.lineno}: {exc.msg}")
    assert not bad, bad


def test_the_lockfiles_are_resolved_on_the_proxy_images_own_base():
    # The compile reads the proxy Dockerfile's FROM, digest and all, so it resolves on the
    # Python the image ships and a digest bump there moves it too.
    script = (ROOT / "scripts" / "compile-requirements.sh").read_text(encoding="utf-8")
    assert re.search(r'^IMAGE="\$\(sed .*src/proxy/Dockerfile', script, re.M)
    assert not re.search(r'^IMAGE="python:', script, re.M)


def test_dependabot_proposes_digest_bumps_and_no_new_tags():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    docker = [u for u in cfg["updates"] if u["package-ecosystem"] == "docker"]
    assert len(docker) == 1
    assert docker[0].get("open-pull-requests-limit", 5) > 0
    ignored = {t for rule in docker[0].get("ignore", []) if rule.get("dependency-name") == "*"
               for t in rule.get("update-types", [])}
    assert {"version-update:semver-major", "version-update:semver-minor"} <= ignored
