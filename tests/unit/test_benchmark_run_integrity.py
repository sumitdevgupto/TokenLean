"""The public benchmark must not report a result it did not get (2026-09-18).

Found running the local stack on a cold start. A Docker Desktop self-update restarted the
engine mid-run, every one of 36 requests failed, and the runner's only defence was "zero
successes = error": ONE success would have produced a savings figure from the survivors, a
last_run.json that looked like a result, and exit 0. Around it:

  * the warm-up could fail and the run carried on ("Continuing"), and it was too short (86
    tokens vs G01's 200) to reach the LLMLingua sidecar it claimed to warm, so the sidecar's
    ~9 s cold load landed on the first TIMED prose request;
  * the quality gate dropped failed requests from its denominator, and counted the five
    tool-intent records (`expected_facts: []`) as passes, so "36/36" was really 31;
  * run.ps1 exited 0 whatever the runner returned, and had no config pin at all;
  * run_ab.py printed ERROR for a failed pair and exited 0.

No network: a mock transport plays the proxy. No gitignored file is read or written.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "examples" / "benchmark"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(BENCH))
    spec.loader.exec_module(mod)
    return mod


rb = _load("run_benchmark")
pin = _load("pin_config")
run_ab = _load("run_ab")

DATASET = [json.loads(ln) for ln in (BENCH / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
           if ln.strip()]


def _label_of(body):
    for r in DATASET:
        if r["messages"] == body.get("messages"):
            return r["_label"]
    return None


def _answer_for(label):
    """An answer that contains every curated fact of the record (first alternative of each)."""
    rec = next((r for r in DATASET if r["_label"] == label), None)
    facts = (rec or {}).get("expected_facts") or []
    return " ".join(f[0] if isinstance(f, list) else f for f in facts) or "ok"


class FakeProxy:
    def __init__(self, fail=(), refuse_once=(), disconnect=(), healthy=True):
        self.fail, self.refuse_once, self.disconnect = set(fail), set(refuse_once), set(disconnect)
        self.healthy = healthy
        self.seen: dict = {}
        self.timed_posts = 0

    def handle(self, request):
        if request.url.path == "/health":
            if not self.healthy:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"status": "ok"})
        if not self.healthy:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content)
        if body["messages"][0]["content"] == rb._WARMUP_PROSE:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}}],
                                             "_token_opt": {"step_savings": {"G01": {"abs_saving": 90}}}})
        label = _label_of(body)
        self.seen[label] = self.seen.get(label, 0) + 1
        self.timed_posts += 1
        self.temperatures = getattr(self, "temperatures", []) + [body.get("temperature", "absent")]
        if label in self.refuse_once and self.seen[label] == 1:
            raise httpx.ConnectError("[WinError 10061] refused", request=request)
        if label in self.disconnect:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.",
                                            request=request)
        if label in self.fail:
            return httpx.Response(500, json={"error": "upstream"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": _answer_for(label)}}],
            "_token_opt": {"baseline_tokens": 100, "final_tokens_sent": 40,
                           "step_savings": {"G19": {"abs_saving": 60}}}})


@pytest.fixture
def run(monkeypatch, tmp_path, capsys):
    """Run run_benchmark.main() against a FakeProxy; returns (exit_code, artifact, output)."""
    def _run(fake, *argv):
        transport = httpx.MockTransport(fake.handle)
        real_client = httpx.Client
        monkeypatch.setattr(rb.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
        monkeypatch.setattr(rb.httpx, "get", lambda url, **kw: real_client(transport=transport).get(url))
        monkeypatch.setattr(rb.time, "sleep", lambda *_: None)
        monkeypatch.setattr(rb, "OUT", tmp_path / "result.json")
        monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--api-key", "tok-test",
                                          "--proxy-url", "http://proxy.test", *argv])
        rc = rb.main()
        out = capsys.readouterr().out
        artifact = json.loads(rb.OUT.read_text(encoding="utf-8")) if rb.OUT.exists() else None
        return rc, artifact, out
    return _run


class TestAFailedRequestIsNeverAResult:
    def test_a_complete_run_exits_zero_and_says_complete(self, run):
        rc, art, _ = run(FakeProxy(), "--limit", "5")
        assert rc == rb.EXIT_OK
        assert art["complete"] is True and art["failed_requests"] == 0 and art["requests"] == 5

    def test_one_failed_request_makes_the_whole_run_incomplete(self, run):
        rc, art, out = run(FakeProxy(fail={"faq: gdpr regions"}), "--limit", "5")
        assert rc == rb.EXIT_INCOMPLETE, "one dead request out of five used to exit 0"
        assert art["complete"] is False and art["failed_requests"] == 1
        assert art["failures"][0]["label"] == "faq: gdpr regions"
        assert "TOTAL TOKEN SAVINGS   n/a" in out, "an incomplete run must not print a headline figure"

    def test_a_request_that_never_reached_the_proxy_is_sent_again_once(self, run):
        fake = FakeProxy(refuse_once={"faq: deploy pending"})
        rc, art, _ = run(fake, "--limit", "5")
        assert rc == rb.EXIT_OK and art["requests"] == 5
        assert fake.seen["faq: deploy pending"] == 2

    def test_a_dropped_connection_is_not_resent(self, run):
        """It may already have run the pipeline (and stored a cache entry the retry would then
        'hit'), so re-sending it could manufacture a saving. It is a failure instead."""
        fake = FakeProxy(disconnect={"faq: deploy pending"})
        rc, art, _ = run(fake, "--limit", "5")
        assert fake.seen["faq: deploy pending"] == 1
        assert rc == rb.EXIT_INCOMPLETE and art["failed_requests"] == 1

    def test_no_timed_request_is_sent_until_the_warmup_is_served(self, run):
        fake = FakeProxy(healthy=False)
        rc, art, out = run(fake, "--limit", "5", "--warmup-timeout", "1")
        assert rc == rb.EXIT_ERROR
        assert fake.timed_posts == 0, "the timed run must not start against a proxy that is not serving"
        assert art is None, "nothing measured, so nothing may be written"


class TestTheQualityGateCountsWhatHappened:
    def test_a_failed_fact_record_is_a_failed_check_not_a_skipped_one(self, run):
        rc, _, out = run(FakeProxy(fail={"faq: gdpr regions"}), "--limit", "5", "--quality-check")
        assert "Facts: 4/5" in out, "the unanswered record must stay in the denominator"
        assert "no answer - request failed" in out
        assert rc == rb.EXIT_INCOMPLETE, "incomplete outranks the gate verdict"

    def test_records_without_curated_facts_are_not_counted_as_passes(self, run):
        rc, _, out = run(FakeProxy(), "--quality-check")
        with_facts = sum(1 for r in DATASET if r.get("expected_facts") or r.get("forbidden"))
        assert with_facts < len(DATASET), "fixture assumption: some records carry no facts"
        assert f"Facts: {with_facts}/{with_facts}" in out
        assert rc == rb.EXIT_OK

    def test_the_gate_still_fails_a_missing_fact(self, run, monkeypatch):
        monkeypatch.setattr(sys.modules[__name__], "_answer_for", lambda label: "no facts here")
        rc, _, out = run(FakeProxy(), "--limit", "5", "--quality-check")
        assert rc == rb.EXIT_QUALITY_FAIL and "QUALITY GATE: FAIL" in out


class TestTheGateMeasuresTheProxyNotTheSampler:
    """2026-09-18, measured with the A/B harness on the three records that flipped between two
    consecutive --quality-check runs (70+ paired samples each). At the provider-default temperature
    the runner used to send, the DIRECT arm - no proxy at all - missed those facts about as often
    as the proxy arm (api-504 37/40 direct vs 39/40 proxy; ci-build 39/40 vs 36/40; prose 39/40
    vs 39/40). At temperature 0 neither arm missed once in 40. The flakiness was the sampler."""

    def test_temperature_zero_is_sent_by_default(self, run):
        fake = FakeProxy()
        run(fake, "--limit", "5")
        assert fake.temperatures and set(fake.temperatures) == {0.0}

    def test_none_restores_the_provider_default(self, run):
        fake = FakeProxy()
        rc, art, _ = run(fake, "--limit", "3", "--temperature", "none")
        assert set(fake.temperatures) == {"absent"} and art["temperature"] is None

    def test_a_nonsense_temperature_is_refused_before_anything_is_sent(self, run):
        fake = FakeProxy()
        rc, _, _ = run(fake, "--limit", "3", "--temperature", "warm")
        assert rc == rb.EXIT_ERROR and fake.timed_posts == 0

    def test_a_miss_on_a_truncated_answer_says_so(self, run, monkeypatch):
        """Still counted as a failure; but "ran out of max_tokens" is told apart from "dropped"."""
        real = FakeProxy.handle

        def truncating(self, request):
            resp = real(self, request)
            if request.url.path != "/health" and _label_of(json.loads(request.content)):
                d = json.loads(resp.content)
                d["choices"][0]["message"]["content"] = "cut off before the facts"
                d["choices"][0]["finish_reason"] = "length"
                return httpx.Response(200, json=d)
            return resp
        monkeypatch.setattr(FakeProxy, "handle", truncating)
        rc, _, out = run(FakeProxy(), "--limit", "3", "--quality-check")
        assert rc == rb.EXIT_QUALITY_FAIL and "[truncated at max_tokens]" in out


class TestTheWarmupReachesTheSidecar:
    def test_the_warmup_crosses_g01s_token_floor(self):
        sys.path.insert(0, str(REPO / "src" / "proxy"))
        from savings.calculator import count_messages_tokens
        floor = yaml.safe_load((REPO / "config" / "config.yaml.template").read_text(encoding="utf-8")
                               )["groups"]["G1_compression"]["min_tokens_to_compress"]
        tokens = count_messages_tokens([{"role": "user", "content": rb._WARMUP_PROSE}], "gpt-4o-mini")
        assert tokens >= floor * 1.1, (
            f"warm-up is {tokens} tokens; G01 skips anything under {floor}, and then the sidecar's "
            "cold model load lands on the first timed prose request")

    def test_sidecar_warmup_waits_for_a_slow_server_but_not_for_an_absent_one(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(rb.time, "time", lambda: clock["t"])
        monkeypatch.setattr(rb.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

        def refused(*a, **k):
            clock["t"] += 1.0
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(rb.httpx, "post", refused)
        ok, why = rb.warm_sidecar("http://sidecar.test/compress", timeout=600, connect_grace=30)
        assert not ok and "ConnectError" in why
        assert clock["t"] < 60, "an absent sidecar must cost the grace period, not the full budget"


class TestTheSharedPin:
    TEMPLATE = yaml.safe_load((REPO / "config" / "config.yaml.template").read_text(encoding="utf-8"))

    def _pin(self, operator=None):
        import copy
        return pin.build(copy.deepcopy(self.TEMPLATE), operator, "openai")[0]

    def test_the_measured_groups_are_pinned(self):
        g = self._pin()["groups"]
        for k in ("G1_compression", "G5_cache", "G6_routing", "G8_tools", "G19_headroom",
                  "g22_deduplication"):
            assert g[k]["enabled"] is True, k
        assert g["G28_ccr"]["enabled"] is False
        assert g["G1_compression"]["sidecar_url"] == "http://llmlingua:8080/compress"

    def test_langfuse_follows_the_operator_not_the_template(self):
        """The template ships tracing OFF, so pinning it switched tracing off for every
        benchmark run and left the Langfuse dashboards empty for exactly that traffic."""
        assert self.TEMPLATE["groups"]["G18_observability"]["langfuse_enabled"] is False
        op = {"groups": {"G18_observability": {"langfuse_enabled": True,
                                               "capture_trace_content": False}}}
        g18 = self._pin(op)["groups"]["G18_observability"]
        assert g18["langfuse_enabled"] is True and g18["capture_trace_content"] is False

    def test_nothing_that_can_move_a_number_is_inherited(self):
        op = {"groups": {"G1_compression": {"enabled": False},
                         "G18_observability": {"reasoning_rate_multiplier": 9.9,
                                               "langfuse_enabled": True}},
              "rate_limit": {"default": {"requests_per_minute": 1}}}
        c = self._pin(op)
        assert c["groups"]["G1_compression"]["enabled"] is True
        assert c["groups"]["G18_observability"]["reasoning_rate_multiplier"] == \
            self.TEMPLATE["groups"]["G18_observability"]["reasoning_rate_multiplier"]
        assert c["rate_limit"]["default"]["requests_per_minute"] == 100000
        assert set(pin.OBSERVABILITY_CARRY_OVER) == {"langfuse_enabled", "capture_trace_content"}

    def test_without_an_operator_config_it_is_the_template_value(self):
        assert self._pin(None)["groups"]["G18_observability"]["langfuse_enabled"] is False


class TestTheLaunchers:
    SH = (BENCH / "run.sh").read_text(encoding="utf-8")
    PS = (BENCH / "run.ps1").read_text(encoding="utf-8")

    def test_both_launchers_pin_through_the_same_module(self):
        for name, text in (("run.sh", self.SH), ("run.ps1", self.PS)):
            assert "examples/benchmark/pin_config.py" in text, f"{name} does not use the shared pin"
        assert "python - <<'PY'\nimport yaml" not in self.SH, "the old inline pin is back"

    def test_both_launchers_hand_back_the_runners_exit_status(self):
        assert '|| rc=$?' in self.SH and 'exit "$rc"' in self.SH
        assert "$rc = $LASTEXITCODE" in self.PS and self.PS.rstrip().endswith("exit $rc")

    def test_the_config_is_pinned_before_the_stack_is_started(self):
        """Pinning after `up -d` restarted a cold proxy mid-warm-up - two cold starts."""
        for text in (self.SH, self.PS):
            assert text.index("pin_config.py") < text.index("up -d"), "pin must precede up -d"

    def test_both_launchers_warm_the_sidecar(self):
        for text in (self.SH, self.PS):
            assert "--sidecar-url" in text and "http://localhost:8080/compress" in text

    @pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
    @pytest.mark.parametrize("layout", [
        ".config.yaml.prepin/4242.yaml",   # current: inside the gitignored directory
        ".config.yaml.prepin.4242",        # 2026-09-17: PID-suffixed sibling file
        ".config.yaml.prepin",             # before that: one stable file
    ])
    def test_restore_recovers_another_runs_backup_in_every_layout(self, tmp_path, layout):
        """`--restore` tested for THIS run's PID-named backup, which never exists - so it
        reported "nothing to restore" in exactly the case it is for. A backup stranded by any
        earlier version of the launcher must also still come back."""
        (tmp_path / "config").mkdir()
        (tmp_path / "examples" / "benchmark").mkdir(parents=True)
        shutil.copy(BENCH / "run.sh", tmp_path / "examples" / "benchmark" / "run.sh")
        (tmp_path / "config" / "config.yaml").write_text("pinned\n", encoding="utf-8")
        backup = tmp_path / "config" / layout
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text("original\n", encoding="utf-8")
        proc = subprocess.run(["bash", "examples/benchmark/run.sh", "--restore"], cwd=tmp_path,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / "config" / "config.yaml").read_text(encoding="utf-8") == "original\n"
        assert not list((tmp_path / "config").glob(".config.yaml.prepin*"))

    @pytest.mark.skipif(not shutil.which("git") or not (REPO / ".git").exists(), reason="needs the git checkout")
    def test_every_launchers_backup_path_is_gitignored(self):
        """HIGH, 2026-09-18. The backup holds the operator's LIVE config. 096fb4d renamed it to
        `config/.config.yaml.prepin.<pid>` while the ignore line it added covers only
        `config/.config.yaml.prepin` - and checkin-push.sh stages the public repo with
        `git add -A`. Checked against the launchers' own path strings, instantiated."""
        import re
        sh_path = re.search(r'^BACKUP_DIR="([^"]+)"', self.SH, re.M).group(1) + "/12345.yaml"
        assert re.search(r'^ORIG_BACKUP="\$BACKUP_DIR/\$\$\.yaml"', self.SH, re.M)
        ps_dir = re.search(r'^\$backupDir = "([^"]+)"', self.PS, re.M).group(1)
        assert re.search(r'^\$origBackup = "\$backupDir/\$PID\.yaml"', self.PS, re.M)
        for path in (sh_path, ps_dir + "/12345.yaml"):
            proc = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=REPO)
            assert proc.returncode == 0, f"{path} is NOT gitignored - a leftover backup would be published"


class TestTheAbHarness:
    def test_a_never_sent_request_is_resent_and_a_dropped_one_is_not(self, monkeypatch):
        calls = {"n": 0}

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "_token_opt": {"tokens_provider_billed": 5, "response_tokens": 3}}

        def refused_then_ok(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("refused")
            return _Resp()
        monkeypatch.setattr(run_ab.httpx, "post", refused_then_ok)
        monkeypatch.setattr(run_ab, "wait_for_health", lambda *a, **k: True)
        out = run_ab.call_proxy("http://proxy.test", "tok", "gpt-4o-mini",
                                [{"role": "user", "content": "hi"}], 8, {}, None, 5.0)
        assert calls["n"] == 2 and out["content"] == "ok"

        calls["n"] = 0

        def dropped(url, **kw):
            calls["n"] += 1
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        monkeypatch.setattr(run_ab.httpx, "post", dropped)
        with pytest.raises(httpx.RemoteProtocolError):
            run_ab.call_proxy("http://proxy.test", "tok", "gpt-4o-mini",
                              [{"role": "user", "content": "hi"}], 8, {}, None, 5.0)
        assert calls["n"] == 1

    def test_an_errored_pair_fails_the_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(run_ab, "RESULTS", tmp_path / "ab.json")
        monkeypatch.setattr(run_ab, "COST_LOG", tmp_path / "cost.jsonl")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        answer = {"content": "x", "prompt_tokens": 10, "completion_tokens": 2, "tool_calls": [],
                  "finish_reason": "stop", "cache_read_tokens": None, "cache_write_tokens": None,
                  "assistant_msg": {"role": "assistant", "content": "x"}}
        monkeypatch.setattr(run_ab, "call_direct", lambda *a, **k: dict(answer))
        n = {"b": 0}

        def proxy_arm(*a, **k):
            n["b"] += 1
            if n["b"] == 2:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return {**answer, "routed_model": "gpt-4o-mini", "cache_hit": False, "groups_fired": ()}
        monkeypatch.setattr(run_ab, "call_proxy", proxy_arm)
        monkeypatch.setattr(sys, "argv", ["run_ab.py", "--api-key", "tok", "--no-warmup",
                                          "--mode", "cold", "--limit", "3"])
        assert run_ab.main() == run_ab.EXIT_INCOMPLETE, "an errored pair used to exit 0"
        meta = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))["meta"]
        assert meta["complete"] is False and len(meta["errored_pairs"]) == 1
