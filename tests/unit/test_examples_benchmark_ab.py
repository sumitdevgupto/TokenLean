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
def _rec(kind, profile, a_pt, b_pt, hit, facts_ok=True):
    prices = run_ab.load_prices()
    return {"provider": "openai", "kind": kind, "profile": profile,
            "a": {"prompt_tokens": a_pt, "completion_tokens": 50,
                  "cost": run_ab.price("gpt-4o-mini", a_pt, 50, prices)},
            "b": {"prompt_tokens": b_pt, "completion_tokens": 0 if hit else 50,
                  "cost": 0.0 if hit else run_ab.price("gpt-4o-mini", b_pt, 50, prices),
                  "cache_hit": hit, "routed_model": "gpt-4o-mini"},
            "facts": {"graded": True, "passed": facts_ok}}


def test_aggregate_cold_replay_and_cache_hits():
    recs = [_rec("original", "rag", 1000, 600, False),
            _rec("original", "chat", 800, 500, False, facts_ok=False),
            _rec("repeat", "rag", 1000, 0, True)]
    agg = run_ab.aggregate(recs, "both")["openai"]
    cold, replay = agg["cold"]["total"], agg["replay"]["total"]
    assert cold["b_calls"] == 2 and cold["cache_hits"] == 0
    assert replay["b_calls"] == 3 and replay["cache_hits"] == 1
    assert replay["token_saving_pct"] > cold["token_saving_pct"], "cache lifts replay above cold"
    assert cold["facts_checked"] == 2 and cold["facts_regressed"] == 1


def test_cache_hit_contributes_zero_arm_b_tokens():
    recs = [_rec("repeat", "rag", 1000, 0, True)]
    total = run_ab.aggregate(recs, "replay")["openai"]["replay"]["total"]
    assert total["b_prompt"] == 0, "a cache hit means 0 provider tokens on arm B"
    assert total["token_saving_pct"] == 100.0


# --------------------------------------------------------------------------- #
# Structural
# --------------------------------------------------------------------------- #
def test_run_ab_has_main():
    assert callable(getattr(run_ab, "main", None))
