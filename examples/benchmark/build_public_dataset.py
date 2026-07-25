#!/usr/bin/env python3
"""
Deterministic builder for the A/B benchmark's *recognized-standard* dataset.

Pulls items VERBATIM from public, clean-licensed datasets and emits three
checked-in artifacts consumed by ``run_ab.py``:

  public_dataset.jsonl   the canonical UNIQUE item set (cold mode runs it once,
                         in file order). Each record carries provenance + grading.
  replay_schedule.json   an ordered list of refs with repeat/paraphrase markers
                         (replay mode expands it to model production traffic).
  public_dataset.meta.json   seed, per-corpus revisions, counts, repeat rates,
                         sha256 of public_dataset.jsonl, and build_source.

Recognized datasets (see DATA_LICENSES.md for the pinned revisions + licenses):
  rag     SQuAD v2            (CC BY-SA 4.0)   gold-answer facts gate
  chat    MT-Bench            (Apache-2.0)     judge-only (8 categories)
  swe     SWE-bench Lite      (permissive)     gold-patch paths/symbols facts gate
  code    HumanEval           (MIT)            judge / opt-in exec pass@1
  reason  GSM8K               (MIT)            final-numeric-answer facts gate

We invent NO question content: the HF path (``--hf`` / auto when the `datasets`
library is importable) reads each corpus at a PINNED revision. The ``--from-fixture``
path builds from a small local sample and is used for offline unit tests and to
ship a STRUCTURAL placeholder artifact; that placeholder is clearly marked
``build_source: "fixture"`` in the meta and MUST be regenerated from Hugging Face
(``python build_public_dataset.py --hf``) before any headline scorecard is published.

Determinism: a single ``random.Random(seed)`` drives all sampling/ordering, and
pinned revisions freeze the upstream content, so two builds with the same seed +
source are byte-identical.

Usage
-----
  python build_public_dataset.py --hf                 # real build from Hugging Face
  python build_public_dataset.py --from-fixture ../..  # offline/test build
  python build_public_dataset.py --seed 42 --counts rag=30,chat=20,swe=20,code=15,reason=15
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_FIXTURE = REPO / "tests" / "data" / "ab_corpus_fixture.json"

# Pinned Hugging Face revisions (commit shas / stable tags). Verify + update in
# DATA_LICENSES.md whenever these move. MT-Bench is not a HF `datasets` set; its
# question.jsonl ships in the FastChat repo at the pinned tag below.
PINNED = {
    "rag":    {"repo": "rajpurkar/squad_v2",         "revision": "main",   "license": "CC BY-SA 4.0"},
    "chat":   {"repo": "lm-sys/FastChat",            "revision": "main",   "license": "Apache-2.0"},
    "swe":    {"repo": "princeton-nlp/SWE-bench_Lite","revision": "main",   "license": "SWE-bench (permissive research)"},
    "code":   {"repo": "openai_humaneval",           "revision": "main",   "license": "MIT"},
    "reason": {"repo": "gsm8k",                       "revision": "main",   "license": "MIT"},
}

DEFAULT_COUNTS = {"rag": 30, "chat": 20, "swe": 20, "code": 15, "reason": 15}

# Proxy per-request controls mirrored from the calibrated dataset: the A/B measures
# the safe, quality-preserving stages, and opts out of the slow one-time embedding
# downloads the harness does not need. Semantic cache stays ON here (unlike the
# single-arm calibrated run) so replay-mode near-duplicates exercise G05 L2/G22.
X_CONTROLS = {"x_jit_retrieval": False}

MAX_TOKENS = 256
SWE_CONTEXT_CHARS = 4000  # bound the injected code context so a request stays cheap


# --------------------------------------------------------------------------- #
# Fact extraction (deterministic, no LLM)
# --------------------------------------------------------------------------- #
def extract_swe_facts(patch: str) -> list:
    """Touched file basenames + changed def/class names from a unified diff.
    These are what a faithful 'which files/functions change?' answer must mention."""
    paths, symbols = [], []
    for line in (patch or "").splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            p = line[6:].strip()
            if p and p != "/dev/null":
                paths.append(p)
        elif line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3 and parts[2].startswith("a/"):
                paths.append(parts[2][2:])
        elif line.startswith("+") and not line.startswith("+++"):
            m = re.match(r"\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", line)
            if m:
                symbols.append(m.group(1))
    paths = list(dict.fromkeys(Path(p).name for p in paths))
    symbols = list(dict.fromkeys(symbols))
    facts = paths[:2] + symbols[:2]
    return facts


def extract_gsm8k_answer(answer: str) -> str:
    """GSM8K gold answers end with '#### <number>'."""
    m = re.search(r"####\s*([\-0-9,\.]+)", answer or "")
    return m.group(1).replace(",", "").strip() if m else ""


# --------------------------------------------------------------------------- #
# Per-profile builders — each takes raw records + rng + count -> request dicts
# --------------------------------------------------------------------------- #
def _rec(profile, n, source, messages, *, grade, expected_facts=None):
    r = {
        "request_id": f"{profile}-{n:04d}",
        "_label": f"{profile}-{n:04d}",
        "_profile": profile,
        "_source": source,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "grade": grade,
        **X_CONTROLS,
    }
    if expected_facts:
        r["expected_facts"] = expected_facts
    return r


def _src(profile, rid):
    p = PINNED[profile]
    return {"corpus": p["repo"], "hf_revision": p["revision"], "record_id": str(rid), "license": p["license"]}


def build_rag(raw, rng, count):
    # answerable-only, seeded sample, gold-answer facts gate.
    pool = [r for r in raw if (r.get("answers") or {}).get("text")]
    rng.shuffle(pool)
    out = []
    for n, r in enumerate(pool[:count], 1):
        ctx, q = r["context"], r["question"]
        golds = [t for t in r["answers"]["text"] if t.strip()]
        facts = [golds] if len(golds) > 1 else golds[:1]  # OR-group when multiple
        msgs = [
            {"role": "system", "content": "Answer the question using ONLY the context. Be concise."},
            {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {q}"},
        ]
        out.append(_rec("rag", n, _src("rag", r.get("id", n)), msgs, grade="facts", expected_facts=facts))
    return out


def build_chat(raw, rng, count):
    # MT-Bench: first user turn only (single-shot for A/B fairness), judge-graded.
    pool = list(raw)
    rng.shuffle(pool)
    out = []
    for n, r in enumerate(pool[:count], 1):
        turns = r.get("turns") or []
        if not turns:
            continue
        msgs = [{"role": "user", "content": turns[0]}]
        src = _src("chat", r.get("question_id", n))
        src["category"] = r.get("category", "")
        out.append(_rec("chat", n, src, msgs, grade="judge"))
    return out


def build_swe(raw, rng, count):
    pool = list(raw)
    rng.shuffle(pool)
    out = []
    for n, r in enumerate(pool[:count], 1):
        problem = r.get("problem_statement", "")
        context = (r.get("text_context") or r.get("text") or "")[:SWE_CONTEXT_CHARS]
        facts = extract_swe_facts(r.get("patch", ""))
        msgs = [
            {"role": "system", "content": "You are a senior engineer. Given an issue and code context, "
                                          "state which file(s) and function(s) must change and why. Be specific."},
            {"role": "user", "content": f"Issue:\n{problem}\n\nCode context:\n{context}"},
        ]
        out.append(_rec("swe", n, _src("swe", r.get("instance_id", n)), msgs,
                        grade="facts", expected_facts=facts or None))
    return out


def build_code(raw, rng, count):
    pool = list(raw)
    rng.shuffle(pool)
    out = []
    for n, r in enumerate(pool[:count], 1):
        prompt = r.get("prompt", "")
        entry = r.get("entry_point", "")
        msgs = [
            {"role": "system", "content": "Complete the Python function. Return only the code."},
            {"role": "user", "content": prompt},
        ]
        src = _src("code", r.get("task_id", n))
        # entry_point kept for the optional --exec-humaneval pass@1 in run_ab.py.
        src["entry_point"] = entry
        src["test"] = r.get("test", "")
        src["canonical_solution"] = r.get("canonical_solution", "")
        out.append(_rec("code", n, src, msgs, grade="judge",
                        expected_facts=[entry] if entry else None))
    return out


def build_reason(raw, rng, count):
    pool = list(raw)
    rng.shuffle(pool)
    out = []
    for n, r in enumerate(pool[:count], 1):
        q = r.get("question", "")
        gold = extract_gsm8k_answer(r.get("answer", ""))
        msgs = [
            {"role": "system", "content": "Solve the problem. End your reply with 'Answer: <number>'."},
            {"role": "user", "content": q},
        ]
        out.append(_rec("reason", n, _src("reason", r.get("id", n)), msgs,
                        grade="facts", expected_facts=[gold] if gold else None))
    return out


BUILDERS = {"rag": build_rag, "chat": build_chat, "swe": build_swe,
            "code": build_code, "reason": build_reason}


# --------------------------------------------------------------------------- #
# Replay schedule — disclosed repeats + paraphrases modelling production traffic
# --------------------------------------------------------------------------- #
PARAPHRASE_PREFIXES = ["Quick question - ", "Hey, ", "Follow-up: ", "One more - "]


def build_replay_schedule(items, rng, seed):
    """Every ORIGINAL first (shuffled), THEN injected verbatim repeats + paraphrases
    for a subset of the cacheable profiles (rag/chat), plus a repeated swe context.

    Ordering invariant: all originals precede any repeat/paraphrase. This lets a
    SINGLE replay pass yield both numbers without cache cross-contamination —
    cold = the first-occurrence 'original' entries (genuine cache misses), replay =
    the whole run (repeats/paraphrases land as hits). run_ab.py relies on it."""
    originals = [{"ref": it["request_id"], "kind": "original"} for it in items]
    rng.shuffle(originals)

    cacheable = [it["request_id"] for it in items if it["_profile"] in ("rag", "chat")]
    rng.shuffle(cacheable)
    # ~40% of cacheable items get a verbatim repeat (G05 L1); ~25% a paraphrase (G05 L2 / G22).
    n_repeat = max(1, int(0.40 * len(cacheable)))
    n_para = max(1, int(0.25 * len(cacheable)))
    extras = []
    for rid in cacheable[:n_repeat]:
        extras.append({"ref": rid, "kind": "repeat"})
    for i, rid in enumerate(cacheable[:n_para]):
        extras.append({"ref": rid, "kind": "paraphrase",
                       "prefix": PARAPHRASE_PREFIXES[i % len(PARAPHRASE_PREFIXES)]})
    # A repeated swe context exercises long-context cache/compression.
    swe = [it["request_id"] for it in items if it["_profile"] == "swe"]
    if swe:
        extras.append({"ref": swe[0], "kind": "repeat"})
    rng.shuffle(extras)

    entries = originals + extras  # originals ALWAYS before their repeats
    return {"seed": seed, "entries": entries,
            "counts": {"originals": len(items), "repeats": n_repeat + (1 if swe else 0),
                       "paraphrases": n_para}}


# --------------------------------------------------------------------------- #
# Source loading — Hugging Face (real) or local fixture (offline/tests)
# --------------------------------------------------------------------------- #
def load_from_fixture(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: data.get(k, []) for k in BUILDERS}


def load_from_hf():  # pragma: no cover - requires network + `datasets`
    from datasets import load_dataset
    raw = {}
    raw["rag"] = [dict(r) for r in load_dataset(
        PINNED["rag"]["repo"], split="validation", revision=PINNED["rag"]["revision"])]
    raw["swe"] = [dict(r) for r in load_dataset(
        PINNED["swe"]["repo"], split="test", revision=PINNED["swe"]["revision"])]
    raw["code"] = [dict(r) for r in load_dataset(
        PINNED["code"]["repo"], split="test", revision=PINNED["code"]["revision"])]
    raw["reason"] = [dict(r) for r in load_dataset(
        PINNED["reason"]["repo"], "main", split="test", revision=PINNED["reason"]["revision"])]
    # MT-Bench question.jsonl lives in the FastChat repo, not the datasets hub.
    from huggingface_hub import hf_hub_download
    qpath = hf_hub_download(repo_id=PINNED["chat"]["repo"], repo_type="space",
                            filename="fastchat/llm_judge/data/mt_bench/question.jsonl",
                            revision=PINNED["chat"]["revision"])
    raw["chat"] = [json.loads(ln) for ln in Path(qpath).read_text(encoding="utf-8").splitlines() if ln.strip()]
    return raw


# --------------------------------------------------------------------------- #
def _ascii(obj):
    """Force ASCII so the artifacts read under any locale (cp1252 on Windows)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the recognized-standard A/B dataset.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--hf", action="store_true", help="build from Hugging Face at pinned revisions")
    ap.add_argument("--from-fixture", default=None,
                    help="build from a local fixture JSON (offline; ships the structural placeholder)")
    ap.add_argument("--counts", default=None, help="override, e.g. rag=30,chat=20,swe=20,code=15,reason=15")
    ap.add_argument("--out-dir", default=str(HERE))
    args = ap.parse_args()

    counts = dict(DEFAULT_COUNTS)
    if args.counts:
        for part in args.counts.split(","):
            k, _, v = part.partition("=")
            if k.strip() in counts:
                counts[k.strip()] = int(v)

    if args.hf and not args.from_fixture:
        raw, build_source = load_from_hf(), "huggingface"
    else:
        fixture = args.from_fixture or str(DEFAULT_FIXTURE)
        raw, build_source = load_from_fixture(fixture), "fixture"

    rng = random.Random(args.seed)
    items = []
    for profile in ("rag", "chat", "swe", "code", "reason"):  # stable order
        items.extend(BUILDERS[profile](raw.get(profile, []), rng, counts[profile]))

    schedule = build_replay_schedule(items, rng, args.seed)

    def _write_lf(path, text):
        # Write bytes with LF newlines so the artifact is byte-identical on every
        # OS (Windows text mode would translate \n -> \r\n and break the sha match).
        path.write_bytes(text.encode("utf-8"))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ds_path = out_dir / "public_dataset.jsonl"
    ds_text = "\n".join(_ascii(it) for it in items) + "\n"
    _write_lf(ds_path, ds_text)

    _write_lf(out_dir / "replay_schedule.json",
              json.dumps(schedule, ensure_ascii=True, indent=2, sort_keys=True) + "\n")

    sha = hashlib.sha256(ds_text.encode("utf-8")).hexdigest()
    per_profile = {}
    for it in items:
        per_profile[it["_profile"]] = per_profile.get(it["_profile"], 0) + 1
    meta = {
        "build_source": build_source,
        "seed": args.seed,
        "counts": per_profile,
        "total_items": len(items),
        "replay_counts": schedule["counts"],
        "dataset_sha256": sha,
        "pinned": PINNED,
        "note": ("STRUCTURAL PLACEHOLDER built from the offline fixture. Regenerate with "
                 "`python build_public_dataset.py --hf` (needs `pip install datasets huggingface_hub`) "
                 "to pull the recognized-standard items verbatim before publishing any headline number."
                 if build_source == "fixture" else
                 "Built from Hugging Face at the pinned revisions above."),
    }
    _write_lf(out_dir / "public_dataset.meta.json",
              json.dumps(meta, ensure_ascii=True, indent=2, sort_keys=True) + "\n")

    print(f"built {len(items)} items ({build_source}) -> {ds_path.name}  sha256={sha[:12]}…")
    print(f"  per-profile: {per_profile}")
    print(f"  replay: {schedule['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
