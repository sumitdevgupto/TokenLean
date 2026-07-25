# `run_ab.py` — true A/B benchmark (proxy vs direct)

`run_ab.py` proves the proxy's savings **without trusting the proxy's own numbers**. Every
request is fired twice — once **direct to the provider** and once **through the proxy** — and
the two are compared on the **provider's own billed usage**, priced identically. It is the
independently-verifiable counterpart to the single-arm [`run_benchmark.py`](run_benchmark.py)
(which uses the proxy's internal counterfactual).

- [How it works](#how-it-works)
- [The two arms & where keys come from](#the-two-arms--where-keys-come-from)
- [Local vs GCP](#local-vs-gcp)
- [Providers & running more than one](#providers--running-more-than-one)
- [Cold vs replay: the two numbers](#cold-vs-replay-the-two-numbers)
- [Datasets & quality gate](#datasets--quality-gate)
- [Spend caps & budget](#spend-caps--budget)
- [Output](#output)
- [CLI reference](#cli-reference)
- [Tenant self-verify](#tenant-self-verify)
- [Troubleshooting](#troubleshooting)

---

## How it works

For each request in the bundled dataset:

1. **Arm A (direct):** `litellm.completion(model="<provider>/<model>")` straight to the provider
   — the proxy is not involved at all. This is the honest "cost without TokenLean".
2. **Arm B (proxy):** the same request body, sent to the proxy, which applies its optimisations
   and calls the provider.

Both arms are measured on **provider-billed tokens** — arm A from the provider's `usage`, arm B
from `_token_opt.tokens_provider_billed` (the real provider prompt tokens the proxy saw; **0** on
a cache hit, because the provider was never called). Cost for both arms comes from the checked-in,
dated [`prices.json`](prices.json), applied identically. Token savings and cost savings are
reported **separately** (routing changes cost without always cutting input tokens).

> The baseline is a **real external call**, not the proxy running with optimisations off — that's
> what makes the result defensible.

---

## The two arms & where keys come from

Two different credentials, because the arms authenticate differently:

| | Auth | Source |
|---|---|---|
| **Arm B (proxy)** | `Authorization: Bearer <proxy key>` | `--api-key` (or `PROXY_API_KEY`). Your **tenant** key `tok-…`. The proxy holds the provider keys server-side; you never pass one to arm B. |
| **Arm A (direct)** | provider key in the **process environment** | native var (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) **or** the `LLM_KEY_<PROVIDER>` fallback, which `apply_litellm_env()` mirrors to the native var litellm expects. |

Per-provider environment variables for the direct arm:

| Provider | Native var | Fallback | Extra required |
|---|---|---|---|
| openai | `OPENAI_API_KEY` | `LLM_KEY_OPENAI` | — |
| anthropic | `ANTHROPIC_API_KEY` | `LLM_KEY_ANTHROPIC` | — |
| gemini | `GEMINI_API_KEY` | `LLM_KEY_GEMINI` | — |
| mistral | `MISTRAL_API_KEY` | `LLM_KEY_MISTRAL` | — |
| groq | `GROQ_API_KEY` | `LLM_KEY_GROQ` | — |
| deepseek | `DEEPSEEK_API_KEY` | `LLM_KEY_DEEPSEEK` | — |
| xai | `XAI_API_KEY` | `LLM_KEY_XAI` | — |
| cohere | `COHERE_API_KEY` | `LLM_KEY_COHERE` | — |
| azure | `AZURE_API_KEY` | `LLM_KEY_AZURE` | `AZURE_API_BASE` |
| bedrock | `AWS_ACCESS_KEY_ID` | `LLM_KEY_BEDROCK` | `AWS_SECRET_ACCESS_KEY`, `AWS_REGION_NAME` |

`run_ab.py` reads **`os.environ`, not `.env`.** How the environment gets populated depends on how
you launch it (see below).

---

## Local vs GCP

The only thing that changes between targets is **`--proxy-url`** (arm B's endpoint) and the
matching **`--api-key`**. Arm A always goes direct to the provider.

**Local (Docker stack on `:4000`)** — easiest via the launcher, which boots the stack, pins a
known-good config, finds a proxy key, loads `.env` credentials, and runs:

```bash
./examples/benchmark/run.sh --ab
./examples/benchmark/run.sh --ab --judge --providers all
```

The launcher discovers the proxy key in this order: `$PROXY_API_KEY` in the shell → a
`PROXY_API_KEY=tok-…` line in `.env` → the first `ROI_PROXY_API_KEY_*` in `.env` → a generated
local key. **Set `PROXY_API_KEY=tok-…` in `.env` to run with a fixed key and pass nothing at the
command line.** Against an **already-running** proxy (e.g. a commercial deploy), add
`--no-pin-config` so the launcher measures the live config and does not rewrite/restart it.

Or call the script directly against an already-running local proxy:

```bash
export OPENAI_API_KEY=sk-...
python examples/benchmark/run_ab.py \
  --proxy-url http://localhost:4000 --api-key tok-<local-key> --tenant bench
```

**GCP (live Cloud Run proxy)** — easiest via the tenant launcher (no Docker, own venv):

```bash
./examples/benchmark/verify.sh \
  --proxy-url https://<your-proxy>.run.app --api-key tok-<tenant-key> --provider-key sk-...
```

Or directly:

```bash
export OPENAI_API_KEY=sk-...
python examples/benchmark/run_ab.py \
  --proxy-url https://<your-proxy>.run.app --api-key tok-<tenant-key> \
  --require-direct --no-cache-flush        # NOTE: no --tenant (the tok- key self-identifies)
```

| | Local | GCP |
|---|---|---|
| `--proxy-url` | `http://localhost:4000` (default) | `https://<your-proxy>.run.app` |
| `--api-key` | local admin key (from `run.sh` / `config/local-keys.json`) | your tenant key `tok-…` |
| `--tenant` | `bench` (admin key namespaces cache) | **omit** — tenant key is self-identifying |
| cache flush | `run.sh` clears the `bench` tenant | `--no-cache-flush` (can't flush a live proxy; not needed) |

---

## Providers & running more than one

**It does not run every provider by default.** Selection is opt-in via `--providers`:

```bash
--providers openai                 # default
--providers openai,anthropic,groq  # a subset
--providers all                    # every provider whose creds are detected
```

With `--providers all`, a provider runs only if its key (native **or** `LLM_KEY_<P>`) is in the
environment **and** its extras are present (azure → `AZURE_API_BASE`; bedrock →
`AWS_SECRET_ACCESS_KEY` + `AWS_REGION_NAME`). Others are **skipped-with-note** — or a **hard
error** under `--require-direct` (the tenant self-verify contract).

### Multiple keys in `.env`

`run_ab.py` reads the environment, not `.env`. The **`run.sh` / `run.ps1` launchers auto-export
every provider credential found in `.env`** when you pass `--ab`, so a multi-key `.env` "just
works" with `--providers all`. The launcher exports only these allow-listed names (the rest of
`.env` is left untouched):

```
LLM_KEY_*   AZURE_API_BASE   AZURE_API_VERSION
AWS_ACCESS_KEY_ID   AWS_SECRET_ACCESS_KEY   AWS_REGION_NAME   AWS_SESSION_TOKEN
```

Example `.env`:

```dotenv
LLM_KEY_OPENAI=sk-...
LLM_KEY_ANTHROPIC=sk-ant-...
LLM_KEY_GROQ=gsk-...
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION_NAME=us-east-1
```

```bash
./examples/benchmark/run.sh --ab --providers all      # runs openai, anthropic, groq, bedrock
```

If you invoke `run_ab.py` **directly** (not through a launcher), export the vars yourself first —
nothing reads `.env` for you in that path.

---

## Cold vs replay: the two numbers

No recognized capability benchmark has repeat/traffic structure — they run each item once. So the
harness reports **two numbers on the same standard items, from one pass**:

- **cold** — first-occurrence items with a cold cache. Savings from the **stateless** optimisations
  only (compression, routing, pruning, lazy tools, schema). The indisputable **floor**.
- **replay** — the same items plus a **disclosed** repeat/paraphrase schedule that exercises the
  cache/dedup lever. The realistic **ceiling**.

One pass yields both because the replay schedule keeps every original **before** its repeats, so
first occurrences are genuine misses and repeats are genuine hits — no cache cross-contamination.
`--mode` selects what runs/reports: `both` (default), `cold`, or `replay`.

---

## Datasets & quality gate

Items come **verbatim** from recognized public datasets (pinned revisions + licenses in
[`DATA_LICENSES.md`](DATA_LICENSES.md)):

| Profile | Dataset | License | Grading |
|---------|---------|---------|---------|
| `rag` | SQuAD v2 | CC BY-SA 4.0 | gold-answer facts |
| `chat` | MT-Bench | Apache-2.0 | LLM judge |
| `swe` | SWE-bench Lite | permissive research | gold-patch paths/symbols facts |
| `code` | HumanEval | MIT | judge / opt-in exec pass@1 |
| `reason` | GSM8K | MIT | final-numeric-answer facts |

`public_dataset.jsonl` + `replay_schedule.json` are checked in (no HF account needed). Regenerate
from source with `python build_public_dataset.py --hf` (`pip install datasets huggingface_hub`);
the shipped copy may be a **structural placeholder** (`build_source: "fixture"` in
`public_dataset.meta.json`) — regenerate before publishing any headline number.

**Quality gate is relative:** a record fails only if arm B (proxy) drops a required fact that arm A
(direct) had — the proxy is never penalised for a fact the direct model itself missed. `--judge`
adds an LLM-judge pass over both arms (warns if the proxy mean drops >0.5 below direct).
`--exec-humaneval` runs HumanEval canonical tests in a subprocess for a true pass@1 (executes model
code — opt-in only).

---

## Spend caps & budget

`--max-spend-per-provider` (default **$1**) caps each provider's cumulative A+B spend;
`--max-spend` (default $10) is the overall ceiling. On a breach the provider is stopped
(`stopped_at_cap: true`) and the run exits non-zero. OpenAI-only default is ~$0.20–0.40 including
the judge; `--providers all` scales roughly linearly with configured-provider count.

Exit codes: `0` clean · `1` config error (before any spend) · `2` a quality regression occurred ·
`3` a spend cap tripped.

---

## Output

- **`ab_results.json`** — per provider → per mode (`cold`/`replay`) → per dataset + total (both
  arms' tokens/cost/calls/cache-hits, `token_saving_pct`, `cost_saving_pct`, facts) + a top-level
  meta block (`prices_as_of`, `seed`, `dataset_sha256`, `providers_run`, `providers_skipped`,
  `spend_total_usd`, `stopped_at_cap`, optional `judge`).
- **`ab_cost_log.jsonl`** — one line per pair (provider, kind, per-arm cost, cache hit).
- Console — a cold-vs-replay summary per provider.

All three are gitignored (the harness + built dataset ship; per-run outputs do not).

---

## CLI reference

| Flag | Default | Meaning |
|---|---|---|
| `--providers` | `openai` | `openai` · `all` · comma list |
| `--mode` | `both` | `cold` · `replay` · `both` |
| `--proxy-url` | `http://localhost:4000` (or `PROXY_URL`) | arm B endpoint |
| `--api-key` | `PROXY_API_KEY` | proxy tenant key `tok-…` |
| `--tenant` | `BENCHMARK_TENANT` | `X-Tenant-ID`; **omit for tenant self-verify** |
| `--prices` | `prices.json` | price table path |
| `--max-spend-per-provider` | `1.0` | per-provider USD cap |
| `--max-spend` | `10.0` | overall USD ceiling |
| `--limit` | `0` (all) | cap trace length per provider |
| `--judge` | off | LLM-judge both arms |
| `--exec-humaneval` | off | run HumanEval tests (executes model code) |
| `--require-direct` | off | hard-fail if a selected provider has no direct-arm key |
| `--no-cache-flush` | off | don't attempt a local cache flush (always for remote) |
| `--timeout` | `180` | per-request timeout (s) |

---

## Tenant self-verify

Onboarded on a live proxy and want to preview savings before first prod traffic? Use `verify.sh`
/ `verify.ps1` — remote, no Docker, makes its own venv, always a **true A/B** (so it **requires
your own provider key**; onboarding never gives you one, BYOK tenants already have theirs). Only
the bundled **public** dataset is sent to the provider — never your data.

```bash
git clone https://github.com/sumitdevgupto/TokenLean.git
cd TokenLean/examples/benchmark
./verify.sh --proxy-url https://<your-proxy>.run.app --api-key tok-... --provider-key sk-...
```

See also [`../../docs/client-onboarding.md`](../../docs/client-onboarding.md).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "no runnable providers" | No provider key in the environment. Export `OPENAI_API_KEY` / `LLM_KEY_<P>`, or use a launcher that loads `.env`. |
| `--providers all` only ran OpenAI | Other keys weren't in the environment. Put them in `.env` and use `run.sh --ab`, or `export` them before a direct `run_ab.py` call. |
| "provider … has no direct-arm credentials" | `--require-direct` (tenant flow) with a missing key — supply `--provider-key` or the provider's env vars. |
| "no price for model …" | The routed/mapped model isn't in `prices.json`. Add it (never priced at $0 silently). |
| azure/bedrock skipped | Missing extras — set `AZURE_API_BASE`, or `AWS_SECRET_ACCESS_KEY` + `AWS_REGION_NAME`. |
| exit 3 | A spend cap tripped — raise `--max-spend-per-provider` / `--max-spend` or lower `--limit`. |
