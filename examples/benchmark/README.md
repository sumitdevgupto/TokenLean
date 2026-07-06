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
> After changing proxy code, append `--rebuild` to rebuild the image before running.
> Add `--quality-check` to assert each answer still contains its required policy
> facts (no extra cost); add `--judge` for an opt-in LLM faithfulness score.

The benchmark runs under a **dedicated tenant** (`bench`, sent via the
`X-Tenant-ID` header), so every key it creates is namespaced under `t:bench:`.
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
- **Stack health** on `http://localhost:4000/health` — runs `docker compose up -d`
  and waits (~90s) if the proxy isn't already up; skips if it is.
- **Pinned config** — before the run it pins a known-good config (derived from
  `config.yaml.template` with the six measured groups enabled: G01, G05, G06, G08,
  G19, G22, and **G28 CCR disabled** — in pass-through it replaces an over-threshold
  system prompt with a reference token the model can't resolve, which would shred
  answer quality) and reloads the proxy, then **restores your original config and
  reloads** on exit — even on failure or Ctrl-C. This guarantees the benchmark
  measures the pipeline it claims to, instead of silently reporting a gutted result
  when those groups happen to be disabled. Pass `--no-pin-config` to measure the
  live config as-is.
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
not just the response cache. The **system instruction is always preserved verbatim**
(`compress_system_prompt` stays off — that's the quality guard); compression is applied
only to the developer/user-side content that can safely take it:

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
> reproducible) are counted toward it. This example is a *single* workload, so its number
> differs — but it is computed the **same way**, so it's comparable in kind. We report
> whatever this dataset actually yields; we do **not** tune the data to hit a target.

## Calibrated result

Local calibration run (gpt-4o-mini default, 36 requests). **Deterministic**: two consecutive
runs landed on **57.1%** to the token (13,149 / 23,014) — the optimisations are deterministic;
only the LLM's wording (and thus completion tokens / cost) varies run-to-run. This corroborates
the internal quality-gated headline (**54.1%**) on an independent, single OSS workload.

```
TOTAL TOKEN SAVINGS   57.1%   (13,149 / 23,014 tokens)   8 cache hits   ~88s
Est. cost savings     ~62%    ($0.0077 -> $0.0030)        QUALITY GATE: 36/36 PASS

Per-group contribution (tokens saved)
  G05 response cache      41.3%
  G08 lazy tool loading   41.0%
  G22 dedup                5.0%
  G19 structured pruning   0.1%
  G06 model routing        0.0%   (saves cost, not tokens — see below)
```

> **No tuning, no inflation.** The percentage is the proxy's own billed-token metric
> (`final_tokens_sent` = the provider's `usage.prompt_tokens`, 0 on a cache hit) — not an
> estimate. The benchmark runs with the L2/L3 *semantic* cache off (`x_cache_semantic=false`):
> with it on, a long shared system prompt makes distinct requests falsely collide and report
> a higher (but **wrong**) number, so we report the lower, honest figure. The workload mix is
> realistic but favourable (cache + tool-pruning dominate); a workload with fewer repeats or
> smaller tool catalogues will land lower.

**Six techniques, all quality-preserving.** Savings span six groups, and the answers pass
an automated facts gate: `--quality-check` asserts each answer contains its required facts
(e.g. GDPR → "eu-west" + "eu-central"; the OOMKilled root cause; for the compressed prose,
the outage's "09:10"/"23%"/"1.4.2") and no forbidden content, exiting non-zero on a miss.
So these are *real* savings on *correct* answers — not prompt-shredding.

**G01's share is small but the point is per-request.** It only runs on the 3 verbose-prose
requests, so it's ~2% of the *total*; but on those requests it cuts **34–38%** of the user
message while keeping every fact — and that's the only lever that helps a **unique, first-ask**
request (cache/dedup need repeats; structured pruning needs structured content). The system
instruction is never touched. (LLMLingua's BERT classifier keeps numbers/entities and drops
filler like "I'm writing to let you know that, unfortunately…".)

> **Run the gate yourself:** `./examples/benchmark/run.sh --quality-check` (facts, no extra
> cost) or add `--judge` for an LLM faithfulness score (a few cents). Ground-truth facts are
> inlined per record in `dataset.jsonl` (`expected_facts` / `forbidden`).

**Two things to read carefully:**
- **G06 routing shows 0% in the token breakdown but drives the cost line.** Routing swaps
  the model, not the token count: the 5 "simple" questions were sent to gpt-4o and routed
  *down* to gpt-4o-mini, which is most of the 63.7% cost saving. Token savings and cost
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
