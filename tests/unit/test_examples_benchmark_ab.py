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
