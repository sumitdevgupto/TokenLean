"""Structural gate for examples/benchmark/ — keeps the public reproducible-proof
harness from silently rotting. No LLM calls; validates the dataset shape and that
the runner imports and exposes a main() entrypoint."""
import importlib.util
import json
from pathlib import Path

import pytest

BENCH = Path(__file__).parent.parent.parent / "examples" / "benchmark"
DATASET = BENCH / "dataset.jsonl"
SECURITY_SMOKE = BENCH / "security-smoke.jsonl"
SCRIPT = BENCH / "run_benchmark.py"


def _load_requests():
    return [json.loads(ln) for ln in DATASET.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _load_security_smoke():
    return [json.loads(ln) for ln in SECURITY_SMOKE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_benchmark", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    assert callable(getattr(_load_runner(), "main", None)), "run_benchmark.py must expose main()"


# ── Trust & Safety smoke (--security-smoke / _security_summary) ───────────────

def test_security_smoke_dataset_exists_and_shaped():
    assert SECURITY_SMOKE.exists(), f"missing {SECURITY_SMOKE}"
    reqs = _load_security_smoke()
    assert len(reqs) >= 3, "security smoke should carry at least the G30/G31 attacks + a control"
    # Every record parses and carries a readable label.
    for r in reqs:
        assert isinstance(r.get("messages"), list) and r["messages"]
        assert "_label" in r
    # At least one attack hides its injection in a NON-user (system/tool) role — that is
    # the indirect / RAG case G31 exists for (G30 only scans the user turn).
    ctx_attacks = [
        r for r in reqs
        if any(m.get("role") in ("system", "tool")
               and any(k in m.get("content", "").lower()
                       for k in ("ignore all", "disregard your", "unrestricted"))
               for m in r["messages"])
    ]
    assert ctx_attacks, "security smoke must include an injection in a system/tool context (G31)"
    # Attack rows carry a `forbidden` marker; the control carries expected_facts.
    assert any("forbidden" in r for r in reqs), "attack rows need a forbidden marker"
    assert any("expected_facts" in r for r in reqs), "need a benign control with expected_facts"


def test_security_smoke_dataset_is_ascii():
    SECURITY_SMOKE.read_text(encoding="ascii")  # raises on non-ASCII bytes


def test_security_summary_passes_when_blocked_or_not_obeyed():
    _sec = _load_runner()._security_summary
    rows = [
        {"label": "ctx-sys", "finish_reason": "content_filter",
         "facts": {"passed": True, "missing": [], "present_forbidden": []}},
        {"label": "user-inj", "finish_reason": "stop",
         "facts": {"passed": True, "missing": [], "present_forbidden": []}},
        {"label": "control", "finish_reason": "stop",
         "facts": {"passed": True, "missing": [], "present_forbidden": []}},
    ]
    assert _sec(rows) == 0


def test_security_summary_fails_on_leak():
    _sec = _load_runner()._security_summary
    rows = [
        {"label": "leaky", "finish_reason": "stop",
         "facts": {"passed": False, "missing": [], "present_forbidden": ["EXFIL-OK"]}},
    ]
    assert _sec(rows) == 2, "an obeyed injection (forbidden marker present) must fail the smoke"


def test_security_summary_fails_when_control_loses_facts():
    _sec = _load_runner()._security_summary
    rows = [
        {"label": "control", "finish_reason": "stop",
         "facts": {"passed": False, "missing": ["Paris"], "present_forbidden": []}},
    ]
    assert _sec(rows) == 2, "a benign control that lost its expected facts must fail the smoke"
