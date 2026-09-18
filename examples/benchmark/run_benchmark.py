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
on your own key — not a re-run of the internal multi-dataset ablation behind the
project's headline figure. Your number will vary with the dataset and your provider.

Prerequisites
-------------
  1. Local stack running:   docker compose up -d         (proxy + redis + postgres
                            + llmlingua sidecar; a provider key configured in the proxy)
  2. A proxy-issued key:    export PROXY_API_KEY=tok-...
  3. (optional) proxy URL:  export PROXY_URL=http://localhost:4000

Usage
-----
  python run_benchmark.py [--limit N] [--model gpt-4o-mini] [--proxy-url URL]
                          [--sidecar-url http://localhost:8080/compress]

Exit codes
----------
  0  every request answered (and, with --quality-check, the facts gate passed)
  1  nothing measured: bad arguments, or the proxy never served the warm-up request
  2  complete run, quality gate FAILED
  3  INCOMPLETE run: at least one request failed, so the figures are not a result

See examples/benchmark/README.md for the methodology and the calibrated result.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

EXIT_OK, EXIT_ERROR, EXIT_QUALITY_FAIL, EXIT_INCOMPLETE = 0, 1, 2, 3

# The warm-up must reach G01, or it does not warm the LLMLingua sidecar: G01 skips any request
# under `min_tokens_to_compress` (200 in the template), and the old ~90-token warm-up never got
# there - so the sidecar's lazy model load (~9 s measured, minutes on a first-ever download)
# landed on the first TIMED prose request. That was the proxy-overhead p99 of ~9 s in the local
# dashboards. This text is ~260 tokens of filler-heavy prose - the shape G01 exists for.
_WARMUP_PROSE = (
    "Warm-up only, please ignore. This deliberately wordy message exists so that the prompt "
    "compression stage has enough ordinary natural-language text to work on before the real "
    "benchmark begins. The first compression a fresh stack performs has to load its language "
    "model into memory, and on a laptop that one-time cost can take many seconds, or several "
    "minutes on the very first run while the model downloads. If a timed request had to pay "
    "that cost it would distort the latency figures and could even time out and skip "
    "compression altogether, which would change the measured savings. So this paragraph goes "
    "first, gets compressed, and absorbs the cold start instead. It says nothing important: it "
    "is simply a long, rambling, slightly repetitive block of everyday prose, much like a "
    "verbose status update or a meandering customer email that restates the same point in "
    "several different ways before finally getting to the question it wanted to ask, which in "
    "this case is simply to reply with the single word ready."
)
_SIDECAR_WARM_TEXT = (
    "Warm-up: load the compression model now, before the timed benchmark requests begin, "
    "so that no measured request pays the one-time model-load cost."
)


# --------------------------------------------------------------------------- #
# Cold-stack robustness (2026-09-18). A request that FAILED is not a result, and a request
# that never reached the proxy is the only kind that is safe to send again.
# --------------------------------------------------------------------------- #
def never_reached_proxy(exc: BaseException) -> bool:
    """True only when the request provably never reached the proxy, so a retry cannot
    double-count anything (e.g. populate the response cache on attempt one and score a
    'hit' on attempt two). Connection refused / connect timeout qualify. A server that
    accepted the connection and then dropped it ("Server disconnected without sending a
    response") does NOT: it may already have run the pipeline, so it stays a failure."""
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def wait_for_health(health_url: str, deadline: float, poll: float = 2.0) -> bool:
    """Poll the proxy's liveness endpoint until it answers 200 or `deadline` passes."""
    while True:
        try:
            if httpx.get(health_url, timeout=3.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if time.time() + poll > deadline:
            return False
        time.sleep(poll)


def warm_sidecar(sidecar_url: str, timeout: float, connect_grace: float = 120.0) -> tuple[bool, str]:
    """Force the LLMLingua sidecar to load its model before anything is timed.

    Called directly rather than through the proxy because G01 gives the sidecar 10 s per call:
    a first-ever model download outlives that, G01 skips compression, and the proxy cannot
    report whether the model has finished loading. A direct call can simply wait for it.

    Two budgets: a server that accepted the connection gets all of `timeout` (it may be
    downloading ~700 MB), but one that is not listening at all gets `connect_grace` - a sidecar
    that never started should cost the run two minutes, not ten."""
    start = time.time()
    deadline = start + timeout
    connect_deadline = start + min(timeout, connect_grace)
    last, http_errors = "no attempt made", 0
    while time.time() < deadline:
        try:
            resp = httpx.post(sidecar_url, json={"text": _SIDECAR_WARM_TEXT, "ratio": 0.5},
                              timeout=max(5.0, deadline - time.time()))
            if resp.status_code == 200:
                return True, "ok"
            last, http_errors = f"HTTP {resp.status_code}", http_errors + 1
            if http_errors >= 3:
                break                                   # it answers, and keeps saying no
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if never_reached_proxy(exc) and time.time() >= connect_deadline:
                break                                   # nothing listening: absent, not starting
        time.sleep(3.0)
    return False, last


def warm_up_proxy(url: str, health_url: str, headers: dict, body: dict,
                  timeout: float, health_grace: float = 180.0):
    """Get ONE unmeasured request served. Returns (response_json, None) or (None, reason).

    `/health` only proves the process is up; the embedding model and everything else loaded
    lazily are only proven by a served request. On 2026-09-18 a failed warm-up was logged as
    "Continuing" and all 36 timed requests went into a proxy that was not serving. A proxy that
    never answers /health gets `health_grace`, not the whole budget - a mistyped --proxy-url
    should fail in minutes."""
    start = time.time()
    deadline = start + timeout
    if not wait_for_health(health_url, start + min(timeout, health_grace)):
        return None, f"nothing answered {health_url}"
    attempt, last = 0, None
    while time.time() < deadline:
        attempt += 1
        try:
            with httpx.Client(timeout=max(10.0, deadline - time.time())) as wc:
                resp = wc.post(url, headers=headers, json=body)
                resp.raise_for_status()
                return resp.json(), None
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code in (400, 401, 403, 404, 422) or attempt >= 5:
                break                                   # a config problem, or a stable refusal
        except httpx.HTTPError as exc:
            last = exc
            if never_reached_proxy(exc) and not wait_for_health(
                    health_url, min(deadline, time.time() + health_grace)):
                break
        time.sleep(min(5.0 * attempt, 15.0))
    return None, (f"{type(last).__name__}: {last}" if last else "timed out")


def post_with_retry(client, url: str, headers: dict, body: dict, health_url: str,
                    retry_window: float = 120.0, attempts: int = 3):
    """POST once; resend ONLY a request that never reached the proxy (after waiting for it to
    come back), or a 429 (G00 rejects before the pipeline runs). Anything else propagates -
    the caller records it as a failed request."""
    for attempt in range(1, attempts + 1):
        try:
            resp = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            if attempt == attempts or not never_reached_proxy(exc):
                raise
            wait_for_health(health_url, time.time() + retry_window)
            continue
        if resp.status_code == 429 and attempt < attempts:
            time.sleep(min(2.0 * attempt, 10.0))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover


def has_curated_facts(req: dict) -> bool:
    """A record is checked by the facts gate only if it carries a fact or a forbidden string.
    The tool-intent records ship `expected_facts: []` - their answer is a tool call - and used
    to be counted as 5 vacuous passes inside "36/36 checked"."""
    return bool(req.get("expected_facts") or req.get("forbidden"))

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


_MD_EMPHASIS = re.compile(r"[*`~]+")
_WHITESPACE = re.compile(r"\s+")


def normalise_for_match(s):
    """Lowercase, strip Markdown emphasis, and collapse whitespace for substring matching.

    A raw substring gate silently fails whenever the model EMPHASISES the very entity
    being checked: "A simple iron boar crest" is not a substring of
    "A **simple iron boar crest**". Models differ sharply in how much Markdown they
    emit, so the raw gate ends up measuring formatting style rather than answer
    fidelity — and it fires hardest on precisely the arm whose formatting an
    optimisation changed, manufacturing "regressions" where no fact was lost.

    Applied to BOTH needle and haystack, so a fact that legitimately contains one of
    these characters still matches. Whitespace is collapsed so a fact broken across a
    line wrap still matches. Underscores are deliberately NOT stripped — identifiers
    like `_affinity_propagation.py` depend on them.

    Emphasis runs are replaced with a SPACE, never deleted: deleting them merges the
    neighbouring characters ("2*4" → "24"), which would let an expected fact "24" match
    an answer that actually computed 2*4 — a manufactured pass. Space-replacement keeps
    the boundary ("2 4") so no new adjacency is ever created, while "**St Andrews**"
    still normalises to "St Andrews" via whitespace collapse.
    """
    return _WHITESPACE.sub(" ", _MD_EMPHASIS.sub(" ", s or "")).strip().lower()


# Dotted identifiers stay WHOLE. Splitting "base.py" into ("base", "py") makes the
# extension match any other path in the sentence - and `swe` facts ARE file paths, so a
# split tokeniser let "the base class defined in utils.py" satisfy an expected "base.py".
# Kept whole, such a fact is a single token and therefore stays on the strict path.
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")

# Words allowed to sit BETWEEN the gold span's content words. Anything else intervening
# means the answer is making a different statement that merely reuses the words.
# "and"/"or" are deliberately absent: they join separate items rather than glue one claim.
_FILLER = frozenset({
    "a", "an", "the", "of", "s", "in", "on", "at", "to", "for", "by", "with", "from",
    "as", "is", "was", "are", "were", "be", "been", "that", "which", "this", "these",
    "those", "it", "its", "his", "her", "their", "our", "your",
})

# Dropped from a gold span before comparing: they carry no fact, and the possessive "s"
# is the exact thing that makes "Trump's private jet" and "the private jet of Trump"
# different strings while being the same claim.
_FACT_STOPWORDS = frozenset({"a", "an", "the", "of", "s"})

# A negation inside the matched window flips the claim, so the window must not be credited.
# Apostrophes are stripped by _TOKEN, so "isn't" arrives as ("isn", "t") - match the stems.
_NEGATIONS = frozenset({
    "not", "no", "never", "without", "nor", "none", "neither", "cannot", "cant", "dont",
    "isn", "wasn", "aren", "weren", "doesn", "didn", "don", "won", "hasn", "haven",
    "couldn", "shouldn", "wouldn",
})

# How far the gold span's tokens may spread WITHIN one sentence before they stop being
# one claim. Generous enough for reordering and an inserted article, tight enough that
# two loosely related mentions never combine.
_WINDOW_SLACK = 3

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Never treat the period after these as a sentence end. A naive split cuts
# "Donald J. Trump" in half at the initial - i.e. exactly through the kind of span
# these facts are made of - and would defeat the whole guard.
_ABBREV = frozenset({"mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs",
                     "etc", "inc", "ltd", "fig", "eg", "ie", "approx", "no"})


def _sentences(text):
    """Sentence-split normalised text, keeping initials and abbreviations intact."""
    out = []
    for part in _SENT_SPLIT.split(text):
        if out:
            prev = out[-1]
            tail = _TOKEN.findall(prev)
            last = tail[-1] if tail else ""
            if prev.endswith(".") and (len(last) == 1 or last in _ABBREV):
                out[-1] = prev + " " + part
                continue
        out.append(part)
    return out


def _min_window(hay, needles):
    """Smallest (start, end) slice of `hay` containing every token in `needles`, or None.

    Standard linear min-window scan. Distinct tokens only - repetition in the gold span
    carries no extra meaning for a containment check.
    """
    need = set(needles)
    if not need:
        return None
    have, covered, best, left = {}, 0, None, 0
    for right, tok in enumerate(hay):
        if tok in need:
            have[tok] = have.get(tok, 0) + 1
            if have[tok] == 1:
                covered += 1
        while covered == len(need):
            if best is None or (right - left) < (best[1] - best[0]):
                best = (left, right)
            lt = hay[left]
            if lt in need:
                have[lt] -= 1
                if have[lt] == 0:
                    covered -= 1
            left += 1
    return best


def _fact_present(fact, text, sentence_tokens):
    """Is `fact` asserted by the answer? Substring first, then ONE narrow second chance.

    WHY THE SECOND CHANCE EXISTS
        A raw substring gate scores a REPHRASING as a dropped fact. Measured on the
        checked-in calibrated A/B artifact: gold "Donald J. Trump's private jet", direct
        arm wrote it verbatim, proxy arm wrote "the private jet of Donald J. Trump" - the
        same claim, graded as a regression, and (via the cache repeats) counted ten times.
        That manufactures regressions on precisely the arm whose phrasing an optimisation
        changed, which is the same failure family as the Markdown-blind gate fixed earlier.

    WHY IT IS NARROW, AND CANNOT MANUFACTURE A PASS
        - Multi-token gold spans ONLY. A single token (a GSM8K numeric, "Illinois", or a
          dotted path like "base.py") keeps exact substring semantics.
        - EVERY content token must be present - this is containment, not similarity.
        - They must fall inside ONE SENTENCE, and be CONTIGUOUS THERE apart from filler
          words. This is the load-bearing guard. An earlier version allowed any gap up to
          a token budget, which passed "the base class defined in utils.py" for an expected
          "base.py", "42 oranges and 7 apples" for "42 apples", and "Gary played opposite
          Oldman Smith" for "Gary Oldman" - manufactured passes that would hide real
          regressions in the arm being measured.
        - A negation anywhere in that sentence (and not in the gold span) rejects it.

    The bias is deliberately conservative: every ambiguous case resolves to "missing",
    which makes the proxy arm look WORSE, never better. That is the only safe direction
    for a gate whose output feeds a published savings number.
    """
    needle = normalise_for_match(str(fact))
    if not needle:
        return True
    if needle in text:
        return True

    content = [t for t in _TOKEN.findall(needle) if t not in _FACT_STOPWORDS]
    if len(content) < 2:          # single-token facts stay strict - see docstring
        return False

    gold = set(content)
    cap = 2 * len(content) + _WINDOW_SLACK          # backstop; contiguity is the real gate
    for toks in sentence_tokens:
        win = _min_window(toks, content)
        if win is None:
            continue
        start, end = win
        if (end - start + 1) > cap:
            continue
        # Everything inside the span that is not part of the fact must be filler.
        if any(t not in gold and t not in _FILLER for t in toks[start:end + 1]):
            continue
        if any(t in _NEGATIONS and t not in gold for t in toks):
            continue
        return True
    return False


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
    text = normalise_for_match(answer)
    sentence_tokens = [_TOKEN.findall(s) for s in _sentences(text)]
    missing = []
    for item in (expected_facts or []):
        if isinstance(item, (list, tuple)):
            if not any(_fact_present(opt, text, sentence_tokens) for opt in item):
                missing.append(list(item))
        elif not _fact_present(item, text, sentence_tokens):
            missing.append(item)
    # `forbidden` stays EXACT-substring on purpose. Relaxing it would let coincidental
    # co-occurrence of a banned phrase's words trip the gate on an answer that never
    # said it - a manufactured failure, the mirror of the manufactured pass guarded above.
    present_forbidden = [f for f in (forbidden or []) if normalise_for_match(str(f)) in text]
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
                    help="how long the warm-up may take in total, retries included (s). The "
                         "timed run does not start until the proxy has served it.")
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the warmup request (only if the proxy is already warm)")
    ap.add_argument("--temperature", default="0",
                    help="sampling temperature sent with every request (default 0, as the A/B "
                         "harness and the internal ablation use). 'none' sends no temperature, "
                         "i.e. the provider's default (1.0 on OpenAI) - the pre-2026-09-18 "
                         "behaviour, under which the facts gate flipped run to run on answer "
                         "wording (see README, 'Why the quality gate runs at temperature 0').")
    ap.add_argument("--sidecar-url", default=os.environ.get("LLMLINGUA_WARM_URL", ""),
                    help="LLMLingua sidecar /compress URL to warm directly before the run "
                         "(the launchers pass http://localhost:8080/compress). Without it the "
                         "sidecar is warmed only through the proxy, which cannot wait out a "
                         "first-ever model download.")
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
    if str(args.temperature).strip().lower() in ("none", ""):
        temperature = None
    else:
        try:
            temperature = float(args.temperature)
        except ValueError:
            return _fail(f"--temperature must be a number or 'none', not {args.temperature!r}")
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
    failures: list[dict] = []      # requests that got no answer - any one makes the run INCOMPLETE
    health_url = args.proxy_url.rstrip("/") + "/health"

    # 1. The LLMLingua sidecar, directly (see warm_sidecar for why not through the proxy).
    if args.sidecar_url and not args.no_warmup:
        print(f"Warming the compression sidecar at {args.sidecar_url} (a first run downloads "
              "its model - this can take a few minutes)...")
        ok, detail = warm_sidecar(args.sidecar_url, args.warmup_timeout)
        print("  sidecar ready.\n" if ok else
              f"  WARNING: the sidecar did not answer ({detail}). G01 prose compression may not "
              "fire, and the result would then exclude that lever.\n")

    # 2. The proxy - must SUCCEED before anything is timed (see warm_up_proxy).
    if not args.no_warmup:
        print("Warming up the proxy (the first run downloads the embedding model - this can take a")
        print("few minutes; subsequent requests are fast)...")
        warm_body = {
            "model": args.model,
            "messages": [{"role": "user", "content": _WARMUP_PROSE}],
            "max_tokens": 8,
            # Same per-request skips the dataset uses, so warmup doesn't trigger G07
            # retrieval (a slow one-time embedding-model download) or semantic-cache work.
            "x_jit_retrieval": False,
            "x_cache_semantic": False,
            # Long enough to cross G01's min_tokens_to_compress, so G01 really calls the
            # sidecar through the proxy's own client path.
            "x_compress_user": True,
            # Leaves nothing behind in either cache level: the warm-up is not part of the run.
            "x_no_cache": True,
        }
        warm_data, why = warm_up_proxy(url, health_url, headers, warm_body, args.warmup_timeout)
        if warm_data is None:
            return _fail(f"the proxy did not serve the warm-up request ({why}). No timed "
                         "requests were sent. Check: docker compose logs proxy")
        fired = sorted(((warm_data.get("_token_opt") or {}).get("step_savings") or {}).keys())
        print(f"  warmup complete (groups that recorded a step: {', '.join(fired) or 'none'}).\n")

    t0 = time.time()   # after the warm-up: cold-start time is not the benchmark's duration
    print(f"Sending {len(reqs)} requests to {url} (model default: {args.model}, tenant: {args.tenant}, "
          f"temperature: {'provider default' if temperature is None else temperature})\n")
    with httpx.Client(timeout=args.timeout) as client:
        for i, req in enumerate(reqs, 1):
            label = req.get("_label", "")
            body = {"model": req.get("model", args.model), "messages": req["messages"]}
            if temperature is not None:
                # Input-side only: this cannot move a token-savings figure (the pipeline's work on
                # the prompt is deterministic). What it removes is answer-wording variance from
                # the facts gate - measured 2026-09-18 with the A/B harness, where the DIRECT arm
                # (no proxy at all) missed facts at the default temperature as often as the proxy.
                body["temperature"] = temperature
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
                data = post_with_retry(client, url, headers, body, health_url)
            except Exception as exc:  # noqa: BLE001 - recorded, and it fails the run
                print(f"  [{i}/{len(reqs)}] {label:<30} ERROR: {exc}")
                failures.append({"request": i, "label": label,
                                 "error": f"{type(exc).__name__}: {exc}"})
                if args.quality_check and has_curated_facts(req):
                    # An unanswered record is a FAILED check, not a skipped one: dropping it
                    # shrank the denominator, so a run with dead requests could print PASS.
                    quality_rows.append({"label": label, "question": "", "answer": "", "failed": True,
                                         "facts": {"passed": False, "missing": ["(no answer - request failed)"],
                                                   "present_forbidden": []}})
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
                if args.quality_check and has_curated_facts(req):
                    fres = check_facts(answer, req.get("expected_facts"), req.get("forbidden"))
                    # Still a failure - but a reader must be able to tell "the fact was dropped"
                    # from "the answer ran out of max_tokens before it got there" (the A/B
                    # harness already makes the same distinction).
                    fres["truncated"] = ((data.get("choices") or [{}])[0].get("finish_reason") == "length")
                    row["facts"] = fres
                    if not fres["passed"]:
                        print(f"        facts FAIL — missing={fres['missing']} "
                              f"forbidden_present={fres['present_forbidden']}"
                              f"{'  [answer truncated at max_tokens]' if fres['truncated'] else ''}")
                quality_rows.append(row)

    if n_ok == 0:
        return _fail("no successful requests — is the proxy up and the key valid?")

    dur = time.time() - t0
    saved = tot_base - tot_sent
    pct = (100.0 * saved / tot_base) if tot_base else 0.0
    cost_pct = (100.0 * (cost_base - cost_act) / cost_base) if cost_base else 0.0
    complete = not failures

    _render(args, reqs, n_ok, dur, cache_hits, tot_base, tot_sent, saved, pct,
            cost_base, cost_act, cost_pct, per_group, failures)

    # Written either way, so a previous run's number can never sit in this file looking like
    # this run's - but an incomplete run says so in the artifact itself.
    OUT.write_text(json.dumps({
        "complete": complete,
        "requests": n_ok, "requests_planned": len(reqs), "failed_requests": len(failures),
        "failures": failures,
        "duration_s": round(dur, 1), "cache_hits": cache_hits,
        "baseline_tokens": tot_base, "tokens_sent": tot_sent,
        "tokens_saved": saved, "pct_saving": round(pct, 2),
        "cost_baseline_usd": round(cost_base, 6), "cost_actual_usd": round(cost_act, 6),
        "cost_pct_saving": round(cost_pct, 2),
        "per_group_tokens_saved": dict(sorted(per_group.items(), key=lambda x: -x[1])),
        "metric": "proxy _token_opt: baseline_tokens vs final_tokens_sent",
        "temperature": temperature,   # None = provider default was used
    }, indent=2), encoding="utf-8")
    print(f"  Full detail -> {OUT}{'' if complete else '  (marked complete: false)'}")
    print("=" * 60)

    rc = EXIT_OK
    if args.quality_check or args.judge:
        rc = _quality_summary(args, quality_rows)
    if not complete:
        # Takes precedence over the gate's verdict: an incomplete run's gate is incomplete too.
        print(f"\n  INCOMPLETE RUN: {len(failures)} of {len(reqs)} requests failed "
              f"({', '.join(f['label'] for f in failures[:5])}{' ...' if len(failures) > 5 else ''}).")
        print("  This is not a benchmark result. Fix the cause and re-run.")
        return EXIT_INCOMPLETE
    return rc


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
                          f"forbidden={r['facts']['present_forbidden']}"
                          f"{'  [truncated at max_tokens]' if r['facts'].get('truncated') else ''}")
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
            if r.get("failed"):
                continue        # no answer to judge; the facts gate already counts it as failed
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
            cost_base, cost_act, cost_pct, per_group, failures=()) -> None:
    line = "=" * 60
    print("\n" + line)
    print("  TOKEN OPTIMISATION - BENCHMARK RESULT  (single-tenant)")
    print(line)
    if failures:
        print(f"  *** INCOMPLETE: {len(failures)} of {len(reqs)} requests failed - the figures")
        print("  *** below cover only the requests that were answered. Not a result. ***")
    print(f"  Dataset            {DATASET.name} ({n_ok} reqs)")
    print(f"  Proxy / model      {args.proxy_url} / {args.model}")
    print(f"  Duration           {dur:.1f}s     cache hits: {cache_hits}/{n_ok}")
    print()
    print(f"  Baseline tokens    {tot_base:>9,}   (un-optimised counterfactual)")
    print(f"  Tokens sent        {tot_sent:>9,}")
    print("  " + "-" * 56)
    if failures:
        print(f"  TOTAL TOKEN SAVINGS   n/a    (incomplete - partial {pct:.1f}% over {n_ok} reqs)")
    else:
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
    # No internal figure is quoted here. This line used to print "(55.78%)" on every run - a
    # number two re-measurements out of date, with none of the caveats the project attaches to
    # its headline. A viewer's terminal is the last place a stale claim should come from.
    print("  Method: aggregate of the proxy's own per-request _token_opt")
    print("  (baseline_tokens vs final_tokens_sent) - the same metric the project's")
    print("  internal quality-gated ablation uses. This is one safe, reproducible")
    print("  workload, not that headline; see examples/benchmark/README.md.")
    print()


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
