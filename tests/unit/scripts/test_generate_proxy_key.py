"""generate_proxy_key.py must never overwrite a key store it could not read.

It used to treat any read error on config/local-keys.json — a corrupt file, or a UTF-8
byte-order mark from a Windows editor — as an empty store, then overwrite the file with
only the new key (and the write was not atomic). That file is also synced to production
Postgres on every commercial deploy.
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_proxy_key.py"


def _generate(out_dir, tenant="ACME"):
    return subprocess.run([sys.executable, str(SCRIPT), "--tenant", tenant, "--output-dir", str(out_dir)],
                          capture_output=True, encoding="utf-8", errors="replace")


def _store(out_dir):
    return out_dir / "local-keys.json"


def test_a_byte_order_marked_store_is_read_and_extended(tmp_path):
    _store(tmp_path).write_text(json.dumps({"h1": {"tenant_id": "OLD"}}), encoding="utf-8-sig")
    r = _generate(tmp_path)
    assert r.returncode == 0, r.stderr
    data = json.loads(_store(tmp_path).read_text(encoding="utf-8"))
    assert "h1" in data and len(data) == 2


def test_an_unreadable_store_is_left_untouched_and_the_run_fails(tmp_path):
    _store(tmp_path).write_text("{not json", encoding="utf-8")
    r = _generate(tmp_path)
    assert r.returncode != 0
    assert _store(tmp_path).read_text(encoding="utf-8") == "{not json"


def test_a_store_that_is_not_an_object_is_refused(tmp_path):
    _store(tmp_path).write_text("[]", encoding="utf-8")
    r = _generate(tmp_path)
    assert r.returncode != 0
    assert _store(tmp_path).read_text(encoding="utf-8") == "[]"


def test_issuing_twice_accumulates_keys_and_leaves_no_temp_file(tmp_path):
    assert _generate(tmp_path, "A").returncode == 0
    assert _generate(tmp_path, "B").returncode == 0
    data = json.loads(_store(tmp_path).read_text(encoding="utf-8"))
    assert sorted(meta["tenant_id"] for meta in data.values()) == ["A", "B"]
    assert list(tmp_path.iterdir()) == [_store(tmp_path)]
