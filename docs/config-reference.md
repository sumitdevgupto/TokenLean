# Configuration Reference — `config/config.yaml.template`

All parameters are externalised in `config.yaml`, hot-reloaded every 60 seconds with no restart.

- **Local Docker:** edit `config/config.yaml` directly (mounted into the proxy container).
- **GCP:** the proxy reads `config.yaml` from GCS — modify and re-upload:
  `gsutil cp config/config.yaml gs://<bucket>/config/config.yaml`

Per-group parameter files under `config/params/` are merged into this config alphabetically at
startup (`params_dir`), so a group's tuning can live in its own file. This reference covers the
optimisation groups **G0–G28** (G26 reserved — 27 implemented); the tables below show the most
commonly tuned keys per group, not every field.

## Top-level sections

| Section | Purpose |
|---|---|
| `proxy` | Port, log level, CORS, default model |
| `providers` | LLM provider endpoints (keys in Secret Manager — not here) |
| `groups` | Per-group enable/disable + tuning parameters |
| `savings` | Token-savings **estimate** tuning (reporting only — never billed) |

### proxy
| Parameter | Default | Description |
|---|---|---|
| `port` | `4000` | Proxy server port |
| `api_key_header` | `Authorization` | HTTP header for proxy API key authentication |
| `log_level` | `INFO` | Logging level |
| `cors_origins` | `["*"]` | CORS allowed origins |
| `default_model` | `gpt-4o` | Fallback model when G6 routing is disabled or no tiers configured |
| `default_provider` | `openai` | Provider used when a model matches no `providers[].model_prefixes` (replaces the old hardcoded OpenAI fallback) |

### providers

A list; each entry maps model name prefixes to a provider adapter. 10 first-class providers ship
configured; add more here. See [extensibility.md](extensibility.md) for the full guide.

| Key | Description |
|---|---|
| `name` | Provider name; selects the adapter and the key var (`LLM_KEY_<NAME>` / `llm-key-<name>`) |
| `model_prefixes` | List of model-name prefixes routed to this provider (first match wins) |
| `api_base` | Endpoint override (required for Azure and for `openai_compatible` providers) |
| `adapter: generic` | Use the config-only `GenericLiteLLMAdapter` (no dedicated adapter class) |
| `litellm_prefix` | Generic mode A: route the model as `<prefix>/<model>` via LiteLLM |
| `openai_compatible: true` | Generic mode B: route via LiteLLM's OpenAI path using `api_base` |
| `api_version` | Azure only — API version |
| `aws_region` | Bedrock only — AWS region (auth via SigV4 env creds, no API key) |
| `supports_reasoning` | Generic: opt in to reasoning-param injection (default off) |

`pricing:` is a flat map of `model-fragment → {input, output}` (USD per 1k tokens, reporting only —
billing is per-request); add a row per new provider model.

### savings

Token-savings **estimate** tuning. Reporting only — none of this affects the request-count bill.

| Parameter | Default | Description |
|---|---|---|
| `non_gpt_tiktoken_fallback` | `true` in the template (`false` when the key is unset) | Non-GPT models (Claude/Gemini/Mistral/…) use `cl100k_base` tiktoken locally for a closer-than-`chars/4` ingress baseline. Approximate, no provider API call; affects the savings-% **estimate** only. Env override: `NON_GPT_TIKTOKEN_FALLBACK`. |

**Persisted savings columns** — the metering engine writes these to Postgres `usage_events` (the value/confidence layer, never billed):

- `proxy_optimised_tokens` — the proxy's post-optimisation token estimate (`y`).
- `provider_prompt_tokens` — provider-reported prompt tokens from the response `usage` (`z`), when the provider returns them.

(`x` = `baseline_tokens`. Billing is the request **count**, not tokens — see the two-track model in [request-flow-diagram.md](request-flow-diagram.md).)

## Group parameters

### G1_compression
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable LLMLingua-2 prompt compression |
| `min_tokens_to_compress` | `200` | Skip compression below this token count |
| `compression_ratio_target` | `0.5` | Target ratio (0.5 = 50% compression) |
| `sidecar_url` | `http://llmlingua-svc` | LLMLingua-2 Cloud Run internal URL |
| `compress_user_messages` | `false` | Opt-in: also apply compression to `role="user"` messages (default only compresses `system`/`assistant`) |

### G4_bypass
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable rules-based bypass |
| `rules` | `[]` | List of bypass rules — see `config/bypass-rules.yaml` |

### G5_cache
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable L1+L2 caching |
| `cache_scope` | `tenant` | `tenant` = key cache by tenant + request content (an answer is reused across providers within a tenant — max savings). `tenant+model` = also key on the **requested** model, so a tenant using several providers never gets one model's cached answer served to another. Per-tenant override: `tenants.<id>.groups.G5_cache.cache_scope`. Default keeps keys byte-identical to prior behaviour (no cache invalidation). |
| `l1_ttl_seconds` | `3600` | Redis exact-match TTL |
| `l2_similarity_threshold` | `0.90` | Semantic similarity threshold (0.88–0.92 range) |
| `l2_ttl_seconds` | `86400` | pgvector semantic cache TTL |

### G6_routing
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable model routing |
| `mode` | `heuristic` | Routing mode: `heuristic` (default), `routellm`, or `custom` |
| `on_unreachable_tier` | `fallback` | When a routed tier model's provider has **no usable credential** (key or ambient creds): `fallback` serves the caller's own requested model (cost-routing no-ops); `error` returns a clean 503 |
| `confidence_threshold` | `0.88` | Cascade escalation threshold — tune per workload (see *Tuning the cascade threshold* below) |
| `routellm.enabled` | `true` | Enable RouteLLM sidecar (when mode=routellm) |
| `routellm.sidecar_url` | `http://routellm-svc` | RouteLLM Cloud Run internal URL |
| `routellm.router` | `mf` | RouteLLM router: `mf` (recommended), `sw_ranking`, or `random` |
| `routellm.threshold` | `0.11593` | Cost threshold (calibrate per workload) |
| `routellm.strong_model` | `gpt-4-1106-preview` | Strong/expensive model for RouteLLM |
| `routellm.weak_model` | `gpt-4o-mini` | Weak/cheap model for RouteLLM |
| `routellm.timeout_ms` | `500` | Max wait for routing decision before fallback |
| `tiers.simple` | `[gemini-flash-lite, ...]` | Simple task models (heuristic mode) |
| `tiers.medium` | `[gemini-flash, ...]` | Medium task models (heuristic mode) |
| `tiers.complex` | `[gemini-pro, ...]` | Complex task models (heuristic mode) |

**RouteLLM Configuration Notes:**
- The `mf` and `sw_ranking` routers require an OpenAI API key for embeddings (stored in Secret Manager as `routellm-openai-key`)
- **No OpenAI key?** The proxy auto-degrades: if `router` is `mf`/`sw_ranking` and no OpenAI key is configured, it falls back to the `causal_llm` router (a local classifier, no embeddings) so routing still works for Anthropic/Gemini-only deployments
- **Tier models must be reachable.** The default tiers/`weak_model`/`strong_model` are OpenAI models — an OpenAI-free deployment should point them at its own provider's models (e.g. `weak_model: claude-haiku-4-5`, `strong_model: claude-sonnet-4-5`). If a routed tier's provider has no credential, G6 logs the unreachable tier(s) once at first use and, per `on_unreachable_tier`, either falls back to the requested model (default) or returns a clean 503
- The `mf` router is recommended for best performance with low latency
- Calibrate the threshold using: `python -m routellm.calibrate_threshold --routers mf --strong-model-pct 0.5`
- If the RouteLLM sidecar is unavailable, the proxy automatically falls back to heuristic routing
- Switch between modes by changing `mode` in config and re-uploading to GCS (no code deploy needed)

**Tuning the cascade threshold (offline).** This is for `confidence_threshold` (the cascade escalation dial) — **not** the same as `routellm.threshold`, which is calibrated separately via `routellm.calibrate_threshold` above.

`scripts/validate-cascade.py` measures the accuracy/cost trade-off of the cascade against a ground-truth dataset so you can set `confidence_threshold` with evidence instead of guessing:

```bash
cp config/cascade-test.yaml.template config/cascade-test.yaml   # edit tiers/judge_model to match your config
python scripts/validate-cascade.py \
  --dataset tests/data/cascade-validation-sample.jsonl \
  --config config/cascade-test.yaml \
  --output reports/cascade-validation-report.json
# proxy must be running; pass the key via --proxy-key or the PROXY_API_KEY env var
```

It runs each case tier-1 → LLM judge → optional tier-3 escalation, sweeps thresholds `[0.5 … 0.95]`, and reports accuracy, cost-saving %, and escalation rate per threshold — plus an optimal threshold (best accuracy keeping >50% cost saving) and per-`workload_tag` recommendations. Set the recommended value back into `confidence_threshold`. Run it before enabling the cascade, after changing tier models, when onboarding a new workload, or on suspected drift. It makes real (paid) LLM calls — keep validation sets small.

### G7_retrieval
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable RAG retrieval optimisation |
| `chunk_size_tokens` | `256` | Chunk size for RAG documents |
| `top_k` | `3` | Retrieve top-K before reranking |
| `top_k_after_rerank` | `1` | Inject only top-N after reranking |
| `similarity_threshold` | `0.85` | Minimum score to include a chunk |

### G10_memory
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable conversation memory management |
| `sliding_window_turns` | `6` | Keep last N turns verbatim |
| `summary_model` | `gemini-flash-lite` | Cheap model for history summarisation |

### G11_output
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable output format control |
| `enforce_max_tokens` | `true` | Auto-set max_tokens if not provided |
| `default_max_tokens_multiplier` | `2.0` | max_tokens = 2× estimated output |
| `verbosity_steering.enabled` | `false` | Append a terseness suffix to steer shorter output |
| `output_holdout.enabled` | `false` | A3 holdout: route a % of traffic to a control cohort that **skips** G11 shaping, so the real output-token reduction can be measured (treatment vs holdout) via the `g11_output_holdout_completion_tokens` metric. Opt-in — control traffic is intentionally un-optimised. |
| `output_holdout.fraction` | `0.05` | Share of requests in the control cohort (0.0–1.0) |
| `output_holdout.sticky_key` | `workflow_id` | Stable cohort key (sticky per conversation); falls back to `user_id` then `request_id` |

### G12_reasoning
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable reasoning budget injection |
| `default_effort` | `medium` | `low` \| `medium` \| `high` — validate per workload |

### G13_batch
Batch accumulation (Redis Streams) plus TOON compact notation — converts JSON arrays of uniform objects into pipe-delimited rows. The `toon_*` knobs gate when TOON fires so it never inflates tokens. All `toon_*` knobs honour per-tenant overrides (`tenants.<id>.groups.G13_batch`).

**Provider-native batch lane (optional, default off):** when `provider_native: true`, a flushed batch is grouped by provider and submitted to a native Batch API (**50% discount** on latency-tolerant traffic) instead of looping `litellm.acompletion` per item at sync price. **OpenAI** uses the OpenAI SDK directly; **Anthropic/Gemini** route through litellm's unified batch API (`custom_llm_provider`), which normalises results to OpenAI shape. A background poller (`poll_batch_jobs` / `start_batch_poller`) maps completed results back to each `request_id`, served by the existing `/v1/batch/results/{id}` endpoint. A missing key, an unsupported provider, or a submit error → graceful fallback to the per-item loop (a failed provider is memoised for the process so it isn't re-attempted every flush). Applies only to requests tagged with `batch_topic`; quality-neutral (same model/inputs). *Anthropic/Gemini batch depends on the installed litellm's batch support for that provider and needs live verification.*

**Flex / `service_tier`:** a request may set `service_tier` (e.g. `"flex"` — ~50% off, latency-tolerant) in its params. It is forwarded only to providers that accept it (OpenAI) and stripped for others (Anthropic/Gemini reject it), via `adapter.supports_service_tier()` in the outgoing-params build.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable batch processing + TOON |
| `provider_native` | `false` | Submit grouped items to the provider Batch API (OpenAI) for the 50% discount; off = per-item sync loop |
| `completion_window` | `"24h"` | OpenAI Batch completion window |
| `poll_interval_seconds` | `30` | How often the background poller checks outstanding native-batch jobs |
| `toon_auto_detect` | `false` | Compress eligible arrays without a manual `schema:` system marker |
| `toon_min_rows` | `2` | Minimum array length to consider for TOON |
| `toon_uniform_threshold` | `1.0` | 0.0–1.0; min fraction of rows sharing the modal key-set (`1.0` = strictly uniform) |
| `toon_allow_nested` | `false` | Allow nested object/array values (usually inflates — keep `false`) |
| `toon_require_net_savings` | `true` | Revert to JSON unless the TOON form is strictly smaller (never inflate) |
| `toon_max_block_chars` | `20000` | Max JSON array-block size to scan (raised from the legacy 2000) |

### G17_loop
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable loop control |
| `max_iterations` | `10` | Hard iteration limit per workflow |
| `starting_budget_tokens` | `10000` | Initial token budget per workflow |
| `compact_output_below_tokens` | `500` | Inject compact-mode when budget < this |

### G18_observability
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable Langfuse tracing |
| `langfuse_host` | `http://langfuse-svc` | Langfuse internal URL (local: `http://langfuse-svc:3000`) |
| `prometheus_enabled` | `true` | Expose `/metrics` Prometheus counters |
| `openllmetry_enabled` | `false` | Enable OTLP auto-instrumentation (set OTLP endpoint first) |
| `et_weights.input` | `1.0` | ET metric input weight |
| `et_weights.cache_read` | `0.1` | ET metric cache-read weight |
| `et_weights.output` | `4.0` | ET metric output weight (output costs ~4× input) |
| `reasoning_rate_multiplier` | `1.0` | Reporting-only price-book refinement: `>1.0` models a reasoning-token surcharge on the `cost_actual` estimate; `1.0` = none. |
| `batch_discount_multiplier` | `0.5` | Reporting-only: multiplier applied to `cost_actual` **only** for requests served via the native async batch lane (`_native_batch`); `1.0` elsewhere. |

### G19_headroom
Structured (AST-aware) pruning of code/JSON/logs/text. Runs on both request and response paths. Additive to G1's natural-language compression.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable structured context pruning |
| `request_side_enabled` | `true` | Compress structured content in request messages |
| `response_side_enabled` | `true` | Compress structured content in responses / tool outputs |
| `min_length_to_compress` | `50` | Skip content shorter than this (chars) |
| `compression_strategies.json` | `{remove_empty, dedupe_keys}` | Drop null/empty fields, dedupe repeated array structures |
| `compression_strategies.code` | `{strip_comments, strip_whitespace, compress_imports}` | Remove comments/blank lines, collapse import blocks |
| `compression_strategies.logs` | `{dedupe_lines, truncate_long_lines: 200}` | Dedupe repeated log lines, truncate long lines |
| `compression_strategies.text` | `{dedupe_sentences, max_sentence_len: 0}` | Collapse duplicate sentences (`0` = no truncation) |

### G20_prompt_optimization
Inline application of prompts tuned by the offline optimiser (`scripts/run_prompt_optimization.py`). The heavy optimisation runs out-of-band; the middleware applies the learned templates.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Apply optimised prompts/templates inline |
| `optimizer` | `builtin` | `builtin` \| `MIPROv2` \| `HRPO` \| `MetaPrompt` \| `dspy` (offline pipeline) |
| `model` | `gpt-4o-mini` | LLM used for optimisation trials |
| `max_prompt_tokens` | `4000` | Upper bound on an optimised prompt |
| `quality_threshold` | `0.95` | Minimum quality score to accept a new prompt |
| `schedule` | `weekly` | How often the offline optimiser re-runs |

### G21_cache_alignment
Reorders messages so shared prefixes are contiguous for provider auto-caching, **and applies a provider cache policy v2**: it emits a deterministic, tenant-scoped OpenAI `prompt_cache_key` (so identical prefixes from a tenant route to the same cache shard, raising the hit rate) and supplies a per-provider `cache_read_multiplier` that lets G18 credit the **real** `cached_tokens` discount from the response into `cost_actual_usd` — replacing the old static `discount_pct` estimate. Final pre-send stage; zero latency / zero quality risk (request content unchanged); cost saving only. Skipped on `bypassed`/`cache_hit`. (See `config/params/` for the full per-provider block.)

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable prefix-cache alignment + cache policy |
| `providers.openai.auto` | `true` | Reorder for OpenAI prefix caching |
| `providers.openai.prompt_cache_key` | `true` | Emit a deterministic, tenant-scoped `prompt_cache_key` (pure upside; set `false` to disable) |
| `providers.openai.prompt_cache_key_len` | `32` | Hex chars of the sha256 cache key |
| `providers.openai.prompt_cache_retention` | *(unset)* | Optional OpenAI cache retention (`"24h"` \| `"in-memory"`); unset = provider default |
| `providers.openai.cache_read_multiplier` | `0.5` | Cost weight for provider-reported cached input tokens (OpenAI bills cache reads at ~50%) |
| `providers.openai.discount_pct` | `50` | Legacy cached-prefix discount % (savings reporting) |
| `providers.anthropic.marker` | `false` | Inject `cache_control` markers (requires Anthropic adapter). Tenant-overridable — set `true` per Claude-heavy tenant to capture the 90% discount. |
| `providers.anthropic.cache_read_multiplier` | `0.1` | Anthropic bills cache reads at ~10% |
| `providers.anthropic.discount_pct` | `90` | Legacy cached-prefix discount % (savings reporting) |
| `providers.gemini.cache_read_multiplier` | `0.25` | Gemini implicit-cache hits bill at ~25% |

### context_editing
Anthropic-native context editing — clears stale tool results / thinking blocks server-side as context fills. Per-tenant opt-in; the OpenAI/Gemini adapters treat it as a no-op, so it is safe to leave enabled cluster-wide and switch on per Claude-routed tenant (`tenants.<id>.groups.context_editing`).

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` | Inject the Anthropic context-editing beta (`context-management-2025-06-27`) |
| `strategy` | `clear_tool_uses_20250919` | `clear_tool_uses_20250919` (clears tool results) \| `clear_thinking_20251015` (clears thinking blocks) |
| `clear_tool_inputs` | `false` | Also clear `tool_use` params, not just results (`clear_tool_uses` only) |

### g22_deduplication
Collapses near-duplicate conversation turns by similarity. Falls back to character n-gram similarity when sentence-transformers is unavailable. *(Config key is lowercase `g22_deduplication`.)*

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable semantic deduplication |
| `dedup_threshold` | `0.92` | Cosine similarity threshold (0.90–0.97 recommended) |
| `embedding_model` | `BAAI/bge-small-en-v1.5` | Embedding model when `use_embeddings: true` |
| `use_embeddings` | `false` | `false` = n-gram fallback (no extra dependency) |
| `tenant_thresholds` | `{}` | Per-tenant threshold overrides |

### G23_streaming_compression
Collapses repeated n-gram patterns in response text (response path); stores the compressed version under `response["x_compressed_content"]` for G10 memory / downstream agents.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable streaming output compression |
| `min_repeat` | `3` | Minimum repetitions before compressing |
| `ngram_size` | `5` | Words per n-gram |

### G24_adaptive_bypass
Runs first in the pipeline. Loads learned rules and populates `ctx.skip_groups` so groups that historically show negative savings for a request pattern are skipped.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable adaptive bypass |
| `rules_file` | `config/adaptive_bypass_rules.yaml` | YAML of learned skip rules (generated + reviewed offline) |

**Tuning the rules (offline).** The `rules_file` is produced by a two-step, human-in-the-loop workflow — nothing is auto-applied:

1. **Analyse** — `scripts/analyse_savings_patterns.py` scans live Prometheus metrics (`--prometheus http://localhost:9090`) — or a directory of ROI run outputs (`--run-dir`) — and writes `analysis/pattern_report.json`, flagging G-groups that consistently *add* tokens for a given request pattern, with a confidence score per candidate.
2. **Review & approve** — `scripts/review_bypass_candidates.py --input analysis/pattern_report.json` presents each candidate for approve / reject / modify and writes the approved skip rules to `config/adaptive_bypass_rules.yaml`, stamping confidence, approver, and timestamp on each. Use `--auto-approve 0.8 --non-interactive` to promote only candidates above a confidence threshold.

G24 picks up the rules file on the normal config-reload cycle (local) or after the deploy uploads it to the config bucket (GCP). With an empty or missing `rules_file`, G24 is a no-op.

### G25_adaptive_reasoning
Classifies request complexity (HIGH/MEDIUM/LOW) and injects `reasoning_effort` before G12 applies the budget. Only fires on reasoning-capable models (o1/o3/o4, Claude). Setting `reasoning_effort` explicitly in the request bypasses classification.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable adaptive reasoning classification |
| `effort_floor` | `low` | Never classify below this effort level |
| `effort_ceiling` | `high` | Never classify above this effort level |
| `extra_reasoning_prefixes` | `[]` | Additional reasoning-model name prefixes |

### G26 *(reserved — not implemented)*
Reserved slot; no configuration.

### G27_multimodal
Compresses inline base64 image blocks before the LLM call via `headroom.compress_image()`, with a SHA256-keyed LRU cache for repeated images. No-op when headroom is absent or there are no image blocks.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable multimodal image optimisation |
| `min_bytes` | `4096` | Skip images smaller than this (raw bytes) |
| `quality` | `75` | JPEG quality target (1–95; lower = more compression) |

### G28_ccr
Contextual Content Reuse. Replaces a large content block (≥ `min_tokens`) with a compact `[CCR:sha256]` reference token before the call, then exposes MCP tools (`headroom_compress`/`retrieve`/`stats`) so the model can fetch the full text on demand. Runs on both request and response paths. Falls back gracefully without `headroom.ccr` or Redis.

**Off by default.** A `[CCR:ref]` is only resolvable by a client that runs the `headroom_retrieve` agent loop (calls the tool, re-sends the result). In a plain pass-through chat completion the model can't resolve the reference and answers from a gutted context, so enable G28 only for cooperating agent clients. The **system instruction is never replaced** unless `compress_system_prompt` is explicitly set true — losing it silently strips the policy/facts the answer depends on.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable context compression & reuse (agent clients only) |
| `min_tokens` | `300` | Minimum block size eligible for CCR |
| `ttl_seconds` | `86400` | Redis TTL for stored content (24h) |
| `expose_mcp_tools` | `true` | Inject `headroom_*` tools into the request |
| `compress_system_prompt` | `false` | Allow CCR to replace the system instruction (keep false for pass-through) |
