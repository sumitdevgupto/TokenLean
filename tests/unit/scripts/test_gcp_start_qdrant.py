"""start-gcp.sh checks Qdrant without changing how it is reached.

Its Qdrant step opened ingress and an allUsers invoker grant, probed without the api-key (so it
always read "empty" and offered a reseed), then set ingress to internal-only — which, per
infra/main.tf, rejects the proxy and the jobs, so retrieval and docs chat returned nothing until
the next Terraform apply. A Ctrl-C at its prompt left the allUsers grant in place. Now the probe
goes as the operator (an identity token) with the key and never touches ingress; only a reseed
opens the allUsers window, and a trap closes it however the script ends.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
START = (_ROOT / "scripts" / "gcp" / "start-gcp.sh").read_text(encoding="utf-8")
DEPLOY = (_ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")


def _function(script: str, name: str) -> str:
    m = re.search(rf"^([ ]*){name}\(\) \{{\n.*?^\1\}}\n", script, re.M | re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _one_liner(script: str, name: str) -> str:
    m = re.search(rf"^{name}\(\)\s*\{{.*\}}[ \t]*$", script, re.M)
    assert m, f"{name}() not found"
    return m.group(0)


def _bash():
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
case "$*" in
  "run services describe token-opt-qdrant"*) echo "${FAKE_QDRANT_URL:-}" ;;
  "secrets versions access latest --secret=qdrant-api-key"*) printf '%s' "k-123" ;;
  "auth print-identity-token"*) echo "tok-abc" ;;
  "run services add-iam-policy-binding token-opt-qdrant"*) ;;
  "run services remove-iam-policy-binding token-opt-qdrant"*) ;;
  *) echo "fake gcloud: unhandled: $*" >&2; exit 2 ;;
esac
'''

FAKE_SEED = r'''#!/usr/bin/env bash
echo "seed key=${QDRANT_API_KEY:-} $*" >> "$FAKE_LOG"
if [[ "${FAKE_SEED:-ok}" == "interrupt" ]]; then kill -INT "$PPID"; fi
exit 0
'''


class _Qdrant(BaseHTTPRequestHandler):
    status = 200
    points = 5
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({"result": {"points_count": type(self).points}}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def qdrant():
    _Qdrant.status, _Qdrant.points, _Qdrant.seen = 200, 5, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Qdrant)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _run(tmp_path, qdrant_url, answer="", can_seed=True, **env):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    python = Path(sys.executable).as_posix()
    for name, text in (("gcloud", FAKE_GCLOUD),
                       ("python3", f'#!/usr/bin/env bash\nexec "{python}" "$@"\n')):
        (fakebin / name).write_text(text, encoding="utf-8", newline="\n")
        os.chmod(fakebin / name, 0o700)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "seed-data.sh").write_text(FAKE_SEED, encoding="utf-8", newline="\n")
    os.chmod(repo / "scripts" / "seed-data.sh", 0o700)
    body = "\n".join([
        "set -euo pipefail", "PROJECT_ID=fake-proj; REGION=asia-south1",
        f'REPO_ROOT="{repo.as_posix()}"', "RED= GREEN= YELLOW= BLUE= NC=",
        *(_one_liner(START, f) for f in ("info", "success", "warn", "error", "can_seed")),
        *(_function(START, f) for f in ("qdrant_points", "close_qdrant_window",
                                        "seed_qdrant", "verify_qdrant")),
        "can_seed() { return 0; }" if can_seed else "can_seed() { return 1; }",
        "QDRANT_STATUS=unknown", "verify_qdrant", 'echo "STATUS=${QDRANT_STATUS}"',
        'echo "[DONE]"'])
    harness = tmp_path / "harness.sh"
    harness.write_text(body + "\n", encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    r = subprocess.run([_bash(), harness.as_posix()], input=answer, capture_output=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PATH": fakebin.as_posix() + os.pathsep + os.environ["PATH"],
                            "CLOUDSDK_CONFIG": (tmp_path / "no-gcloud-config").as_posix(),
                            "FAKE_LOG": log.as_posix(), "FAKE_QDRANT_URL": qdrant_url,
                            "QDRANT_IAM_SETTLE_SECONDS": "0", **env})
    return r, [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _iam(calls):
    return [c.split()[3] for c in calls if "iam-policy-binding" in c]


@needs_bash
class TestTheQdrantCheck:

    def test_a_seeded_collection_is_read_as_the_operator_with_the_key(self, tmp_path, qdrant):
        r, calls = _run(tmp_path, qdrant)
        assert r.returncode == 0 and "[DONE]" in r.stdout, r.stdout + r.stderr
        assert "STATUS=seeded (5 docs)" in r.stdout
        assert len(_Qdrant.seen) == 1
        seen = _Qdrant.seen[0]
        assert seen.get("api-key") == "k-123" and seen.get("authorization") == "Bearer tok-abc"
        assert not any("services update" in c or "--ingress" in c for c in calls)
        assert _iam(calls) == []

    def test_a_collection_it_cannot_read_is_not_called_empty(self, tmp_path, qdrant):
        _Qdrant.status = 401
        r, calls = _run(tmp_path, qdrant, answer="y\n")
        assert r.returncode == 0 and "STATUS=unknown" in r.stdout, r.stdout + r.stderr
        assert "Re-seed" not in r.stdout and _iam(calls) == []
        assert not any(c.startswith("seed ") for c in calls)

    def test_an_unreachable_qdrant_is_not_called_empty(self, tmp_path):
        r, calls = _run(tmp_path, "http://127.0.0.1:9", answer="y\n")     # nothing listens
        assert r.returncode == 0 and "STATUS=unknown" in r.stdout, r.stdout + r.stderr
        assert "Re-seed" not in r.stdout and _iam(calls) == []

    def test_a_missing_collection_offers_a_reseed_and_no_means_no(self, tmp_path, qdrant):
        _Qdrant.status = 404
        r, calls = _run(tmp_path, qdrant, answer="no\n")
        assert r.returncode == 0 and "STATUS=EMPTY" in r.stdout, r.stdout + r.stderr
        assert "Re-seed" in r.stdout and _iam(calls) == []

    def test_a_reseed_opens_the_window_and_closes_it(self, tmp_path, qdrant):
        _Qdrant.points = 0
        r, calls = _run(tmp_path, qdrant, answer="y\n")
        assert r.returncode == 0 and "STATUS=seeded" in r.stdout, r.stdout + r.stderr
        assert _iam(calls) == ["add-iam-policy-binding", "remove-iam-policy-binding"]
        assert any(c.startswith(f"seed key=k-123 --qdrant-url {qdrant}") for c in calls)
        assert not any("--ingress" in c for c in calls)

    def test_an_interrupted_reseed_still_closes_the_window(self, tmp_path, qdrant):
        _Qdrant.points = 0
        r, calls = _run(tmp_path, qdrant, answer="y\n", FAKE_SEED="interrupt")
        assert r.returncode == 130, r.stdout + r.stderr
        assert _iam(calls) == ["add-iam-policy-binding", "remove-iam-policy-binding"]

    def test_the_script_runs_the_check(self):
        assert "\nverify_qdrant\n" in START
        assert "--ingress" not in START


def test_the_deploy_seeding_window_is_closed_by_a_trap_too():
    seed = _function(DEPLOY, "seed_qdrant")
    grant = seed.index("add-iam-policy-binding token-opt-qdrant")
    trap = re.search(r"trap (\S+) EXIT", seed[:grant])
    assert trap, "no EXIT trap before the grant"
    assert "trap 'exit 130' INT TERM" in seed[:grant]
    # The handler closes the window, and still removes the work directory whose trap it
    # replaced; once the window is closed that trap is put back, not cleared.
    handler = _function(DEPLOY, trap.group(1))
    assert "\n  close_qdrant_seed_window\n" in handler and "\n  remove_work_dir\n" in handler
    after = seed[seed.rindex("\n  close_qdrant_seed_window\n"):]
    assert "trap remove_work_dir EXIT" in after and "trap - EXIT" not in after
