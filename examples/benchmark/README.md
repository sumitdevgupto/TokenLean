# Reproducible token-savings benchmark

A small, **single-tenant** harness that proves the optimisation pipeline works on
**your own key** — clone, run one command against a local proxy, and watch the tokens
drop across **six** techniques (cache, structured pruning, dedup, lazy tools, prompt
compression, routing).
Savings are measured with the **same metric** as the project's headline result (the
proxy's per-request `_token_opt`: `baseline_tokens` — the un-optimised counterfactual —
vs `final_tokens_sent`), plus a config-priced cost estimate and an automated quality gate.

---

## Quick start — steps to run

Run everything **from the repo root**. The launcher checks prerequisites, starts
the local stack if it isn't already up, finds a proxy key, and runs the benchmark.

**Step 1 — set the proxy's OpenAI key** in `.env` at the repo root:

```bash
LLM_KEY_OPENAI=sk-...        # NOTE: LLM_KEY_OPENAI, not OPENAI_API_KEY
```
(You can reuse your existing `OPENAI_API_KEY` value. This is the only manual step.)

**Step 2 — run the one command:**

```powershell
# Windows (PowerShell)
.\examples\benchmark\run.ps1
```
```bash
# Linux / macOS
./examples/benchmark/run.sh
```

> Want a quick, cheap check first? Append `--limit 5` to run just 5 requests.
> The proxy image is rebuilt from your checkout on every run (a cached no-op when
> nothing changed), so a `git pull` is picked up automatically; `--rebuild` rebuilds
> every image, not just the proxy.
> Add `--quality-check` to assert each answer still contains its required policy
> facts (no extra cost); add `--judge` for an opt-in LLM faithfulness score.

**Both launchers behave the same** — config pin, restore, exit status. (Until
2026-09-18 `run.ps1` had no config pin, so a Windows run measured whatever
`config/config.yaml` held and could not reproduce the figure below.)

**Exit status — a run either completed or it is not a result:**

| Code | Meaning |
|---|---|
| `0` | every request answered (and, with `--quality-check`, the facts gate passed) |
| `1` | nothing measured — bad arguments, or the proxy never served the warm-up request |
| `2` | complete run, **quality gate failed** |
| `3` | **INCOMPLETE** — at least one request failed; no savings figure is printed and `last_run.json` says `"complete": false` |

With `--ab`: `2` = a fact regression, `3` = spend cap, `4` = a requested lever never fired,
`5` = incomplete (an A/B pair errored). If a run is interrupted and leaves your config pinned,
`run.sh --restore` / `run.ps1 --restore` puts it back.

> **Recording or demoing?** Turn off Docker Desktop's automatic updates first. On
> 2026-09-18 an auto-update restarted the engine mid-run and killed every container; the
> launcher now reports that as INCOMPLETE (exit 3) instead of printing a number.

The benchmark runs under a **dedicated tenant** (`bench`, sent via the
`X-Tenant-ID` header), so every key it creates is namespaced under `t:bench:`.
(That holds for an **admin** key — the one the launcher generates on a first run. A
non-admin key runs under its own tenant whatever the header says; the launcher detects
this, tells you, and flushes that tenant's namespace instead.)
By default the launcher **clears that tenant's prior-run keys** first (via
`clear-cache.sh`/`.ps1`, which deletes only `t:bench:*`) so each run measures real
optimisation instead of replaying a warm cache — otherwise a second run would
report ~100% (every request served from cache). Because deletion is scoped to the
benchmark tenant, it touches **only the benchmark's own data** — no other tenant's
cache and no global state. Pass `--keep-cache` to skip the reset (e.g. to observe
warm-cache behaviour).

The launcher is **self-contained** — it creates `config/config.yaml` from the
template and generates a local proxy key on first run if you don't have one, then
builds + starts the stack. It only needs the repo's `docker-compose.yml`; it does
not depend on the `scripts/` folder.

**Step 3 — read the result.** Per-request savings stream by, then a summary box
with the **total token savings**, an estimated cost saving, and a **per-group
breakdown** (which techniques earned the savings). Full detail is written to
`last_run.json`.

> The proxy makes real LLM calls (a few cents). Cache hits — the FAQ repeats — are
> served locally and cost nothing.

### What the launcher checks / does for you
- **Docker** installed and running.
- **`.env` has `LLM_KEY_OPENAI`** set (fails fast with a clear message if empty).
- **Pinned config, applied BEFORE the stack starts** — it pins a known-good config
  (`pin_config.py`, shared by both launchers: derived from `config.yaml.template` with the
  six measured groups enabled: G01, G05, G06, G08, G19, G22, and **G28 CCR disabled** — in
  pass-through it replaces an over-threshold system prompt with a reference token the model
  can't resolve, which would shred answer quality), then **restores your original config
  and reloads** on exit — even on failure or Ctrl-C. Pinning first means a cold stack
  starts once, on the pinned config; only a proxy that was already running is restarted.
  One setting follows *your* config rather than the template: whether Langfuse tracing is
  on (reporting only — it moves no token count). Pass `--no-pin-config` to measure the live
  config as-is.
- **Stack health, then a warm-up that must succeed** — `docker compose up -d`, wait for
  `http://localhost:4000/health`, then load the LLMLingua sidecar's model directly and send
  one unmeasured request through the proxy. No timed request is sent until that warm-up has
  been served (on a first run the model downloads can take a few minutes). A timed request
  that never reached the proxy (connection refused while it restarts) is sent again once it
  is healthy; one that reached it and then died is not — it may already have run the
  pipeline — so it fails the run instead.
- **Proxy key** — uses `$PROXY_API_KEY`, else a `tok-...` from `ROI_PROXY_API_KEY_*`
  in `.env`. No keys yet? Generate them with `bash scripts/local/deploy-local.sh`.

### Manual invocation (if you'd rather not use the launcher)

```bash
docker compose up -d
export PROXY_API_KEY=tok-...                 # a proxy-issued key, not the LLM key
python examples/benchmark/run_benchmark.py   # --limit N / --model / --proxy-url
```

---

## What it runs

A realistic **DevOps/support** workload (`dataset.jsonl`, 36 requests), deliberately
shaped so that *each* safe, quality-preserving stage of the pipeline actually fires —
not just the response cache. The **system instruction is never compressed**
(`compress_system_prompt` stays off — that's the quality guard; on the structured-data
requests G19 collapses its blank lines, never its words); compression is applied only to the
developer/user-side content that can safely take it:

| Scenario | Requests | Technique exercised |
|---|---|---|
| Support FAQs asked repeatedly (deflection) | 13 (5 unique + 8 repeats) | **G05** L1 exact-match response cache |
| Troubleshooting with a pasted JSON / log / code payload as its own message | 7 | **G19** structured pruning (compacts JSON, dedups timestamped logs, strips code) |
| Multi-turn incidents that re-paste the same context block | 3 | **G22** dedup (collapses the redundant copies; an authoritative copy is kept) |
| Agent requests carrying a 14-tool catalogue | 5 | **G08** lazy tool loading (only the intent-relevant tools are sent) |
| Verbose customer prose (outage report, rambling bug report, meeting notes) | 3 | **G01** LLMLingua-2 compression of the **user message only** (filler dropped, facts kept; system prompt untouched) |
| Simple questions a developer naively sent to a capable model (gpt-4o) | 5 | **G06** routing (classified simple → routed down to gpt-4o-mini) |

Nothing here is reverse-engineered to a number — it's a plausible real-world mix, and we
report exactly what the quality-preserving pipeline yields.

> **Why G01 is on here.** Blanket prompt compression was the failure mode (it shreds the
> system instruction). The safe version is **selective**: compress the rambling *user* prose
> (a pasted transcript, a verbose write-up), never the instruction. This is the only lever
> that helps a *unique, first-ask* request, and the facts gate proves the answer survives.
> It's opt-in per request (`x_compress_user`) and needs the LLMLingua sidecar warm.

> **Scope vs the 54.1% headline.** The 54.1% figure is a **quality-gated blend across a
> broader multi-dataset ablation** — only datasets whose answer quality held (temperature-0,
> reproducible) are counted toward it — and it carries a caveat and a 2026-09-09 re-measurement
> in the root README; read it there, not here. This example is a *single* workload, so its
> number differs — but it is computed the **same way**, so it's comparable in kind. We report
> whatever this dataset actually yields; we do **not** tune the data to hit a target.

## Calibrated result

Re-calibrated **2026-09-18** on a local stack (gpt-4o-mini, temperature 0, 36 requests, proxy
image rebuilt from this checkout). **Deterministic**: two consecutive runs landed on **53.5%**
to the token (11,070 / 20,684), and an image built nine days earlier — four proxy commits
behind — gave the identical figure. Only the answers' wording (so completion tokens, so the
cost line) varies run to run.

```
TOTAL TOKEN SAVINGS   53.5%   (11,070 / 20,684 tokens)   8 cache hits   ~60s
Est. cost savings     ~62%    ($0.0077 -> $0.0029)        QUALITY GATE: 31/31 PASS

Per-group contribution (tokens saved)
  G05 response cache      49.1%
  G08 lazy tool loading   22.0%
  G19 structured pruning  16.2%
  G22 dedup                5.9%
  G01 compression          1.5%
  G06 model routing        0.0%   (saves cost, not tokens — see below)
```

> **What happened to 57.1%.** That figure was measured on 2026-07-04 and never refreshed
> through the changes since — among them the 2026-09-06 decision to stop pinning a
> system-prompt cap no default install applies, which is why the tool-pruning share fell from
> 41% to 22%. It is superseded, not reconciled token by token. "36/36" became "31/31" because
> five tool-call records carry no facts to check and used to be counted as passes.

> **No tuning, no inflation.** The percentage is the proxy's own billed-token metric
> (`final_tokens_sent` = the provider's `usage.prompt_tokens`, 0 on a cache hit) — not an
> estimate. The benchmark runs with the L2/L3 *semantic* cache off (`x_cache_semantic=false`):
> with it on, a long shared system prompt makes distinct requests falsely collide and report
> a higher (but **wrong**) number, so we report the lower, honest figure. The workload mix is
> realistic but favourable (cache + tool-pruning dominate); a workload with fewer repeats or
> smaller tool catalogues will land lower.

**Six techniques, all quality-preserving.** Savings span six groups, and the answers pass
an automated facts gate: `--quality-check` asserts that each of the 31 answers with curated
facts contains them (e.g. GDPR → "eu-west" + "eu-central"; the OOMKilled root cause; for the
compressed prose, the outage's "09:10"/"23%"/"1.4.2") and no forbidden content, exiting
non-zero on a miss. So these are *real* savings on *correct* answers — not prompt-shredding.

**Why the quality gate runs at temperature 0.** Until 2026-09-18 the runner sent no
temperature, so answers were sampled at the provider's default (1.0) and two consecutive runs
failed on *different* records at an identical token figure. We measured it with the A/B
harness's own arms and relative gate, 40 paired samples per arm on each record that had failed:
at the default temperature the **direct** call — no proxy at all — missed those facts about as
often as the proxy did (37/40 vs 39/40, 39/40 vs 36/40, 39/40 vs 39/40), and at temperature 0
**neither arm missed once**. The prompts the proxy actually sent still contained every checked
fact. So the flakiness was the sampler, not the optimisation, and the gate now runs the way the
project's other quality gates do (`--temperature none` restores the old behaviour). A miss on an
answer that ran out of `max_tokens` is labelled `[truncated at max_tokens]`.

**G01's share is small but the point is per-request.** It only runs on the 3 verbose-prose
requests, so it's ~2% of the *total*; on two of them it cuts **34–38%** of the user message
while keeping every fact, and on the third (the outage report) its faithfulness guard refuses the
compression every run — it would drop a negation or scope qualifier — so that one is sent as
written. It's the only lever that helps a **unique, first-ask** request (cache/dedup need
repeats; structured pruning needs structured content). The system instruction is never
compressed. (LLMLingua's BERT classifier keeps numbers/entities and drops filler like
"I'm writing to let you know that, unfortunately…".)

> **Run the gate yourself:** `./examples/benchmark/run.sh --quality-check` (facts, no extra
> cost) or add `--judge` for an LLM faithfulness score (a few cents). Ground-truth facts are
> inlined per record in `dataset.jsonl` (`expected_facts` / `forbidden`).

**Two things to read carefully:**
- **G06 routing shows 0% in the token breakdown but drives the cost line.** Routing swaps
  the model, not the token count: the 5 "simple" questions were sent to gpt-4o and routed
  *down* to gpt-4o-mini, which is most of the ~62% cost saving. Token savings and cost
  savings are reported separately on purpose.
- **G07 RAG, G14/G15 tool-output are *not* in this number.** G07 *adds* retrieved context
  (it doesn't reduce a request's own tokens), and G14/G15 only fire inside the agent runtime
  (they act on tool-call *results*, which a pass-through chat completion never returns). A
  black-box benchmark can't honestly credit them, so it doesn't.

> **On the project's headline numbers:** any "30–70%" claim should be read as
> *quality-preserving* savings (caching, routing, dedup, safe structured pruning, lazy tools)
> — **not** prompt-shredding. This example is an honest, reproducible cross-section.

## How the number is computed (transparency)

For each request the proxy returns `_token_opt`:

```json
{ "baseline_tokens": 812, "final_tokens_sent": 6, "total_pct_saving": 99.3,
  "cache_hit": true, "step_savings": { "G05": { "abs_saving": 806 } } }
```

The harness sums `baseline_tokens` and `final_tokens_sent` across all requests and
reports `1 - sent/baseline`. Per-group attribution is summed from `step_savings`; the
cost line sums the proxy's per-request `cost_baseline_usd` vs `cost_actual_usd`.

**Per-request controls the benchmark sets** (passed in each request body as `x_*` keys,
which the proxy reads and strips before the upstream call — so they never reach OpenAI):
- `x_complexity: simple` on the data-heavy scenarios — pins them to the cheap tier so a
  large pasted payload isn't misread as a "complex" query and escalated to an expensive model.
- `x_jit_retrieval: false` — skips G07 retrieval (no RAG corpus is seeded here; it would
  only add latency and tokens).
- `x_cache_semantic: false` — uses L1 *exact-match* caching only. A system prompt longer
  than the embedding window otherwise dominates the L2/L3 semantic vector and collapses
  distinct requests onto one another (masking the other techniques — and, in production,
  a real correctness risk worth knowing about). L1 still rewards the repeated FAQ traffic.
- `x_compress_user: true` on the verbose-prose scenario — turns on **selective** G01
  LLMLingua-2 compression of the *user* message for that request, with the system prompt
  left untouched. (Off everywhere else, so it never touches the JSON/log payloads that G19
  handles.) Needs the `llmlingua` sidecar running and warm.

> **Cost figures are config-priced estimates** — token counts × a static pricing
> table — directional, not invoice-grade (no negotiated discounts, provider-side
> caching, or batch/reasoning surcharges). **Token-count savings are measured;
> dollar figures are estimated.**

---

## A/B: proxy vs direct — the independently verifiable number

The single-arm run above uses the proxy's *own* `_token_opt` counterfactual. `run_ab.py`
answers the skeptic's objection ("the proxy grades its own homework") with a **true A/B**:
every request is fired **twice** — once **direct to the provider** (via `litellm`, the same
library the proxy uses) and once **through the proxy** — and we compare the **provider's own
billed usage** on both arms, priced from a checked-in dated `prices.json` applied identically.
Nothing is self-reported: a proxy cache hit shows as **0 provider tokens** (that's the product
working), and quality is gated on both arms.

### Recognized-standard datasets, verbatim

We invent no question content. Items come verbatim (pinned revision + citation) from public,
clean-licensed datasets — see [`DATA_LICENSES.md`](DATA_LICENSES.md):

| Profile | Dataset | License | Grading |
|---------|---------|---------|---------|
| `rag` | HotpotQA (distractor) | CC BY-SA 4.0 | gold-answer facts |
| `chat` | MT-Bench | Apache-2.0 | LLM judge |
| `swe` | SWE-bench Lite | permissive research | gold-patch paths/symbols facts |
| `code` | HumanEval | MIT | judge / opt-in exec pass@1 |
| `reason` | GSM8K | MIT | final-numeric-answer facts |
| `agentic` | BFCL v3 multi_turn | Apache-2.0 | relative tool-trajectory (proxy vs direct) |

`public_dataset.jsonl` (the canonical items) + `cache_schedule.json` + `agentic_dataset.jsonl`
are **checked in**, so you need no Hugging Face account. Regenerate them from source with
`python build_public_dataset.py --hf` (needs `pip install datasets huggingface_hub`);
`build_source` in `public_dataset.meta.json` records the provenance (`"huggingface"` = the real
verbatim build, `"fixture"` = a structural placeholder that must be regenerated before publishing).

### Four savings workloads + one cache-economics probe (the credibility spine)

No recognized capability benchmark has repeat/traffic structure — they run each item once. So the
harness reproduces **each production lever as its own workload** and reports every number
separately. `--workload full` runs all of them in one pass (cold cache never contaminates the
warm burst — every original precedes its repeats):

- **prose / reasoning** (`--workload standard`, cold) — first-occurrence items with **G05 caching
  bypassed**. Savings come only from the *stateless* optimisations (compression, routing, pruning,
  lazy tools). The **indisputable floor** — nobody can claim we shaped the data. Genuinely low on
  the small, stateless recognized-Q&A items (~2–4%; the internal 38% prose figure comes from
  production-scale documents, not one-line questions). One **disclosed** `ops` profile (verbose
  DevOps payloads — pasted JSON/logs/config, flagged in `DATA_LICENSES.md` as **not** a recognized
  benchmark) is included alongside them because the recognized Q&A sets are too small/stateless to
  exercise the **structured-pruning (G19) / dedup (G22)** levers on a first-ask; those payloads read
  ~43% and lift the combined prose lever to ~8%. We report whatever `ops` yields — it is never tuned.
- **cache** (`--workload cache`) — a **disclosed** warm-repeat burst (verbatim repeats, L1 exact-match
  via `x_cache_semantic:false`) that reproduces the caching lever at **0 quality loss**.
- **agentic** (`--workload agentic`) — a multi-turn BFCL tool loop run on both arms. It reproduces
  the **tool-catalogue-pruning** lever (G08/G16) and, since 2026-09-10, the **request-side pruning
  of tool results** as well: the mocked results were a twelve-token status stub, so there was
  nothing for that lever to act on and the slice measured catalogue pruning alone. They are now
  generated from each tool's own schema and capped to the internal dataset's size band (see
  `DATA_LICENSES.md`). The response-side tool-output-projection lever (G14/G15) still
  **structurally cannot fire in a live single-loop A/B** — it reads a `function.result` field a
  live model never emits — so this slice remains below the internal 46%, disclosed, not hidden.
  **The previously published ~12% predates the result-sizing change and is awaiting re-run.**

**And one probe that is deliberately NOT a savings workload:**

- **provider cache read/write** (`--workload provider-cache`) — many **distinct** questions over
  **one long shared prefix** (~13.5k tokens, byte-identical across every request), with the proxy
  cache bypassed so every call genuinely reaches the provider. It exists because the four
  workloads above report `cache_read`/`cache_write` as **0** and always will: on a warm repeat
  neither arm reaches the provider, and the distinct originals share only a ~15-token system
  prompt — under the ~1,024 tokens a provider needs before it caches anything. Cache cost is the
  half of the bill token counts do not show, so the benchmark could say nothing about it at all.
  The dossier is HotpotQA paragraphs already checked in (no download); each question is that
  item's own, so the existing facts gate grades it unchanged.
  **It carries no savings percentage and is excluded from the blend** — its prefix size is a knob
  we chose, and a number built from it would describe the instrument as much as the proxy. Read
  and write are always reported together, as absolute tokens, per arm. If the provider reports
  nothing, that is recorded as *unreported* rather than as zero: "declined to cache" and "does not
  disclose the counters" are different facts.

### Calibrated expected results (OpenAI, temperature-0)

Reproduce each part; the numbers carry the same run-to-run variance as the headline (borderline
items flip under model nondeterminism even at temperature-0), so treat them as bands, not exact:

| Workload | Token savings | Notes |
|---|---|---|
| cache | **90–93%** | warm repeats served locally; 0 fact drops on both runs |
| agentic | **21–27%** (n=2) | tool-catalogue pruning (G08/G16) **plus** request-side pruning of the tool results, which only became measurable once the mocked results were realistically sized (2026-09-10). The previously published ~12% was measured against twelve-token stubs. 2–4 of 15 episodes drop a tool the direct arm called (`multi_turn_base_52` and `_55` every run, others intermittently) |
| prose (cold, recognized Q&A) | **~2.5%** default · **~3.6%** with `--compress-user` | stateless floor on public items; see the two-sided note below |
| ops (cold, production-shaped) | **44.0%** default · **42.8%** with `--compress-user` | G19/G22 structured-pruning on verbose DevOps payloads; disclosed **non**-benchmark |
| reasoning (cold) | **0.3%** | reasoning traffic barely compresses — honest |
| combined prose lever | **8%** default · **9%** with `--compress-user` | `rag`+`chat`+`ops` cold, the figure the blend consumes |
| **illustrative blend** | **34.4–36.0%** | disclosed weighted average (`--weights` tunable), **not** a headline |
| provider cache read/write | **read 91.0% direct · 90.6% proxy** · write **not reported by OpenAI** | `--workload provider-cache`, measured 2026-09-16 on `gpt-4o-mini`, 12 calls per arm, $0.046. Absolute: direct 140,800 cache-read of 154,650 prompt tokens; proxy 136,576 of 150,786. **Not a savings figure and not in the blend.** See the two findings below |

Measured 2026-09-10 on `gpt-4o-mini` at temperature-0, one run per side, $0.19 total. Both sides
ran the identical corpus; they differ by one request field.

**Provider prompt cache — what the first run found (2026-09-16).** Two things, and only one of
them is good news.

1. **The proxy does not break the provider's prefix cache.** OpenAI served **91.0%** of the direct
   arm's prompt from its cache and **90.6%** of the proxy arm's — a 0.4-point gap, and the proxy
   arm is the one that also sent 2.5% fewer tokens. So on this shape the two levers compose rather
   than fight. That is worth knowing precisely because they *can* fight: compressing a prefix below
   the provider's ~1,024-token minimum stops it caching at all, which is the whole subject of
   backlog #41/#55.
2. **The write half cannot be measured on OpenAI at all.** OpenAI publishes `cached_tokens` (read)
   and **no cache-write counter**. The run therefore reports write as `n/r`, never as `0` — a
   confident zero about someone else's billing would be a claim we cannot support. Anthropic does
   publish `cache_creation_input_tokens`, so the write half becomes measurable the day this
   workload is run against it. **Until then, the "two-sided read AND write" promise is only half
   deliverable on OpenAI, and the report says so rather than papering over it.**

One caveat on quality: 11 of 12 answers cleared the facts gate. The flagged record (`pcache-0007`)
is a **synonym, not a dropped fact** — the direct arm said "secondary school study" and the proxy
said "high school". The gate is deliberately literal, because a matcher loose enough to accept
synonyms is loose enough to manufacture passes; so it is reported as a regression and explained
here rather than graded away.

**What the opt-in actually bought, and cost.** Turning compression on moved the combined prose
lever from 8% to 9% — about one point — and **dropped two checked facts that the default side kept**
(`ops-0001` lost the retry count `5`; `ops-0007` lost `insufficient memory`). It also made the `ops`
profile slightly *worse* on tokens, 44.0% down to 42.8%. On this corpus the opt-in is a bad trade,
which is consistent with it shipping off. That is the whole point of publishing both sides: the
compressed number is not the better number.

**Run-to-run variance is real and larger than that gap.** The two runs differ on slices
`--compress-user` does not touch at all — agentic read 26.9% then 21.4%, cache 93.2% then 90.3%.
Treat every figure above as a band, and never compare a one-point difference across two runs.


The **illustrative blend** (`--workload full`) is `Σ wᵢ·savingᵢ` over the reproducible parts, with
default balanced weights `cache=0.30 prose=0.35 agentic=0.20 reasoning=0.15` echoed (with citations)
into `ab_results.json`; the **prose lever** is the combined `rag`+`chat`+`ops` cold savings (≈8%). It
lands **below the internal 54.1% by construction** — agentic is only partially live-reproducible and
the recognized public prose payloads are small — and **we never tune the weights (or the `ops` data)
to hit a target**: change the weights with `--weights` and recompute the blend yourself.

### All 10 providers, auto-detected

The harness swaps the `model` per provider and runs the A/B for **every provider whose
credentials are configured** (`LLM_KEY_<PROVIDER>` / the native var; Azure/Bedrock also need
endpoint/region). Default `--providers openai` stays **under $1**; `--providers all` runs the
full sweep (each provider its own spend sub-cap).

```bash
# local (boots the stack), OpenAI only, all four workloads + the blend, quality-gated:
./examples/benchmark/run.sh --ab --workload full --judge

# a single lever, or a specific set / all configured providers:
./examples/benchmark/run.sh --ab --workload cache
./examples/benchmark/run.sh --ab --providers openai,anthropic
./examples/benchmark/run.sh --ab --providers all
```

Fairness: same body both arms (provider's mapped model, `temperature: 0`, same `max_tokens`);
proxy `x_*` controls stripped from the direct arm; arm B tokens = `_token_opt.tokens_provider_billed`
(the real provider prompt tokens; 0 on a cache hit); token savings and cost savings reported
**separately** (routing changes cost without always cutting input tokens); the facts gate is
**relative** (a record fails only if the proxy drops a fact the direct arm had). Spend is capped
per provider (`--max-spend-per-provider`, default $1) with an overall ceiling; a tripped cap
stops that provider and exits non-zero (3). A pair that errors is not a measurement: the run
exits 5 and `ab_results.json` carries `"complete": false` with the errored pairs listed (it used
to print ERROR and exit 0). Nothing is measured until one unmeasured warm-up request has been
served. Results: `ab_results.json` (per provider → workload slice
(`cold`/`cache`/`agentic`) → per dataset + total, plus the illustrative `blend` block under
`--workload full`) + `ab_cost_log.jsonl`.

### Tenant self-verify (live GCP, no Docker)

Already onboarded on a live TokenLean proxy and want to preview *your* savings before first
prod traffic? Use `verify.sh` (no Docker; it makes its own venv). This flow always does the true
A/B, so it **requires your own provider key** (onboarding never gives you one — BYOK tenants
already have theirs). Only the bundled **public** dataset is sent to the provider — never your data.

```bash
git clone https://github.com/sumitdevgupto/TokenLean.git
cd TokenLean/examples/benchmark
./verify.sh --proxy-url https://<your-proxy>.run.app --api-key tok-... --provider-key sk-...
```

(Windows: `.\verify.ps1 ...`.) See also `docs/client-onboarding.md`.

### Optional: agentic resolution-rate (heavy, ~$100s, Docker)

The `swe` profile above measures **token savings on developer-shaped long-context traffic**, not
issue-**resolution rate**. For a true agentic-resolution number, run the proxy in front of a coding
agent (e.g. SWE-agent) on SWE-bench Lite and diff resolution-rate + per-instance cost against a
direct baseline. That needs the SWE-bench Docker evaluation harness (≥120 GB, a few hundred dollars
of compute) and is **not** part of this under-$1 harness — it is a separate, deliberately heavy
exercise. No code for it ships here.
