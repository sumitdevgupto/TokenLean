"""Backlog #39 — /metrics must aggregate across uvicorn workers.

The proxy images run ``uvicorn --workers 2``. prometheus_client keeps its metrics
in per-process memory, so without ``PROMETHEUS_MULTIPROC_DIR`` each worker holds a
private registry and a scrape answers from whichever worker the OS routed to. That
was observed live as six consecutive scrapes returning 69, 69, 23, 69, 23, 69 — an
oscillating series that every ``rate()`` reads as a counter reset.

These tests pin the three halves of the fix:
  1. the endpoint takes the MultiProcessCollector branch when the env var is set,
     and the ordinary single-process path when it is not;
  2. multiprocess aggregation genuinely sums across separate OS processes (proved
     with real subprocesses, not mocks — that is the whole claim of the fix);
  3. every Gauge in the proxy declares an explicit multiprocess_mode, so a gauge
     added later cannot silently fall back to the default.
"""
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, multiprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import main  # noqa: E402

_SRC_PROXY = Path(__file__).resolve().parents[2] / "src" / "proxy"

# Plain TestClient (no `with`) so the app lifespan — which tears down the shared
# redis pool — does not run; /metrics needs no lifespan state. Mirrors
# tests/unit/test_main_endpoints_isolation.py.
_client = TestClient(main.app)


class TestEndpointBranch:
    """The env var is read PER REQUEST, not at import, so both paths stay live."""

    def test_single_process_path_is_the_default(self):
        # The unit suite has no PROMETHEUS_MULTIPROC_DIR (a session fixture in
        # conftest guarantees it), so this is today's behaviour, unchanged.
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", ""):
            r = _client.get("/metrics")
        assert r.status_code == 200
        assert "token_opt_requests_total" in r.text

    def test_multiproc_branch_taken_when_env_var_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", ""):
            r = _client.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        # An empty multiproc dir is a VALID empty scrape — and it proves the
        # branch was taken, because the in-process registry would have emitted
        # token_opt_requests_total here.
        assert "token_opt_requests_total" not in r.text

    def test_scrape_token_gate_still_applies_in_multiproc_mode(self, tmp_path, monkeypatch):
        """The multiproc branch sits AFTER the auth check — never in front of it."""
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        with patch.object(main, "_METRICS_SCRAPE_TOKEN", "s3cret"):
            assert _client.get("/metrics").status_code == 401
            ok = _client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


_CHILD = textwrap.dedent(
    """
    import sys
    from prometheus_client import Counter, Gauge
    c = Counter("worker_requests_total", "test counter")
    c.inc(float(sys.argv[1]))
    g = Gauge("worker_state", "test gauge", multiprocess_mode="max")
    g.set(float(sys.argv[2]))
    """
)


class TestRealCrossProcessAggregation:
    """The actual claim: one scrape sees the sum of what every worker recorded.

    Two real subprocesses stand in for the two uvicorn workers. mmap files work on
    both Linux (CI) and Windows (local dev), and no uvicorn is needed.
    """

    def _run_child(self, tmp_path, counter_inc, gauge_val):
        env = dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path))
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, str(counter_inc), str(gauge_val)],
            env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc

    def _collect(self, tmp_path):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
        return {
            (m.name, s.name): s.value
            for m in registry.collect()
            for s in m.samples
        }

    def test_counters_sum_across_processes(self, tmp_path):
        self._run_child(tmp_path, 3, 1)
        self._run_child(tmp_path, 4, 2)
        samples = self._collect(tmp_path)
        # 3 + 4 — NOT 3 or 4 depending on which worker answered.
        assert samples[("worker_requests", "worker_requests_total")] == 7.0

    def test_max_gauge_reports_the_highest_worker_value(self, tmp_path):
        self._run_child(tmp_path, 1, 1)
        self._run_child(tmp_path, 1, 5)
        self._run_child(tmp_path, 1, 2)
        samples = self._collect(tmp_path)
        assert samples[("worker_state", "worker_state")] == 5.0

    def test_max_mode_adds_no_pid_label(self, tmp_path):
        """Dashboards and the readiness text parser key off series labels.

        Only the "all"/"liveall" modes add a `pid` label; "max" must not, or every
        Grafana query and run_readiness's counter parser would silently miss.
        """
        self._run_child(tmp_path, 1, 1)
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
        for metric in registry.collect():
            for sample in metric.samples:
                assert "pid" not in sample.labels

    def test_a_single_process_alone_is_still_correct(self, tmp_path):
        """--workers 1 / a single Cloud Run instance must not regress."""
        self._run_child(tmp_path, 5, 3)
        samples = self._collect(tmp_path)
        assert samples[("worker_requests", "worker_requests_total")] == 5.0


class TestGaugeModesArePinned:
    """A Gauge without an explicit mode silently defaults — catch it at the source."""

    def test_the_four_known_gauges_declare_max(self):
        from middleware import g18_observability as g18
        from middleware import quality_metrics as qm

        for gauge in (
            g18.WORKFLOW_TURNS,
            g18.CIRCUIT_BREAKER_STATE,
            g18.MODEL_LOCKOUT_STATE,
            qm.CONTEXT_MAX_AGE_SECONDS,
        ):
            assert gauge._multiprocess_mode == "max"

    def test_every_gauge_in_src_declares_a_mode(self):
        """Scan, so a gauge added in a future change cannot skip this quietly."""
        offenders = []
        for path in _SRC_PROXY.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"=\s*Gauge\(", text):
                # Take the constructor call up to its closing paren at column 0.
                tail = text[match.end():]
                end = tail.find("\n)")
                body = tail[: end if end != -1 else 400]
                if "multiprocess_mode" not in body:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line}")
        assert not offenders, (
            "Gauge(s) without an explicit multiprocess_mode — under "
            "PROMETHEUS_MULTIPROC_DIR these default to 'all', which appends a `pid` "
            f"label and breaks dashboards/readiness parsing: {offenders}"
        )
