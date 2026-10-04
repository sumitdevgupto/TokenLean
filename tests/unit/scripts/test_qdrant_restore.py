"""Qdrant's Cloud Run container restores the newest snapshot of each collection before serving.

infra/qdrant-restore.sh is the container's command: it reads the snapshot bucket (mounted
read-only), passes Qdrant one `--snapshot <file>:<collection>` per collection, the newest
(snapshot objects are named <collection>.<UTC time>.snapshot, so the newest sorts last), and
hands over to the image's own entrypoint, which Qdrant restores them under before it serves.
It runs here in bash with a stand-in entrypoint that prints what it is given.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "qdrant-restore.sh"


def _bash():
    """Git's bash on Windows (a PATH `bash` may be the WSL launcher), else PATH bash."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(_bash() is None, reason="needs bash")


def _start(tmp_path, names):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    for name in names:
        (snapshots / name).write_text("x", encoding="utf-8")
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text('#!/usr/bin/env bash\nfor a in "$@"; do printf "ARG %s\\n" "$a"; done\n',
                          encoding="utf-8", newline="\n")
    os.chmod(entrypoint, 0o700)
    r = subprocess.run([_bash(), SCRIPT.as_posix()], capture_output=True, encoding="utf-8",
                       timeout=60, env={**os.environ, "SNAPSHOT_DIR": snapshots.as_posix(),
                                        "QDRANT_ENTRYPOINT": entrypoint.as_posix()})
    assert r.returncode == 0, r.stdout + r.stderr
    args = [line[4:] for line in r.stdout.splitlines() if line.startswith("ARG ")]
    return snapshots.as_posix(), args


def _pairs(args):
    assert len(args) % 2 == 0 and all(flag == "--snapshot" for flag in args[0::2]), args
    return sorted(args[1::2])


def test_the_newest_snapshot_of_each_collection_is_restored(tmp_path):
    root, args = _start(tmp_path, [
        "rag_ACME-PRD-01.20261001T080000.000000Z.snapshot",
        "rag_ACME-PRD-01.20261003T101500.000001Z.snapshot",   # the newest of rag_ACME-PRD-01
        "rag_ACME-PRD-01.20261002T120000.000000Z.snapshot",
        "rag_NOVA-STG-01_kb.20261002T000000.000000Z.snapshot",
        "notes.txt",                                          # not a snapshot
    ])
    assert _pairs(args) == [
        f"{root}/rag_ACME-PRD-01.20261003T101500.000001Z.snapshot:rag_ACME-PRD-01",
        f"{root}/rag_NOVA-STG-01_kb.20261002T000000.000000Z.snapshot:rag_NOVA-STG-01_kb",
    ]


def test_without_snapshots_qdrant_starts_empty_as_before(tmp_path):
    _, args = _start(tmp_path, [])
    assert args == []
