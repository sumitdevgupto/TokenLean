"""docker-backup.sh uploads only to a bucket in your own project, from a private temp dir.

With no bucket configured it fell back to the global name token-opt-config, swallowed the
failure to create it, and uploaded full pg_dumpall and Redis dumps to whoever owned that name;
the dumps stayed in /tmp. The script runs here as a copy in a temporary repo (never beside the
real .env), against fake docker and gcloud.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (_ROOT / "scripts" / "local" / "docker-backup.sh").read_text(encoding="utf-8")


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
  "config get-value project") echo "${FAKE_PROJECT-fake-proj}" ;;
  "storage buckets list --project=fake-proj --format=value(name)") cat "$FAKE_BUCKETS" ;;
  "storage buckets create gs://"*)
    [[ "${FAKE_CREATE:-ok}" == ok ]] || exit 1
    name="${4#gs://}"; echo "$name" >> "$FAKE_BUCKETS" ;;
  "storage cp "*)
    [[ -f "$3" ]] || { echo "no such file: $3" >&2; exit 1; }
    echo "uploaded $(cygpath -m "$3" 2>/dev/null || echo "$3")" >> "$FAKE_LOG" ;;
  *) echo "fake gcloud: unhandled: $*" >&2; exit 2 ;;
esac
'''

FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  ps) for c in token-opt-redis token-opt-postgres token-opt-qdrant; do
        if [[ "$*" == *"name=$c"* ]]; then echo "$c"; fi; done ;;
  exec) [[ "$*" == *pg_dumpall* ]] && echo "-- dump" ; exit 0 ;;
  cp) dest="${@: -1}"; [[ -d "$dest" ]] && dest="$dest/$(basename "$2")"; echo data > "$dest" ;;
  *) echo "fake docker: unhandled: $*" >&2; exit 2 ;;
esac
'''


def _run(tmp_path, args=(), env_file=None, buckets=(), **env):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    for name, text in (("gcloud", FAKE_GCLOUD), ("docker", FAKE_DOCKER),
                       ("gsutil", '#!/usr/bin/env bash\necho "gsutil $*" >> "$FAKE_LOG"\nexit 1\n'),
                       ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        (fakebin / name).write_text(text, encoding="utf-8", newline="\n")
        os.chmod(fakebin / name, 0o700)
    repo = tmp_path / "repo"
    (repo / "scripts" / "local").mkdir(parents=True, exist_ok=True)
    script = repo / "scripts" / "local" / "docker-backup.sh"
    script.write_text(SCRIPT, encoding="utf-8", newline="\n")
    if env_file is not None:
        (repo / ".env").write_text(env_file, encoding="utf-8", newline="\n")
    log, bucket_file = tmp_path / "calls.log", tmp_path / "buckets.txt"
    log.write_text("", encoding="utf-8")
    bucket_file.write_text("".join(f"{b}\n" for b in buckets), encoding="utf-8", newline="\n")
    r = subprocess.run([_bash(), script.as_posix(), *args], capture_output=True, encoding="utf-8",
                       errors="replace",
                       env={**{k: v for k, v in os.environ.items() if k != "CONFIG_GCS_BUCKET"},
                            "PATH": fakebin.as_posix() + os.pathsep + os.environ["PATH"],
                            "CLOUDSDK_CONFIG": (tmp_path / "no-gcloud-config").as_posix(),
                            "FAKE_LOG": log.as_posix(), "FAKE_BUCKETS": bucket_file.as_posix(),
                            **env})
    return r, [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _uploads(calls):
    return [c for c in calls if c.startswith("gcloud storage cp ")]


@needs_bash
class TestTheBackupBucket:

    def test_without_a_bucket_it_refuses_before_dumping_anything(self, tmp_path):
        r, calls = _run(tmp_path)
        assert r.returncode == 1 and "No backup bucket" in r.stdout, r.stdout + r.stderr
        assert not any(c.startswith("docker ") for c in calls) and not _uploads(calls)
        assert "token-opt-config" not in SCRIPT

    def test_a_bucket_it_cannot_create_in_the_project_is_refused(self, tmp_path):
        r, calls = _run(tmp_path, ["--bucket", "someone-elses"], FAKE_CREATE="taken")
        assert r.returncode == 1 and "not in project fake-proj" in r.stdout, r.stdout + r.stderr
        assert not any(c.startswith("docker ") for c in calls) and not _uploads(calls)

    def test_a_name_that_only_prefixes_one_of_yours_is_not_yours(self, tmp_path):
        r, calls = _run(tmp_path, ["--bucket", "my-backups"], buckets=["my-backups-prod"],
                        FAKE_CREATE="taken")
        assert r.returncode == 1 and "not in project fake-proj" in r.stdout, r.stdout + r.stderr
        assert not _uploads(calls)

    def test_without_a_project_it_refuses(self, tmp_path):
        r, calls = _run(tmp_path, ["--bucket", "b"], buckets=["b"], FAKE_PROJECT="")
        assert r.returncode == 1 and "No GCP project" in r.stdout, r.stdout + r.stderr
        assert not _uploads(calls)

    def test_a_missing_bucket_is_created_in_the_project_then_used(self, tmp_path):
        r, calls = _run(tmp_path, ["--bucket", "my-backups"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert any(c.startswith("gcloud storage buckets create gs://my-backups --project=fake-proj")
                   for c in calls)
        assert len(_uploads(calls)) == 3

    def test_the_env_bucket_is_used_and_the_dumps_go_to_a_timestamped_folder(self, tmp_path):
        r, calls = _run(tmp_path, env_file="CONFIG_GCS_BUCKET=env-backups\n", buckets=["env-backups"])
        assert r.returncode == 0, r.stdout + r.stderr
        uploads = _uploads(calls)
        assert len(uploads) == 3
        for up in uploads:
            assert re.search(r" gs://env-backups/backups/\d{8}-\d{6}/$", up), up

    def test_the_dumps_are_written_to_a_private_temp_dir_and_removed(self, tmp_path):
        r, calls = _run(tmp_path, ["--bucket", "b"], buckets=["b"])
        assert r.returncode == 0, r.stdout + r.stderr
        sources = [line.split(" ", 1)[1] for line in calls if line.startswith("uploaded ")]
        assert len(sources) == 3 and len({str(Path(s).parent) for s in sources}) == 1
        assert not any(Path(s).exists() for s in sources)
        assert not Path(sources[0]).parent.exists()
        # Three dumps, each written into $WORK and uploaded from there.
        assert SCRIPT.count('"${WORK}/') == 6 and 'chmod 700 "$WORK"' in SCRIPT
