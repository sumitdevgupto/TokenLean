"""Offline unit tests for the A/B benchmark harness (examples/benchmark/).

No network, no LLM calls, no Docker. Validates the builder's determinism, the
checked-in dataset artifacts, pricing, provider detection, the require-direct
guard, cold/replay trace loading, the relative facts gate, per-provider spend
caps, and per-provider×mode aggregation."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
BENCH = REPO / "examples" / "benchmark"
FIXTURE = REPO / "tests" / "data" / "ab_corpus_fixture.json"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(BENCH))
    spec.loader.exec_module(mod)
    return mod


run_ab = _load("run_ab")
builder = _load("build_public_dataset")


# --------------------------------------------------------------------------- #
# Builder determinism + artifact shape
# --------------------------------------------------------------------------- #
def _build(tmp, seed=42):
    subprocess.run([sys.executable, str(BENCH / "build_public_dataset.py"),
                    "--from-fixture", str(FIXTURE), "--seed", str(seed),
                    "--out-dir", str(tmp)], check=True, cwd=str(REPO),
                   capture_output=True)
    return (tmp / "public_dataset.jsonl").read_bytes()


def test_builder_deterministic_same_seed(tmp_path):
    a = _build(tmp_path / "a", 42)
    b = _build(tmp_path / "b", 42)
    assert a == b, "same seed + fixture must produce byte-identical output"


def test_builder_differs_on_seed(tmp_path):
    a = _build(tmp_path / "a", 42)
    b = _build(tmp_path / "b", 7)
    assert a != b, "a different seed should change the sampling/order"


def test_checked_in_artifacts_present_and_shaped():
    ds = BENCH / "public_dataset.jsonl"
    meta = json.loads((BENCH / "public_dataset.meta.json").read_text(encoding="utf-8"))
    items = [json.loads(ln) for ln in ds.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert items, "public_dataset.jsonl must be non-empty"
    profiles = {it["_profile"] for it in items}
    assert profiles == {"rag", "chat", "swe", "code", "reason"}, "all five profiles present"
    for it in items:
        assert it["max_tokens"] and "_source" in it and "request_id" in it
        src = it["_source"]
        assert {"corpus", "hf_revision", "record_id", "license"} <= set(src)
    # meta sha matches the file bytes (LF-stable, cross-platform)
    import hashlib
    sha = hashlib.sha256(ds.read_bytes()).hexdigest()
    assert sha == meta["dataset_sha256"], "meta dataset_sha256 must match the file"


def test_checked_in_dataset_is_ascii():
    (BENCH / "public_dataset.jsonl").read_text(encoding="ascii")  # raises on non-ASCII


def test_rag_corpus_is_production_realistic():
    """The RAG lever only reproduces if prompts are large enough to compress. HotpotQA
    distractor stuffs 10 paragraphs (~1-2k tokens) per question, well above G01's
    min_tokens_to_compress=200 floor — the SQuAD single-paragraph corpus sat below it,
    which is why the old cold-floor RAG saving was ~0%. Guard that the shipped corpus
    stays large."""
    rag = [json.loads(ln) for ln in
           (BENCH / "public_dataset.jsonl").read_text(encoding="utf-8").splitlines()
           if ln.strip() and json.loads(ln)["_profile"] == "rag"]
    assert rag, "must ship rag items"
    approx_tok = [sum(len(m.get("content", "")) for m in it["messages"]) // 4 for it in rag]
    approx_tok.sort()
    median = approx_tok[len(approx_tok) // 2]
    assert median >= 400, (
        f"RAG median ~{median} tok is below the compression floor — the cold-floor RAG "
        "lever will not fire; the corpus must be production-realistic multi-doc context")


def test_build_rag_normalises_hotpot_and_filters_yes_no():
    """build_rag handles the HotpotQA dict-context shape and drops yes/no comparison
    answers (a bare 'yes' is a spurious substring match that voids the facts gate)."""
    import random
    raw = [
        {"id": "h1", "question": "Q1", "answer": "Illinois",
         "context": {"title": ["A", "B"], "sentences": [["s1.", "s2."], ["t1.", "t2."]]}},
        {"id": "h2", "question": "Q2", "answer": "yes",  # must be filtered out
         "context": {"title": ["C"], "sentences": [["u1."]]}},
    ]
    out = builder.build_rag(raw, random.Random(0), 10)
    assert len(out) == 1, "yes/no answers must be excluded"
    it = out[0]
    assert it["expected_facts"] == ["Illinois"]
    ctx = it["messages"][1]["content"]
    assert "A" in ctx and "s1." in ctx and "t1." in ctx, "multi-doc paragraphs must be stuffed"


def test_replay_schedule_refs_exist_and_originals_first():
    items = {json.loads(ln)["request_id"]
             for ln in (BENCH / "public_dataset.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()}
    sched = json.loads((BENCH / "replay_schedule.json").read_text(encoding="utf-8"))
    seen = set()
    for e in sched["entries"]:
        assert e["ref"] in items, f"schedule ref {e['ref']} not in dataset"
        if e["kind"] != "original":
            assert e["ref"] in seen, "a repeat/paraphrase must follow its original"
        else:
            seen.add(e["ref"])


# --------------------------------------------------------------------------- #
# Cache-burst lever (Phase 1) — reproduces the published cache number
# --------------------------------------------------------------------------- #
def test_cache_schedule_shape_and_invariant():
    """Checked-in cache_schedule.json: refs exist, all cacheable (rag/chat),
    originals-before-repeats invariant, multiplicity/warm_frac match meta."""
    items = {json.loads(ln)["request_id"]: json.loads(ln)
             for ln in (BENCH / "public_dataset.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()}
    sched = json.loads((BENCH / "cache_schedule.json").read_text(encoding="utf-8"))
    meta = json.loads((BENCH / "public_dataset.meta.json").read_text(encoding="utf-8"))
    seen = set()
    for e in sched["entries"]:
        assert e["ref"] in items, f"cache ref {e['ref']} not in dataset"
        assert items[e["ref"]]["_profile"] in ("rag", "chat"), "cache burst is rag/chat only"
        if e["kind"] == "original":
            seen.add(e["ref"])
        else:
            assert e["ref"] in seen, "a warm repeat must follow its cold original"
    n_orig = sum(1 for e in sched["entries"] if e["kind"] == "original")
    n_warm = sum(1 for e in sched["entries"] if e["kind"] == "repeat")
    assert n_warm == n_orig * sched["multiplicity"], "each original gets `multiplicity` warm repeats"
    assert meta["cache_burst"]["multiplicity"] == sched["multiplicity"]
    assert meta["cache_burst"]["warm_frac"] == sched["warm_frac"]


def test_cache_builder_deterministic_and_multiplicity():
    items = [json.loads(ln) for ln in
             (BENCH / "public_dataset.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    import random
    a = builder.build_cache_burst_schedule(items, random.Random(1042), 42, 5)
    b = builder.build_cache_burst_schedule(items, random.Random(1042), 42, 5)
    assert a == b, "same rng + multiplicity must be byte-identical"
    assert a["multiplicity"] == 5
    assert a["counts"]["warm_repeats"] == a["counts"]["originals"] * 5


def test_builder_emits_cache_schedule(tmp_path):
    """The full build emits cache_schedule.json alongside the other artifacts."""
    _build(tmp_path, 42)  # runs build_public_dataset.py --from-fixture
    cs = tmp_path / "cache_schedule.json"
    assert cs.exists(), "build must emit cache_schedule.json"
    sched = json.loads(cs.read_text(encoding="utf-8"))
    assert sched["entries"] and sched["multiplicity"] >= 1


def test_workload_cache_wiring():
    """Guard the cache workload: run_ab reads CACHE_SCHEDULE and sends x_cache_semantic:false
    to isolate the exact-cache lever (so the ~90% number carries 0 quality loss)."""
    src = (BENCH / "run_ab.py").read_text(encoding="utf-8")
    assert 'CACHE_SCHEDULE' in src and 'cache_schedule.json' in src
    # cache pass = the cache schedule, caching on but semantic OFF (isolate exact L1 lever)
    assert '"cache", load_trace("replay", items, cache_sched)' in src, \
        "cache workload must run the cache schedule"
    assert '{"x_cache_semantic": False}' in src, \
        "cache workload must disable semantic cache to isolate the exact-cache lever"
    assert '--workload' in src and '"standard", "cache"' in src


def test_aggregate_cache_slice_and_warm_hits():
    """A cache-slice record set (1 cold miss + N warm hits) aggregates to ~warm-frac savings."""
    recs = [_rec("cache", "original", "rag", 200, 200, False)] + \
           [_rec("cache", "repeat", "rag", 200, 0, True) for _ in range(9)]
    agg = run_ab.aggregate(recs)["openai"]
    assert "cache" in agg
    t = agg["cache"]["total"]
    assert t["b_calls"] == 10 and t["cache_hits"] == 9
    assert t["token_saving_pct"] == 90.0, "9/10 warm hits -> 90% provider-token savings"


def test_render_shows_per_profile(capsys):
    """render() prints the per-profile breakdown (transparency into where savings come from)."""
    recs = [_rec("replay", "original", "rag", 200, 100, False),
            _rec("replay", "original", "chat", 100, 80, False)]
    agg = run_ab.aggregate(recs)
    meta = {"prices_as_of": "2026-07-25", "seed": 42, "dataset_sha256": "deadbeef" * 8,
            "providers_run": ["openai"], "providers_skipped": {}, "spend_total_usd": 0.0}
    run_ab.render(agg, meta)
    out = capsys.readouterr().out
    assert "rag" in out and "chat" in out, "per-profile rows must be shown"


# --------------------------------------------------------------------------- #
# Multi-turn agentic engine (Phase 2)
# --------------------------------------------------------------------------- #
def _scripted_arm(turns_script):
    """Build an arm_fn that returns each dict in turns_script on successive calls,
    recording the convo roles it saw each turn."""
    seen = []
    it = iter(turns_script)

    def arm(convo, tools):
        seen.append([m.get("role") for m in convo])
        return next(it)
    arm.seen = seen
    return arm


def _turn(content="", pt=100, ct=10, tool=None):
    tcs = ([{"id": "call_0", "function": {"name": tool, "arguments": "{}"}}] if tool else [])
    return {"content": content, "prompt_tokens": pt, "completion_tokens": ct, "cache_hit": False,
            "routed_model": "gpt-4o-mini", "tool_calls": tcs,
            "assistant_msg": {"role": "assistant", "content": content or None,
                              **({"tool_calls": [{"id": "call_0", "type": "function",
                                                  "function": {"name": tool, "arguments": "{}"}}]} if tool else {})}}


def test_run_episode_tool_loop():
    """Two-turn episode: tool_call → local exec (tool result injected) → final answer.
    Provider-billed tokens are summed across the whole episode."""
    arm = _scripted_arm([_turn(tool="list_logs", pt=100, ct=20),
                         _turn(content="Done: 3 errors", pt=150, ct=10)])
    ep = run_ab.run_episode(arm, [{"role": "user", "content": "check logs"}],
                            tools=[{"type": "function", "function": {"name": "list_logs"}}],
                            tool_results={"list_logs": {"errors": 3}})
    assert ep["turns"] == 2
    assert ep["prompt_tokens"] == 250 and ep["completion_tokens"] == 30, "tokens summed per episode"
    assert ep["content"] == "Done: 3 errors"
    assert [t["function"]["name"] for t in ep["tool_calls"]] == ["list_logs"]
    assert arm.seen[1] == ["user", "assistant", "tool"], "tool result injected before turn 2"


def test_run_episode_max_turns():
    """An arm that never stops calling tools is bounded by max_turns (no infinite loop)."""
    arm = _scripted_arm([_turn(tool="loop") for _ in range(20)])
    ep = run_ab.run_episode(arm, [{"role": "user", "content": "go"}],
                            tools=[{"type": "function", "function": {"name": "loop"}}],
                            tool_results={"loop": {}}, max_turns=4)
    assert ep["turns"] == 4, "episode stops at max_turns"


def test_norm_tool_calls_dicts_and_objects():
    class _Fn:  # SDK-like object
        name, arguments = "get_health", '{"svc":"api"}'

    class _TC:
        id, function = "call_9", _Fn()
    got = run_ab._norm_tool_calls([_TC(),
                                   {"id": "call_1", "function": {"name": "list", "arguments": "{}"}}])
    assert got[0] == {"id": "call_9", "function": {"name": "get_health", "arguments": '{"svc":"api"}'}}
    assert got[1]["function"]["name"] == "list"
    # missing id gets a synthesised one
    assert run_ab._norm_tool_calls([{"function": {"name": "x", "arguments": "{}"}}])[0]["id"] == "call_0"


def test_call_signatures_accept_tools():
    import inspect
    assert "tools" in inspect.signature(run_ab.call_direct).parameters
    assert "tools" in inspect.signature(run_ab.call_proxy).parameters


def test_call_proxy_retries_on_429(monkeypatch):
    """G00 rate-limiting is a throughput guard, not a savings lever: a 429 (e.g. the
    cache burst against an un-pinned proxy) must be retried with backoff and served,
    not dropped as an ERROR that corrupts the measurement."""
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}],
                    "_token_opt": {"tokens_provider_billed": 5, "response_tokens": 3,
                                   "cache_hit": False, "routed_model": "gpt-4o-mini"}}

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _Resp(429 if calls["n"] < 3 else 200)

    monkeypatch.setattr(run_ab.httpx, "post", _fake_post)
    monkeypatch.setattr(run_ab.time, "sleep", lambda *_: None)  # no real backoff wait
    out = run_ab.call_proxy("http://localhost:4000", "tok-x", "gpt-4o-mini",
                            [{"role": "user", "content": "hi"}], 64, {}, "bench", 30.0)
    assert calls["n"] == 3, "must retry the two 429s then succeed on the third"
    assert out["prompt_tokens"] == 5 and out["content"] == "ok"


def test_pin_raises_g00_burst_headroom():
    """The benchmark config pin (run.sh) must lift G00's rate limit so the ~500-request
    cache burst is not 429'd — otherwise arm B is throttled and the measurement is invalid."""
    src = (BENCH / "run.sh").read_text(encoding="utf-8")
    assert "rate_limit" in src and "requests_per_minute" in src, \
        "run.sh pin must widen G00 rate-limit headroom for the cache burst"


def test_workload_agentic_wiring():
    """Guard: the agentic workload runs multi-turn episodes on both arms."""
    src = (BENCH / "run_ab.py").read_text(encoding="utf-8")
    assert "AGENTIC_DATASET" in src and "agentic_dataset.jsonl" in src
    assert '"standard", "cache", "agentic"' in src
    assert 'slice_name == "agentic"' in src and "run_episode(" in src
    assert 'agentic_dataset.jsonl missing' in src, "must guard a missing agentic pack"


# --------------------------------------------------------------------------- #
# Agentic dataset (BFCL-derived) + tool-trajectory gate (Phase 3)
# --------------------------------------------------------------------------- #
def test_agentic_dataset_shape():
    """Each agentic item: >10 real BFCL tools (-> G08/G16 pruning), a >800-token system
    prompt (-> G16 cap), tool_results for every tool, well-formed OpenAI tool schemas,
    Apache-2.0 provenance."""
    ds = BENCH / "agentic_dataset.jsonl"
    items = [json.loads(ln) for ln in ds.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert items, "agentic_dataset.jsonl must be non-empty"
    for it in items:
        assert it["_profile"] == "agentic" and "_label" in it and "request_id" in it
        assert it["_source"]["license"] == "Apache-2.0"
        assert it["_source"]["corpus"].startswith("BFCL")
        tools = it["tools"]
        assert len(tools) > 10, "tool catalogue must exceed the pruning threshold"
        for t in tools:
            assert t["type"] == "function"
            fn = t["function"]
            assert fn["name"] and fn["parameters"]["type"] == "object"
        # every tool has a mock result for loop continuation
        names = {t["function"]["name"] for t in tools}
        assert names <= set(it["tool_results"]), "each tool needs a mock result"
        sysmsg = next(m["content"] for m in it["messages"] if m["role"] == "system")
        assert len(sysmsg) // 4 > 800, "system prompt must exceed the G16 cap"
        assert it["messages"][-1]["role"] == "user" and it["messages"][-1]["content"]
    # ASCII-safe (cross-platform)
    ds.read_text(encoding="ascii")


def test_relative_tool_gate():
    """Proxy dropping a tool the direct arm called -> fail; identical/superset -> pass."""
    def tc(*names):
        return [{"id": f"c{i}", "function": {"name": n, "arguments": "{}"}} for i, n in enumerate(names)]
    # proxy dropped 'mv' that the direct arm used
    r = run_ab.relative_tool_gate(tc("ls", "mv", "cat"), tc("ls", "cat"))
    assert r["graded"] and not r["passed"] and r["dropped_tools"] == ["mv"]
    # identical trajectory passes
    assert run_ab.relative_tool_gate(tc("ls", "cat"), tc("ls", "cat"))["passed"]
    # proxy calling MORE tools than direct still passes (no drop)
    assert run_ab.relative_tool_gate(tc("ls"), tc("ls", "cat"))["passed"]
    # direct made no tool calls -> not graded
    assert run_ab.relative_tool_gate([], tc("ls"))["graded"] is False


def test_agentic_pin_enables_g16():
    """The launcher's pinned config must enable G16 tool pruning + system-prompt cap so the
    agentic lever actually fires (safe for standard/cache: no tools, tiny system prompts)."""
    sh = (BENCH / "run.sh").read_text(encoding="utf-8")
    assert "G16_agent_arch" in sh and "max_tools_per_agent" in sh and "max_system_prompt_tokens" in sh


def test_agentic_builder_script_present():
    assert (BENCH / "build_agentic_dataset.py").exists(), "reproducible agentic builder must ship"


# --------------------------------------------------------------------------- #
# Illustrative production-mix blend (Phase 4)
# --------------------------------------------------------------------------- #
def test_parse_weights_default_and_override():
    assert run_ab.parse_weights("") == run_ab.DEFAULT_WEIGHTS
    w = run_ab.parse_weights("cache=0.5,agentic=0.1")
    assert w["cache"] == 0.5 and w["agentic"] == 0.1
    assert w["prose"] == run_ab.DEFAULT_WEIGHTS["prose"], "unspecified weights keep the default"


def test_parse_weights_rejects_bad_input():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        run_ab.parse_weights("cache=-1")            # negative
    with _pytest.raises(ValueError):
        run_ab.parse_weights("bogus=0.5")           # unknown lever
    with _pytest.raises(ValueError):
        run_ab.parse_weights("cache=0,prose=0,agentic=0,reasoning=0")  # all zero


def _blend_agg():
    # cold prose/reasoning (stateless, SMALL) is deliberately different from replay (cache-on,
    # LARGE) so we can assert the blend reads prose/reasoning from COLD, not replay (no double-count).
    return run_ab.aggregate(
        [_rec("cache", "repeat", "rag", 200, 0, True) for _ in range(9)]
        + [_rec("cache", "original", "rag", 200, 200, False),
           _rec("cold", "original", "rag", 1000, 950, False),     # cold prose ~5% (stateless)
           _rec("cold", "original", "chat", 1000, 960, False),
           _rec("cold", "original", "reason", 1000, 1000, False),
           _rec("replay", "original", "rag", 1000, 500, False),   # replay prose ~50% (cache-on)
           _rec("replay", "original", "chat", 1000, 600, False),
           _rec("agentic", "original", "agentic", 10000, 7000, False)])


def test_blend_reads_prose_from_cold_not_replay():
    """Fix guard: prose/reasoning levers come from the COLD (stateless) slice so cache is
    NOT double-counted against the separate cache lever."""
    b = run_ab.blend(_blend_agg(), run_ab.parse_weights(""))["openai"]
    assert set(b["levers"]) == {"cache", "agentic", "prose", "reasoning"}
    assert b["levers"]["cache"] == 90.0
    # prose from COLD (~5%), NOT replay (~45%) — the double-count guard
    assert b["levers"]["prose"] < 10, "prose must be the stateless cold number, not the cache-on replay"
    # cache-heavier weights raise the blend
    hi = run_ab.blend(_blend_agg(), run_ab.parse_weights("cache=0.7,prose=0.1,agentic=0.1,reasoning=0.1"))["openai"]
    assert hi["token_saving_pct"] > b["token_saving_pct"]


def test_blend_renormalises_on_missing_lever():
    """A partial run (no agentic slice) still blends honestly over present levers."""
    recs = [_rec("cache", "repeat", "rag", 200, 0, True),
            _rec("cold", "original", "rag", 1000, 950, False)]
    b = run_ab.blend(run_ab.aggregate(recs), run_ab.parse_weights(""))["openai"]
    assert "agentic" not in b["levers"] and "cache" in b["levers"] and "prose" in b["levers"]
    assert b["token_saving_pct"] > 0


def test_workload_full_wiring():
    src = (BENCH / "run_ab.py").read_text(encoding="utf-8")
    assert '"standard", "cache", "agentic", "full"' in src
    assert 'args.workload == "full"' in src and "render_blend(" in src
    assert "ILLUSTRATIVE" in src and "--weights" in src


# --------------------------------------------------------------------------- #
# Fact extraction
# --------------------------------------------------------------------------- #
def test_extract_swe_facts():
    patch = ("diff --git a/lib/parser.py b/lib/parser.py\n"
             "--- a/lib/parser.py\n+++ b/lib/parser.py\n"
             "@@ -1 +1,3 @@\n+def tokenize(source):\n+    return []\n")
    facts = builder.extract_swe_facts(patch)
    assert "parser.py" in facts and "tokenize" in facts


def test_extract_gsm8k_answer():
    assert builder.extract_gsm8k_answer("... so the total is 72.\n#### 72") == "72"
    assert builder.extract_gsm8k_answer("#### 1,024") == "1024"


# --------------------------------------------------------------------------- #
# Pricing + PROVIDER_MODELS coverage
# --------------------------------------------------------------------------- #
def test_price_math_and_prefix_strip():
    prices = run_ab.load_prices()
    assert run_ab.price("gpt-4o-mini", 1_000_000, 0, prices) == pytest.approx(0.15)
    # provider/ prefix is stripped before lookup
    assert run_ab.price("azure/gpt-4o-mini", 1_000_000, 0, prices) == pytest.approx(0.15)


def test_price_unknown_model_raises():
    with pytest.raises(KeyError):
        run_ab.price("totally-unknown-model", 1, 1, run_ab.load_prices())


def test_all_provider_models_priced():
    prices = run_ab.load_prices()
    assert len(run_ab.PROVIDER_MODELS) == 10, "10 first-class providers"
    for prov, spec in run_ab.PROVIDER_MODELS.items():
        for model in [spec["model"]] + spec["routes"]:
            run_ab.price(model, 1, 1, prices)  # raises if missing


# --------------------------------------------------------------------------- #
# Provider detection + require-direct
# --------------------------------------------------------------------------- #
def test_detect_providers_key_and_extra_config():
    d = run_ab.detect_providers({"OPENAI_API_KEY": "sk-x"})
    assert d["openai"]["configured"] and not d["anthropic"]["configured"]
    # LLM_KEY_ fallback works
    assert run_ab.detect_providers({"LLM_KEY_ANTHROPIC": "x"})["anthropic"]["configured"]
    # bedrock needs AWS extras, azure needs endpoint
    assert not run_ab.detect_providers({"AWS_ACCESS_KEY_ID": "x"})["bedrock"]["configured"]
    assert not run_ab.detect_providers({"AZURE_API_KEY": "x"})["azure"]["configured"]
    assert run_ab.detect_providers(
        {"AZURE_API_KEY": "x", "AZURE_API_BASE": "https://e"})["azure"]["configured"]


def test_require_direct_hard_fails_without_key(monkeypatch):
    # Clear any provider keys from the ambient env.
    for k in list(run_ab.os.environ):
        if k.endswith("_API_KEY") or k.startswith("LLM_KEY_") or k.startswith("AWS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "argv",
                        ["run_ab.py", "--require-direct", "--providers", "openai",
                         "--api-key", "tok-x", "--proxy-url", "http://x"])
    assert run_ab.main() == 1  # hard error, before any spend


# --------------------------------------------------------------------------- #
# Trace loading
# --------------------------------------------------------------------------- #
def test_load_trace_cold_vs_replay():
    items = run_ab.load_items()
    sched = json.loads((BENCH / "replay_schedule.json").read_text(encoding="utf-8"))
    cold = run_ab.load_trace("cold", items, sched)
    rep = run_ab.load_trace("replay", items, sched)
    assert all(kind == "original" for kind, _ in cold)
    assert len(rep) > len(cold), "replay includes repeats/paraphrases"
    # paraphrase actually mutates the user message
    para = [it for kind, it in rep if kind == "paraphrase"]
    if para:
        assert any(m["role"] == "user" for m in para[0]["messages"])


# --------------------------------------------------------------------------- #
# Relative facts gate
# --------------------------------------------------------------------------- #
def test_relative_facts_gate():
    item = {"expected_facts": ["paris"]}
    assert not run_ab.relative_facts_gate(item, "it is paris", "no idea")["passed"], "B drops A's fact -> fail"
    assert run_ab.relative_facts_gate(item, "no idea", "no idea")["passed"], "both miss -> pass"
    assert run_ab.relative_facts_gate(item, "it is paris", "yes, paris")["passed"], "both have it -> pass"
    assert run_ab.relative_facts_gate({}, "x", "y")["passed"], "no facts -> trivially pass"


# --------------------------------------------------------------------------- #
# Spend cap
# --------------------------------------------------------------------------- #
def test_spend_meter_per_provider_and_overall():
    sm = run_ab.SpendMeter(per_provider_cap=0.01, overall_cap=0.05)
    sm.add("openai", 0.02)
    assert sm.provider_tripped("openai") and not sm.provider_tripped("anthropic")
    assert not sm.overall_tripped()
    sm.add("anthropic", 0.04)
    assert sm.overall_tripped()


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _rec(slice_name, kind, profile, a_pt, b_pt, hit, facts_ok=True):
    prices = run_ab.load_prices()
    return {"provider": "openai", "slice": slice_name, "kind": kind, "profile": profile,
            "a": {"prompt_tokens": a_pt, "completion_tokens": 50,
                  "cost": run_ab.price("gpt-4o-mini", a_pt, 50, prices)},
            "b": {"prompt_tokens": b_pt, "completion_tokens": 0 if hit else 50,
                  "cost": 0.0 if hit else run_ab.price("gpt-4o-mini", b_pt, 50, prices),
                  "cache_hit": hit, "routed_model": "gpt-4o-mini"},
            "facts": {"graded": True, "passed": facts_ok}}


def test_aggregate_cold_replay_and_cache_hits():
    # Two-pass reality: the cold pass (G05 bypassed → no hits) and the replay pass
    # (originals populate, a repeat hits) produce SEPARATE records tagged by `slice`.
    recs = [
        _rec("cold", "original", "rag", 1000, 600, False),
        _rec("cold", "original", "chat", 800, 500, False, facts_ok=False),
        _rec("replay", "original", "rag", 1000, 600, False),
        _rec("replay", "original", "chat", 800, 500, False, facts_ok=False),
        _rec("replay", "repeat", "rag", 1000, 0, True),
    ]
    agg = run_ab.aggregate(recs, "both")["openai"]
    cold, replay = agg["cold"]["total"], agg["replay"]["total"]
    assert cold["b_calls"] == 2 and cold["cache_hits"] == 0
    assert replay["b_calls"] == 3 and replay["cache_hits"] == 1
    assert replay["token_saving_pct"] > cold["token_saving_pct"], "cache lifts replay above cold"
    assert cold["facts_checked"] == 2 and cold["facts_regressed"] == 1


def test_cache_hit_contributes_zero_arm_b_tokens():
    recs = [_rec("replay", "repeat", "rag", 1000, 0, True)]
    total = run_ab.aggregate(recs, "replay")["openai"]["replay"]["total"]
    assert total["b_prompt"] == 0, "a cache hit means 0 provider tokens on arm B"
    assert total["token_saving_pct"] == 100.0


# --------------------------------------------------------------------------- #
# Structural
# --------------------------------------------------------------------------- #
def test_run_ab_has_main():
    assert callable(getattr(run_ab, "main", None))


def test_cold_pass_bypasses_cache():
    """The cold floor must be a TRUE stateless floor: the cold pass forces x_no_cache so
    G05 is fully bypassed (no semantic-collision hits inflating savings / serving a
    neighbour's answer, no cache residue for the replay pass). Guards the two-pass design."""
    src = (BENCH / "run_ab.py").read_text(encoding="utf-8")
    assert '("cold", load_trace("cold", items, schedule), {"x_no_cache": True})' in src, \
        "cold pass must run originals with x_no_cache=True (G05 bypassed)"
    assert '("replay", load_trace("replay", items, schedule), {})' in src, \
        "replay pass must run the full schedule with caching ON"
    # records carry an authoritative slice tag consumed by aggregate()
    assert '"slice": slice_name' in src and 'r.get("slice") != slice_name' in src


def test_run_ab_doc_present():
    doc = (BENCH / "run_ab.md").read_text(encoding="utf-8")
    assert "--providers" in doc and "run.sh --ab" in doc, "run_ab.md must document usage"


def test_launchers_autoexport_env_keys():
    """Guard the multi-key .env auto-export in both launchers so a multi-provider
    `--ab --providers all` keeps working from a .env (regression guard)."""
    sh = (BENCH / "run.sh").read_text(encoding="utf-8")
    ps = (BENCH / "run.ps1").read_text(encoding="utf-8")
    for text in (sh, ps):
        assert "LLM_KEY_" in text and "AWS_REGION_NAME" in text, \
            "launcher must export LLM_KEY_* (+ azure/bedrock extras) from .env in --ab mode"
        assert "PROXY_API_KEY=" in text, \
            "launcher must read a fixed PROXY_API_KEY from .env (no runtime key needed)"


def test_launchers_flush_effective_tenant():
    """Guard the effective-tenant cache flush: the launcher must flush the tenant the
    KEY resolves to (non-admin key ignores our X-Tenant-ID and runs under its own
    tenant), else cold mode reads stale cache. Regression guard for the NOVA-STG-01
    contamination where the flush hit `bench` but the run executed under NOVA-STG-01."""
    sh = (BENCH / "run.sh").read_text(encoding="utf-8")
    ps = (BENCH / "run.ps1").read_text(encoding="utf-8")
    # bash resolver + PS resolver, each consulting both key stores.
    assert "resolve_effective_tenant" in sh and 'FLUSH_TENANT=' in sh, \
        "run.sh must resolve the effective tenant before flushing"
    assert "Resolve-EffectiveTenant" in ps, \
        "run.ps1 must resolve the effective tenant before flushing"
    for text in (sh, ps):
        assert "local-keys.json" in text and "proxy_keys" in text, \
            "resolver must consult both the OSS blob and the commercial Postgres key store"
        assert "FROM proxy_keys WHERE key_hash" in text, \
            "resolver must look the key hash up in the commercial proxy_keys store"
