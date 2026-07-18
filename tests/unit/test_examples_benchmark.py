"""Structural gate for examples/benchmark/ — keeps the public reproducible-proof
harness from silently rotting. No LLM calls; validates the dataset shape and that
the runner imports and exposes a main() entrypoint."""
import importlib.util
import json
from pathlib import Path

import pytest

BENCH = Path(__file__).parent.parent.parent / "examples" / "benchmark"
DATASET = BENCH / "dataset.jsonl"
SCRIPT = BENCH / "run_benchmark.py"


def _load_requests():
    return [json.loads(ln) for ln in DATASET.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_dataset_exists_and_nonempty():
    assert DATASET.exists(), f"missing {DATASET}"
    reqs = _load_requests()
    assert len(reqs) >= 10, "benchmark dataset should have a meaningful number of requests"


def test_dataset_request_shapes():
    for r in _load_requests():
        assert isinstance(r.get("messages"), list) and r["messages"], "each request needs messages"
        assert r["messages"][0]["role"] == "system", "first message should be the shared system prompt"
        assert any(m["role"] == "user" for m in r["messages"]), "each request needs a user message"
        assert "_label" in r, "each request should carry a _label for readable output"


def test_dataset_has_cache_eligible_repeats():
    """The headline savings rely on cache hits — ensure the dataset includes repeats."""
    reqs = _load_requests()
    user_msgs = [m["content"] for r in reqs for m in r["messages"] if m["role"] == "user"]
    assert len(user_msgs) > len(set(user_msgs)), "dataset must include repeated queries (G05 cache)"


def test_dataset_is_ascii():
    """ASCII-only so the file reads under any locale encoding (cp1252 on Windows)."""
    DATASET.read_text(encoding="ascii")  # raises if non-ASCII bytes present


def test_runner_imports_and_has_main():
    spec = importlib.util.spec_from_file_location("run_benchmark", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(getattr(mod, "main", None)), "run_benchmark.py must expose main()"
