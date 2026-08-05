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


# ─── Markdown-blind facts gate (fixed 2026-08-05) ────────────────────────────
# A raw substring gate fails whenever the model EMPHASISES the checked entity, so
# it ends up measuring formatting style rather than answer fidelity — and fires
# hardest on precisely the arm whose formatting an optimisation changed.

def _run_benchmark_mod():
    spec = importlib.util.spec_from_file_location("run_benchmark", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_normalise_for_match_strips_emphasis_but_keeps_underscores():
    m = _run_benchmark_mod()
    assert m.normalise_for_match("A **simple iron boar crest**") == "a simple iron boar crest"
    assert m.normalise_for_match("`_affinity_propagation.py`") == "_affinity_propagation.py"
    assert m.normalise_for_match("split across a\n  line") == "split across a line"


def test_check_facts_finds_fact_wrapped_in_markdown():
    m = _run_benchmark_mod()
    assert m.check_facts("A **simple iron boar crest** adorns it.",
                         ["A simple iron boar crest"])["passed"] is True
    assert m.check_facts("The **St Andrews Agreement** followed.",
                         ["The St Andrews Agreement"])["passed"] is True


def test_check_facts_still_fails_on_a_genuinely_absent_fact():
    """The fix must not blunt the gate."""
    r = _run_benchmark_mod().check_facts("Totally unrelated answer.",
                                         ["A simple iron boar crest"])
    assert r["passed"] is False
    assert r["missing"] == ["A simple iron boar crest"]


def test_check_facts_or_group_and_forbidden_normalise_too():
    m = _run_benchmark_mod()
    assert m.check_facts("Plans are **monthly**.", [["annual", "monthly"]])["passed"] is True
    r = m.check_facts("Leaked **secret-host**.", [], ["secret-host"])
    assert r["present_forbidden"] == ["secret-host"]
