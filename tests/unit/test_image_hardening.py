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


def _instructions(lines):
    """The stage's instructions, each with its backslash continuations joined."""
    out, current = [], ""
    for line in lines:
        current += line.rstrip()
        if current.endswith("\\"):
            current = current[:-1] + " "
            continue
        if current.strip() and not current.lstrip().startswith("#"):
            out.append(current.strip())
        current = ""
    return out


METRICS_DOCKERFILES = [p for p in DOCKERFILES if "PROMETHEUS_MULTIPROC_DIR" in p.read_text(encoding="utf-8")]


def test_the_metrics_check_is_not_vacuous():
    assert ROOT / "src/proxy/Dockerfile" in METRICS_DOCKERFILES


@pytest.mark.parametrize("dockerfile", METRICS_DOCKERFILES,
                         ids=[p.relative_to(ROOT).as_posix() for p in METRICS_DOCKERFILES])
def test_the_metrics_directory_exists_for_every_command(dockerfile):
    """PROMETHEUS_MULTIPROC_DIR puts every process in the image into multiprocess metrics, but only
    the server's CMD created the directory: a job started with its own command, or a `docker exec`,
    failed at its first metric. The image creates it, as the user it runs as."""
    _, lines = _stages(dockerfile.read_text(encoding="utf-8"))[-1]
    user, env_set, made = None, False, False
    for ins in _instructions(lines):
        if re.match(r"USER\s", ins, re.I):
            user = ins.split(None, 1)[1].split(":")[0]
        elif re.match(r"ENV\s+PROMETHEUS_MULTIPROC_DIR[=\s]", ins, re.I):
            env_set = True
        elif (env_set and re.match(r"RUN\s", ins, re.I)
              and re.search(r"mkdir\s+-p\s+\"?\$\{?PROMETHEUS_MULTIPROC_DIR\b", ins)):
            made = user not in (None, "root", "0")
    assert made, "no RUN after the ENV creates $PROMETHEUS_MULTIPROC_DIR as the unprivileged user"


def test_the_key_sync_image_hands_its_keys_file_to_the_user_that_reads_it():
    """The key-sync job stages the operator's keys file into the build context with whatever mode it
    has, and COPY makes it root's, while the image runs as an unprivileged user: a keys file only
    its owner may read (mode 600, usual on a Linux host) left the job unable to open it."""
    _, lines = _stages((ROOT / "infra/migrations/Dockerfile.synckeys").read_text(encoding="utf-8"))[-1]
    ins = _instructions(lines)
    user = [i.split(None, 1)[1].split(":")[0] for i in ins if re.match(r"USER\s", i, re.I)][-1]
    copies = [i for i in ins if re.match(r"COPY\s", i, re.I) and "local-keys.json" in i]
    assert copies, "the image no longer copies local-keys.json"
    owner = re.search(r"--chown=(\S+)", copies[0])
    assert owner and owner.group(1).split(":")[0] == user, copies[0]


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
