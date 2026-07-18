#!/usr/bin/env python3
"""
Token-savings benchmark — single-tenant, mixed production-like workload.

Sends the requests in ``dataset.jsonl`` through a running Token Optimisation proxy
and reports the aggregate token savings, using the SAME metric as the project's
headline result: the proxy's own per-request ``_token_opt`` block
(``baseline_tokens`` — the un-optimised counterfactual — vs ``final_tokens_sent``).

The workload is shaped so that *each* safe, quality-preserving stage of the pipeline
actually fires, not just the response cache:
  G05 response cache      — repeated FAQ traffic
  G19 structured pruning  — messages whose whole content is JSON / logs / code
  G22 dedup               — consecutive duplicate context turns (an authoritative copy is kept)
  G08 lazy tool loading   — requests carrying a large tools[] array; only relevant tools kept
  G06 model routing       — simple queries sent to a capable model, routed down to a cheap one
                            (this one shows in the cost line, not the token breakdown)

This is a *reproducible, single-tenant* proof that the optimisation pipeline works
on your own key — not a re-run of the internal 8-dataset ablation that produced the
55.78% headline. Your number will vary with the dataset and your provider.

Prerequisites
-------------
  1. Local stack running:   docker compose up -d         (proxy + redis + postgres
                            + llmlingua sidecar; a provider key configured in the proxy)
  2. A proxy-issued key:    export PROXY_API_KEY=tok-...
  3. (optional) proxy URL:  export PROXY_URL=http://localhost:4000

Usage
-----
  python run_benchmark.py [--limit N] [--model gpt-4o-mini] [--proxy-url URL]

See examples/benchmark/README.md for the methodology and the calibrated result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("ERROR: httpx is required — `pip install httpx` (it ships with the proxy requirements).")

DATASET = Path(__file__).parent / "dataset.jsonl"
OUT = Path(__file__).parent / "last_run.json"
BAR_W = 14

# Friendly names for the per-group savings breakdown.
GROUP_NAMES = {
    "G05": "G05 response cache",
    "G06": "G06 model routing",
    "G08": "G08 lazy tool loading",
    "G19": "G19 structured pruning",
    "G22": "G22 dedup",
    "G01": "G01 compression",
    "G02": "G02 templates",
    "G07": "G07 retrieval",
    "G09": "G09 schema",
    "G11": "G11 output format",
    "G14": "G14 tool output",
    "G21": "G21 cache alignment",
}


def _bar(frac: float) -> str:
    return "#" * max(0, round(frac * BAR_W))


def check_facts(answer, expected_facts=None, forbidden=None):
    """Deterministic ground-truth check of a single answer — no LLM call.

    Standalone copy of the pitch harness's gate (this OSS bundle must not import
    the internal harness). Catches the failure mode cosine similarity misses: a
    "generic but on-topic" answer that drops the policy facts (GDPR regions,
    refund terms, the password self-service flow).

    `expected_facts` items are a string (case-insensitive substring) or a list
    (OR-group; any one member satisfies). `forbidden` strings must NOT appear.
    Returns {passed, missing, present_forbidden}; an answer with no curated
    facts trivially passes.
    """
    text = (answer or "").lower()
    missing = []
    for item in (expected_facts or []):
        if isinstance(item, (list, tuple)):
            if not any(str(opt).lower() in text for opt in item):
                missing.append(list(item))
        elif str(item).lower() not in text:
            missing.append(item)
    present_forbidden = [f for f in (forbidden or []) if str(f).lower() in text]
    return {
        "passed": not missing and not present_forbidden,
        "missing": missing,
        "present_forbidden": present_forbidden,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-tenant token-savings benchmark.")
    ap.add_argument("--proxy-url", default=os.environ.get("PROXY_URL", "http://localhost:4000"))
    ap.add_argument("--api-key", default=os.environ.get("PROXY_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("BENCHMARK_MODEL", "gpt-4o-mini"))
    ap.add_argument("--limit", type=int, default=0, help="cap number of requests (0 = all)")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-request timeout (s)")
    ap.add_argument("--warmup-timeout", type=float, default=600.0,
                    help="timeout for the one-time warmup request (s)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the warmup request (only if the proxy is already warm)")
    ap.add_argument("--tenant", default=os.environ.get("BENCHMARK_TENANT", "BENC-STG-01"),
                    help="X-Tenant-ID to run under — isolates the benchmark's cache/state "
                         "under t:<tenant>: so cleanup deletes only its keys (default: BENC-STG-01)")
    ap.add_argument("--quality-check", action="store_true",
                    help="Assert each answer's curated expected_facts / forbidden "
                         "(from dataset.jsonl). No extra LLM cost. Prints a PASS/FAIL line "
                         "per checked answer and exits non-zero if the facts gate fails — "
                         "proves the savings did not degrade answer quality.")
    ap.add_argument("--judge", action="store_true",
                    help="Opt-in deeper check: ask a judge model whether each answer "
                         "faithfully and correctly addresses the question (1-5 score). "
                         "Costs a few extra cents; needs OPENAI_API_KEY or LLM_KEY_OPENAI.")
    ap.add_argument("--judge-model", default=os.environ.get("QUALITY_JUDGE_MODEL", "gpt-4o-mini"),
                    help="Model for --judge (default gpt-4o-mini; use gpt-4o for sign-off).")
    args = ap.parse_args()

    if not args.api_key:
        return _fail("set PROXY_API_KEY to your proxy-issued key (e.g. tok-...). See README.md.")
    if not DATASET.exists():
        return _fail(f"dataset not found: {DATASET}")

    reqs = [json.loads(ln) for ln in DATASET.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.limit:
        reqs = reqs[: args.limit]

    url = args.proxy_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
        # Run under a dedicated tenant so all cache/state is namespaced under
        # t:<tenant>: and the cleanup can delete exactly the benchmark's keys.
        "X-Tenant-ID": args.tenant,
    }

    tot_base = tot_sent = 0
    cost_base = cost_act = 0.0
    per_group: dict[str, int] = defaultdict(int)
    cache_hits = n_ok = 0
    quality_rows: list[dict] = []  # facts gate results, when --quality-check
    t0 = time.time()

    # Warm up: the first request that needs embeddings lazily downloads + loads the
    # sentence-transformer model (BAAI/bge-small-en-v1.5, used by G05 semantic cache
    # / G22 dedup). That load is CPU-heavy and blocks the proxy, so without a warmup
    # the first timed requests time out. Do it once, up front, with a long timeout.
    if not args.no_warmup:
        print("Warming up the proxy (first run downloads the embedding model — this can take a")
        print("few minutes; subsequent requests are fast)...")
        try:
            with httpx.Client(timeout=args.warmup_timeout) as wc:
                # A prose message (>min_tokens) with x_compress_user warms the LLMLingua
                # sidecar too — its first compression lazily loads the model, which can
                # exceed G01's per-call budget and otherwise makes the first real prose
                # request skip compression. Warming it here keeps the timed run fast.
                wc.post(url, headers=headers, json={
                    "model": args.model,
                    "messages": [{"role": "user", "content": (
                        "Warmup: this is a deliberately verbose message whose only purpose "
                        "is to give the compression sidecar enough natural-language text to "
                        "load its model and return a compressed result before the real "
                        "benchmark requests begin, so that the first timed request does not "
                        "have to pay the one-time model-load cost all by itself today."
                    )}],
                    "max_tokens": 8,
                    # Same per-request skips the dataset uses, so warmup doesn't
                    # trigger G07 retrieval (a slow one-time embedding-model download)
                    # or semantic-cache embedding work the benchmark doesn't need.
                    "x_jit_retrieval": False,
                    "x_cache_semantic": False,
                    "x_compress_user": True,
                }).raise_for_status()
            print("  warmup complete.\n")
        except Exception as exc:  # noqa: BLE001
            print(f"  warmup request did not complete cleanly ({exc}). Continuing — "
                  "if the model is still loading, raise --timeout.\n")

    print(f"Sending {len(reqs)} requests to {url} (model default: {args.model}, tenant: {args.tenant})\n")
    with httpx.Client(timeout=args.timeout) as client:
        for i, req in enumerate(reqs, 1):
            label = req.get("_label", "")
            body = {"model": req.get("model", args.model), "messages": req["messages"]}
            if "max_tokens" in req:
                body["max_tokens"] = req["max_tokens"]
            if req.get("tools"):
                body["tools"] = req["tools"]          # G08 prunes these before the call
            # Forward per-request proxy controls (x_* keys: read by middleware,
            # stripped before the upstream call). e.g. x_complexity (G06 tier),
            # x_jit_retrieval (G07), x_cache_semantic (G05 L2/L3 opt-out).
            for k, v in req.items():
                if k.startswith("x_"):
                    body[k] = v
            try:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001 — report and continue
                print(f"  [{i}/{len(reqs)}] {label:<30} ERROR: {exc}")
                continue

            opt = data.get("_token_opt") or {}
            b = int(opt.get("baseline_tokens", 0) or 0)
            s = int(opt.get("final_tokens_sent", 0) or 0)
            tot_base += b
            tot_sent += s
            cost_base += float(opt.get("cost_baseline_usd", 0.0) or 0.0)
            cost_act += float(opt.get("cost_actual_usd", 0.0) or 0.0)
            if opt.get("cache_hit"):
                cache_hits += 1
            for g, st in (opt.get("step_savings") or {}).items():
                per_group[g] += int(st.get("abs_saving", 0) or 0)
            n_ok += 1
            pct = (100.0 * (b - s) / b) if b else 0.0
            print(f"  [{i}/{len(reqs)}] {label:<30} baseline={b:>6} sent={s:>6} saved={pct:>6.1f}%")

            if args.quality_check or args.judge:
                answer = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
                question = next((m["content"] for m in reversed(req["messages"])
                                 if m.get("role") == "user"), "")
                row = {"label": label, "question": question, "answer": answer}
                if args.quality_check and ("expected_facts" in req or "forbidden" in req):
                    fres = check_facts(answer, req.get("expected_facts"), req.get("forbidden"))
                    row["facts"] = fres
                    if not fres["passed"]:
                        print(f"        facts FAIL — missing={fres['missing']} "
                              f"forbidden_present={fres['present_forbidden']}")
                quality_rows.append(row)

    if n_ok == 0:
        return _fail("no successful requests — is the proxy up and the key valid?")

    dur = time.time() - t0
    saved = tot_base - tot_sent
    pct = (100.0 * saved / tot_base) if tot_base else 0.0
    cost_pct = (100.0 * (cost_base - cost_act) / cost_base) if cost_base else 0.0

    _render(args, reqs, n_ok, dur, cache_hits, tot_base, tot_sent, saved, pct,
            cost_base, cost_act, cost_pct, per_group)

    OUT.write_text(json.dumps({
        "requests": n_ok, "duration_s": round(dur, 1), "cache_hits": cache_hits,
        "baseline_tokens": tot_base, "tokens_sent": tot_sent,
        "tokens_saved": saved, "pct_saving": round(pct, 2),
        "cost_baseline_usd": round(cost_base, 6), "cost_actual_usd": round(cost_act, 6),
        "cost_pct_saving": round(cost_pct, 2),
        "per_group_tokens_saved": dict(sorted(per_group.items(), key=lambda x: -x[1])),
        "metric": "proxy _token_opt: baseline_tokens vs final_tokens_sent",
    }, indent=2), encoding="utf-8")
    print(f"  Full detail -> {OUT}")
    print("=" * 60)

    if args.quality_check or args.judge:
        return _quality_summary(args, quality_rows)
    return 0


def _quality_summary(args, rows: list) -> int:
    """Print the facts gate (and optional judge) result. Returns a non-zero exit
    code if the gate fails, so CI / run.sh surfaces a quality regression."""
    print()
    print("  QUALITY GATE")
    print("  " + "-" * 56)

    gate_ok = True

    if args.quality_check:
        checked = [r for r in rows if "facts" in r]
        passed = sum(1 for r in checked if r["facts"]["passed"])
        if checked:
            print(f"  Facts: {passed}/{len(checked)} checked answers contain their required "
                  f"policy facts (and no forbidden content)")
            for r in checked:
                if not r["facts"]["passed"]:
                    print(f"    FAIL  {r['label']:<28} missing={r['facts']['missing']} "
                          f"forbidden={r['facts']['present_forbidden']}")
            if passed < len(checked):
                gate_ok = False
        else:
            print("  Facts: no records with curated expected_facts were run "
                  "(try without --limit, or check dataset.jsonl).")

    if args.judge:
        scores = _run_judge(args, rows)
        if scores:
            mean = sum(scores) / len(scores)
            print(f"  Judge ({args.judge_model}): mean faithfulness {mean:.2f}/5 over "
                  f"{len(scores)} answers")
            if mean < 4.0:
                gate_ok = False
        else:
            print("  Judge: skipped (no judge API key — set OPENAI_API_KEY or LLM_KEY_OPENAI).")

    print("  " + "-" * 56)
    print(f"  QUALITY GATE: {'PASS' if gate_ok else 'FAIL'}")
    print("=" * 60)
    return 0 if gate_ok else 2


def _run_judge(args, rows: list) -> list:
    """Ask the judge model whether each answer faithfully and correctly addresses
    its question. Direct OpenAI call (never the proxy) so the judge prompt is not
    itself optimised. Returns a list of 1-5 scores (empty if no key)."""
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_KEY_OPENAI", "")
    if not api_key:
        return []
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    sys_prompt = (
        "You are a strict QA judge for a customer-support assistant. Given a user "
        "QUESTION and the assistant's ANSWER, rate whether the answer is correct, "
        "specific and helpful (not vague or generic). Respond ONLY with compact JSON: "
        '{"score": <int 1-5>, "reason": "<one sentence>"}. 5 = fully correct and specific; '
        "1 = wrong or uselessly generic."
    )
    scores: list = []
    with httpx.Client(timeout=60.0) as jc:
        for r in rows:
            try:
                resp = jc.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": args.judge_model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": f"QUESTION:\n{r['question']}\n\nANSWER:\n{r['answer']}"},
                        ],
                        "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
                obj = json.loads(content)
                sc = int(round(float(obj.get("score"))))
                scores.append(sc)
            except Exception:  # noqa: BLE001 — a judge hiccup shouldn't crash the benchmark
                continue
    return scores


def _render(args, reqs, n_ok, dur, cache_hits, tot_base, tot_sent, saved, pct,
            cost_base, cost_act, cost_pct, per_group) -> None:
    line = "=" * 60
    print("\n" + line)
    print("  TOKEN OPTIMISATION - BENCHMARK RESULT  (single-tenant)")
    print(line)
    print(f"  Dataset            {DATASET.name} ({n_ok} reqs)")
    print(f"  Proxy / model      {args.proxy_url} / {args.model}")
    print(f"  Duration           {dur:.1f}s     cache hits: {cache_hits}/{n_ok}")
    print()
    print(f"  Baseline tokens    {tot_base:>9,}   (un-optimised counterfactual)")
    print(f"  Tokens sent        {tot_sent:>9,}")
    print("  " + "-" * 56)
    print(f"  TOTAL TOKEN SAVINGS {pct:>6.1f}%   ({saved:,} tokens)")
    print("  " + "-" * 56)
    if cost_base > 0:
        print(f"  Est. cost savings   {cost_pct:>6.1f}%   (${cost_base:.4f} -> ${cost_act:.4f})")
        print("  (config-priced estimate; credits routing down-tier + token reduction.")
        print("   Directional, not invoice-grade — no provider-side cache/discounts.)")
    print()
    print("  Per-group contribution (tokens saved)")
    top = sorted(per_group.items(), key=lambda x: -x[1])
    shown = top[:8]
    rest = sum(v for _, v in top[8:])
    denom = saved if saved > 0 else 1
    for g, v in shown:
        frac = v / denom
        name = GROUP_NAMES.get(g, g)
        print(f"    {name:<24} {100*frac:>5.1f}%  {_bar(frac)}")
    if rest > 0:
        frac = rest / denom
        print(f"    {'(others)':<24} {100*frac:>5.1f}%  {_bar(frac)}")
    print()
    print("  Note: G06 routing saves cost (cheaper model), not input tokens, so it")
    print("  shows in the cost line above rather than the token breakdown.")
    print()
    print("  Method: aggregate of the proxy's own per-request _token_opt")
    print("  (baseline_tokens vs final_tokens_sent). Same metric as the internal")
    print("  8-dataset result (55.78%); this is one safe, reproducible workload.")
    print()


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
