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
| `rate_limit` | G0 request throttling + per-tier/per-tenant limits + optional monthly quota gate |
| `retention` | Optional periodic purge of aged `audit_events` / `usage_events` / expired `cache_l2` (default OFF) |
| `groups` | Per-group enable/disable + tuning parameters |
| `savings` | Token-savings **estimate** tuning (reporting only — never billed) |

### Per-tenant configuration precedence

At request time the effective config for a tenant resolves in this order (most specific wins):

1. **`tenant_configs` (Postgres)** — per-tenant overrides written by the customer portal
   (G-group knobs, default model, cascade tiers). Deep-merged into the base config;
   propagates within ~60 s (in-process TTL cache).
2. **`tenants.<id>` YAML block** — operator-only escape hatch in the main config.
3. **Base config** — this file (+ `params_dir` files), the platform defaults.

A tenant's default-model override sets both `proxy.default_model` and
`proxy.fallback_request_model`; cascade-tier overrides set the head model of
`groups.G6_routing.tiers.<simple|medium|complex>`. Provider **keys** are never part of
config — per-tenant keys (BYOK, commercial layer) live encrypted in `tenant_provider_keys`
and resolve per (provider, tenant) through `providers/key_resolver.py`. The OSS default
resolver uses the global `LLM_KEY_<PROVIDER>` env / Secret Manager keys, so self-host
behaviour is unchanged.

### rate_limit extras

| Parameter | Default | Description |
|---|---|---|
| `tiers.<tier>` | `{}` | Per-pricing-tier rps/rph defaults (`requests_per_minute`, `requests_per_hour`) |
| `per_tenant.<id>` | `{}` | Per-tenant overrides (most specific after `per_user`/`per_team`) |
| `quota.enabled` | `false` | Monthly request-quota gate from `billing.rate_card.<tier>.included_requests` — 429 `quota_exceeded` past the cap. OSS default OFF |
| `quota.grace_pct` | `10` | Allowance past `included_requests` before rejecting |
| `quota.exempt_tenants` | `[admin, default]` | Never quota-gated |

### retention

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch for the background purge loop |
| `interval_hours` | `24` | How often the purge pass runs |
| `audit_days` | `0` | Purge `audit_events` older than N days (0 = keep forever) |
| `usage_days` | `0` | Purge `usage_events` older than N days (0 = keep forever; clamped to a 400-day billing floor) |
| `cache_l2_expired_cleanup` | `true` | Delete `cache_l2` rows past `expires_at` |

### ip_allowlist

App-level source-IP allowlist (CIDR) for the proxy request path. **Off by default** — OSS/self-host is open; the managed enterprise deploy turns it on. Enforced in core `_authenticate` for the `/v1/*` routes (`net/ip_allowlist.py`).

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch. When off, no IP filtering happens |
| `trust_x_forwarded_for` | `true` | Use the first `X-Forwarded-For` hop as the client IP (Cloud Run / the portal front the proxy). When false, the direct socket peer is used |
| `global_cidrs` | `[]` | CIDRs applied to **all** tenants (e.g. an office / VPN egress). Unioned with each tenant's own list |

A request is allowed iff its source IP is in `global_cidrs ∪ tenant_cidrs`. **Empty global + empty per-tenant ⇒ the tenant is unrestricted.** Per-tenant CIDRs are set from the adminconsole (`PUT /api/v1/admin/tenants/{id}/ip-allowlist`) and stored in the tenant's key metadata; `global_cidrs` is edited here in `config.yaml` (hot-reloaded; the adminconsole exposes it read-only at `GET /api/v1/admin/ip-allowlist`).

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

Optional per-provider `resilience:` sub-block overrides the top-level `resilience:` defaults
(see below) for that provider — e.g. trip a flakier provider's breaker sooner.

### resilience  *(#1 provider failover — top-level section)*

Wraps the two live request paths (non-streaming and streaming `/v1/chat/completions`) with a
circuit breaker, transient-error retry, a per-tenant connection cooldown, and failover to
configured fallback models. Signal scoping keeps tenants isolated: **429s** (rate limits — a
property of the tenant's key) set only that tenant's cooldown and never feed the breaker;
**5xx/timeouts** (true provider health) feed the global per-provider breaker only. Gates
**fail open** — the last viable target is always attempted, so a request is never rejected
without a real provider attempt. Disabled (or omitted) → one attempt, provider error surfaced
(behaviour-preserving). Breaker/cooldown state is per worker (in-process); hot-reloaded values
apply immediately. The G06 tier-cascade, G13 batch, and G10 summary provider calls keep their
own fallback behaviour and are never gated, but their outcomes **feed the breaker** so it sees
all provider traffic.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` in code (`true` in the template) | Master switch. When off, exactly one attempt is made and the original provider error is returned unchanged. |
| `num_retries` | `1` | Transient-error (429/5xx/timeout) retries on the **same** model before failing over. The whole ≥500 family (incl. Cloudflare-style 520–524) is retryable. |
| `failure_threshold` | `5` | Consecutive 5xx/timeout failures that trip a provider's circuit breaker (skipped in favour of fallbacks until cooldown elapses, then one half-open probe). |
| `cooldown_seconds` | `30` | Breaker-open duration **and** per-tenant 429-cooldown TTL. |
| `retry_base_delay` | `0.2` | Exponential-backoff base (seconds) between same-model retries. |
| `model_lockout` | `false` | **Per-model lockout** — finer than the provider breaker: quarantine ONE degraded/deprecated model while the provider's other models keep serving. When on, a model that racks up `model_failure_threshold` model-scoped 5xx/timeout failures is skipped on subsequent requests for `model_lockout_seconds` (then one probe re-tests). Off → provider-breaker-only behaviour is unchanged. Gauge: `token_opt_model_lockout_state{provider,model}` (1=locked). |
| `model_failure_threshold` | `3` | Model-scoped failures that lock one model. Deliberately **lower** than `failure_threshold` so a bad model is isolated before it can open the whole-provider breaker; a fallback model's success then resets the provider breaker, keeping the provider live. |
| `model_lockout_seconds` | `cooldown_seconds` | Lock duration before a single probe re-tests the model (defaults to `cooldown_seconds` when unset). |
| `fallbacks` | `{}` | Map of routed model → ordered list of fallback models, resolved **lazily** (zero per-request cost while the primary is healthy). A fallback whose provider is gated or whose key the tenant lacks is skipped; a fallback's own auth/config error moves on to the next fallback. Empty = retry-only. On failover the winning provider is pinned to the request so cost/provider attribution stays correct; provider-scoped cache params/markers from the primary are scrubbed before a cross-provider fallback call. |

Per-tenant override: set a top-level `resilience:` block in the tenant's config (portal /
`tenant_configs`), which deep-merges over this one. The managed-tier availability SLA
(`billing.rate_card.<tier>.sla_target_pct`, surfaced at `GET /portal/sla`) is backed by this layer.

### savings

Token-savings **estimate** tuning. Reporting only — none of this affects the request-count bill.

| Parameter | Default | Description |
|---|---|---|
| `non_gpt_tiktoken_fallback` | `true` in the template (`false` when the key is unset) | Non-GPT models (Claude/Gemini/Mistral/…) use `cl100k_base` tiktoken locally for a closer-than-`chars/4` ingress baseline. Approximate, no provider API call; affects the savings-% **estimate** only. Env override: `NON_GPT_TIKTOKEN_FALLBACK`. |

**Persisted savings columns** — the metering engine writes these to Postgres `usage_events` (the value/confidence layer, never billed):

- `proxy_optimised_tokens` — the proxy's post-optimisation token estimate (`y`).
- `provider_prompt_tokens` — provider-reported prompt tokens from the response `usage` (`z`), when the provider returns them.
- `group_savings` — per-G-group realised token savings as JSONB (`{"G05": 3400, ...}`), non-zero steps only. Powers the portal's "savings by optimisation" view without querying the Langfuse traces blob.
- `status_code`, `total_duration_ms`, `llm_duration_ms`, `billable` — reliability/latency observability. A non-2xx outcome is persisted as a `billable=false` row so the in-dashboard latency-percentile + error-rate panels have data; **`billable=false` rows are excluded from the request-count invoice** (invoice/quota SQL filters `COALESCE(billable, true)`).

(`x` = `baseline_tokens`. Billing is the request **count**, not tokens — see the two-track model in [request-flow-diagram.md](request-flow-diagram.md).)

**Metering toggles** (`billing.metering.*`, both default `true`):

| Parameter | Default | Description |
|---|---|---|
| `group_savings_enabled` | `true` | Persist the per-group `group_savings` JSONB on each row (C1). Set `false` to drop the extra write. |
| `persist_all_outcomes` | `true` | Persist an observability-only row for non-2xx outcomes (C2). Set `false` to restore the pre-C2 2xx-only write. Never affects billed request counts either way. |

**Observability / FinOps** (`portal.finops.*`) — tunes the Observability tab's spend forecast + anomaly detector:

| Parameter | Default | Description |
|---|---|---|
| `forecast_method` | `linear` | Month-end spend projection method (burn-rate × days in month). |
| `anomaly_ratio` | `2.0` | Flag a day when its cost ≥ ratio × the trailing-median daily cost. Shared by `/portal/finops/anomalies` and the operator `/admin/finops/anomalies`. |
| `anomaly_baseline_days` | `28` | Trailing window the anomaly baseline median is computed over. |

**Invoice history** (`portal.invoices.*`):

| Parameter | Default | Description |
|---|---|---|
| `history_months` | `12` | Max trailing months `GET /portal/invoices/history` recomputes (read-only, no persistence). Caps the `?months=` query param. |

### rate_limit  *(G0 — top-level section)*

Request throttling at the gate (token bucket). Lives at the top level, not under `groups`.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable rate limiting |
| `default.requests_per_minute` | `60` | Per-minute limit applied to all callers without an override |
| `default.requests_per_hour` | `1000` | Per-hour limit applied to all callers without an override |
| `per_user.<id>.requests_per_minute` / `.requests_per_hour` | *(per user)* | Override limits for a specific proxy user/key |
| `per_team.<id>.requests_per_minute` / `.requests_per_hour` | *(per team)* | Override limits for a team |

## Group parameters

> **Quality-impact legend.** ⚠ = tightening this knob for more savings can degrade output
> quality (the description gives the direction); — = no quality trade-off (safety/ops/cost-reporting
> only). Every knob ships at a quality-safe default. See the
> [Tuning Knobs table in the README](../README.md#tuning-knobs--savings-vs-quality) for the
> per-group savings↔quality summary. Tables show the commonly-tuned keys, not every field.

### G1_compression
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable LLMLingua-2 prompt compression |
| `min_tokens_to_compress` | `200` | Skip compression below this token count |
| `compression_ratio_target` | `0.5` | Target ratio (0.5 = 50% compression) |
| `sidecar_url` | `http://llmlingua-svc` | LLMLingua-2 Cloud Run internal URL |
| `compress_user_messages` | `false` | ⚠ Opt-in: also apply compression to `role="user"` messages (default only compresses `system`/`assistant`) |
| `compress_system_prompt` | `false` | ⚠ Opt-in: compress the system prompt (keep off — losing system policy/facts degrades answers) |

Also in the template: `min_chars_to_compress` (100), `reduction_threshold` (0.95), `selective_context_enabled` (false) / `selective_context_max_tokens` (4000), `force_reserve_digit` (true, protects IDs/dates), the Kompress-v2 fallback `kompress_enabled` (true) / `kompress_model` / `kompress_max_new_tokens` (256), and `deterministic_fallback` (false — a zero-LLM regex prose compressor that engages only when neither LLMLingua nor Kompress reduced a message, e.g. sidecar down; protects code/paths/identifiers byte-for-byte).

### G2_template_registry
Versioned prompt templates with per-template token budgets.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable template registry |
| `budgets.<id>.system_prompt_max` | *(per template)* | ⚠ Max system-prompt tokens for this template |
| `budgets.<id>.total_input_max` | *(per template)* | ⚠ Max total input tokens (budget enforcement) |
| `budgets.<id>.output_max` | *(per template)* | ⚠ Max output tokens |
| `budgets.<id>.{version, author, description}` | *(per template)* | Metadata for tracking/stale detection |
| `deprecation_warn_days` / `template_history_ttl_days` / `max_history_per_version` | `30` / `90` / `1000` | Registry housekeeping — **config-first, `TEMPLATE_*` env fallback**; see [appendix](#appendix--knob-coverage-caveats) |

### G3_doc_pipeline
Knowledge ingestion — hybrid RAG chunking + fine-tuning trigger.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable doc pipeline |
| `chunk_size_tokens` | `400` | ⚠ Chunk size for retrieval (smaller = fewer tokens, less context per chunk) |
| `chunk_overlap_tokens` | `50` | Overlap between chunks |
| `rag_fallback.top_k` | `5` | ⚠ Chunks retrieved in fallback (fewer = cheaper, lower recall) |
| `rag_fallback.similarity_threshold` | `0.85` | ⚠ Min score to include a chunk (higher = stricter, may drop relevant context) |
| `rag_fallback.strategies` | `[strict_hybrid, relaxed_hybrid, dense_only, sparse_only]` | Fallback retrieval order |
| `fine_tuning.{enabled, min_docs, stability_days, auto_trigger}` | `true / 100 / 30 / false` | Fine-tuning break-even trigger |
| `tika_sidecar.{enabled, url}` | `false / http://tika-svc:9998` | Apache Tika document extraction |
| `sparse_model` / `dense_model` | `Qdrant/bm25` / `all-MiniLM-L6-v2` | Retrieval models |

*(OOD detection `OOD_SIMILARITY_THRESHOLD` (0.65) / `OOD_MAX_RETRIES` (3) are env-only — see [appendix](#appendix--knob-coverage-caveats).)*

### G4_bypass
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable rules-based bypass |
| `default_confidence_threshold` | `0.7` | ⚠ Min confidence to bypass (lower = bypass more, higher = only high-confidence) |
| `keyword_weight` / `pattern_weight` | `0.4` / `0.6` | ⚠ Weights blended into the confidence score |
| `db_cache_ttl_seconds` | `60` | How long DB-resolved rules are cached before re-fetch |
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
| `classifier` | `cascade` | Complexity classifier: `cascade` (default), `heuristic`, `llm_judge`, or `routellm` |
| `cascade_execution` | `false` | When `classifier: cascade`, run the true tier1→tier2→tier3 execution cascade (call cheap model, escalate only if its answer is inadequate) instead of classify-then-route |
| `strategy` | `priority` | Which model of a chosen tier's list to use (the classifier picks the tier; this picks *within* it). `priority` = the tier's first model (**default — byte-identical to historical behaviour; the savings baseline is unchanged**). `round_robin` = rotate across the tier's models (per worker). `weighted` = deterministic split by `strategy_weights`. `least_latency` = the tier model with the lowest observed served-latency EWMA (fed from real calls; falls back to the first model until measured). `canary` = `canary_pct`% to the tier's **second** model, the rest to the first. All strategies are deterministic (request-id hash / counter / EWMA), never random |
| `strategy_weights` | `{}` | For `strategy: weighted` — `{model: weight}` map; models absent from the map default to weight 1 |
| `canary_pct` | `0` | For `strategy: canary` — percentage of traffic routed to the tier's second (candidate) model |
| `least_latency_alpha` | `0.3` | For `strategy: least_latency` — EWMA smoothing factor (0–1) for the per-model latency estimate; higher reacts faster to recent calls, lower is smoother/slower to react. Hot-reloadable — no redeploy needed to tune it |
| `on_unreachable_tier` | `fallback` | When a routed tier model's provider has **no usable credential** (key or ambient creds): `fallback` serves the caller's own requested model (cost-routing no-ops); `error` returns a clean 503 |
| `cascade_confidence_threshold` | `0.70` | Escalate to the next tier when the current tier's confidence is below this (see *Tuning the cascade threshold* below) |
| `judge_model` | `""` (empty) | Optional model that scores each tier's response for confidence. Empty → a cheap no-LLM response-adequacy heuristic (`response_confidence.*`) is used instead |
| `judge_timeout_ms` | `2000` | Max wait for a `judge_model` confidence score before falling back |
| `max_escalation_cost_usd` | `0.01` | Max cost **delta** (input + expected output, vs the **previous** tier) a single escalation may add. Blocks small requests from jumping into a pricey tier |
| `cascade_cap_to_classified_tier` | `true` | Cap the execution cascade at the tier the request itself classifies as — a plain "medium" query is never pushed to the expensive complex tier. An `x_complexity` request override bypasses the cascade (and this cap) entirely. Set `false` for unbounded confidence-driven escalation |
| `allow_escalation_above_requested` | `false` | When `false`, the cascade never routes to a model costlier than the one the caller requested — escalation only ever saves cost. Set `true` to allow escalating above the requested model |
| `response_confidence.ok` / `.truncated` / `.refusal` / `.empty` | `0.85` / `0.30` / `0.40` / `0.0` | No-judge response-adequacy scores (used when `judge_model` is empty), compared against `cascade_confidence_threshold`: `ok` = clean stop, `truncated` = hit `max_tokens`, `refusal` = content-filter / "I can't help" opening, `empty` = blank content |
| `routellm.enabled` | `true` | Enable RouteLLM sidecar (when classifier=routellm) |
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
- Switch between classifiers by changing `classifier` in config and re-uploading to GCS (no code deploy needed)

> **Over-escalation guards (execution cascade).** With `cascade_execution: true` and no `judge_model`, the cascade judges each tier's *response* (via `response_confidence.*`) — not the request text — so an adequately-answered cheap-tier query is no longer escalated. Two guards bound the blast radius: `cascade_cap_to_classified_tier` (never climb past the request's own complexity tier) and `allow_escalation_above_requested` (never route costlier than the caller's model). These ship safe (`cascade_execution: false`) so default OSS deployments are unaffected; turning the cascade on inherits the guards.

**Tuning the cascade threshold (offline).** This is for `cascade_confidence_threshold` (the cascade escalation dial) — **not** the same as `routellm.threshold`, which is calibrated separately via `routellm.calibrate_threshold` above.

`scripts/validate-cascade.py` measures the accuracy/cost trade-off of the cascade against a ground-truth dataset so you can set `cascade_confidence_threshold` with evidence instead of guessing:

```bash
cp config/cascade-test.yaml.template config/cascade-test.yaml   # edit tiers/judge_model to match your config
python scripts/validate-cascade.py \
  --dataset tests/data/cascade-validation-sample.jsonl \
  --config config/cascade-test.yaml \
  --output reports/cascade-validation-report.json
# proxy must be running; pass the key via --proxy-key or the PROXY_API_KEY env var
```

It runs each case tier-1 → LLM judge → optional tier-3 escalation, sweeps thresholds `[0.5 … 0.95]`, and reports accuracy, cost-saving %, and escalation rate per threshold — plus an optimal threshold (best accuracy keeping >50% cost saving) and per-`workload_tag` recommendations. Set the recommended value back into `cascade_confidence_threshold`. Run it before enabling the cascade, after changing tier models, when onboarding a new workload, or on suspected drift. It makes real (paid) LLM calls — keep validation sets small.

### G7_retrieval
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable RAG retrieval optimisation |
| `chunk_size_tokens` | `256` | Chunk size for RAG documents |
| `top_k` | `3` | ⚠ Retrieve top-K before reranking (fewer = cheaper, lower recall) |
| `top_k_after_rerank` | `1` | ⚠ Inject only top-N after reranking |
| `similarity_threshold` | `0.85` | ⚠ Minimum score to include a chunk (higher = stricter) |
| `max_total_context_tokens` | `4000` | ⚠ Hard cap on total injected context |
| `max_chunk_tokens` | `1000` | ⚠ Per-chunk token guard |
| `rrf_alpha` | `0.5` | Dense-vs-sparse fusion weight (Reciprocal Rank Fusion) |
| `max_age_days` | `null` | ⚠ Freshness soft-filter — drop retrieved chunks older than N days (by the document's `source_date`, else its `ingested_at` stamp). `null`/`0` = off. Chunks with no timestamp (ingested before freshness stamping) are always kept. |
| `jit_require_rag_intent` | `false` | When `true`, JIT retrieval only runs if the caller signalled RAG intent (`rag_query` param or `X-Rag-Collection` header); non-RAG chat requests skip the embed + Qdrant search + rerank. Default `false` auto-extracts a query from the last user message on every request. |

Also present: `dense_model`, `sparse_model`, `reranker_model`, `rrf_k` (60), `jit_retrieval_enabled` (true), `use_pgvector_fallback` (false).

### G8_tools
Lazy tool-definition loading + MCP manifest fetch + scheduled pruning.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable tool loading |
| `max_tools_per_agent` | `20` | ⚠ Prune tools beyond this count (too low → the model loses a tool it needs) |
| `registry_path` | `gs://<bucket>/config/tool-registry.yaml` | Tool registry location |
| `compress_descriptions` | `false` | Opt-in: compress tool/function `description` prose (deterministic regex, zero-LLM; manifests ride every agentic request). Code/paths/identifiers preserved byte-for-byte. |
| `compress_description_fields` | `[description]` | Which string fields to compress when `compress_descriptions` is on |
| `mcp_servers` | `null` | MCP servers — **list of `{url, filter_tools}` dicts** (not URL strings) |
| `pruning.{enabled, inactivity_threshold_days, dry_run_first, schedule}` | `true / 30 / true / 0 2 * * *` | Scheduled removal of unused tools |
| `registry_cache_ttl_seconds`, `mcp_manifest_cache_ttl_seconds`, `mcp_http_timeout_seconds`, `tool_usage_ttl_days` | `300 / 300 / 10 / 90` | **Config-first, `TOOL_*` env fallback** (`TOOL_REGISTRY_CACHE_TTL_SECONDS` etc.) — see [appendix](#appendix--knob-coverage-caveats) |

### G9_context_schema
Prose→schema compaction (Instructor library) with heuristic fallback. **Off by default.**

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` | ⚠ Enable prose→schema compaction (lossy — extracts fields, drops surrounding prose) |
| `prose_min_length_chars` | `80` | ⚠ Min prose length before compaction fires |
| `use_instructor` | `true` | Use Instructor LLM vs heuristic extraction |
| `instructor_model` | `gpt-4o-mini` | Model for compaction |
| `instructor_timeout_ms` | `3000` | Compaction call timeout |
| `instructor_fallback_to_heuristic` | `true` | Fall back to heuristic on failure/timeout |
| `schema_fields` | `null` | Fields to extract (e.g. `{cust: "customer name"}`) |

### G10_memory
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable conversation memory management |
| `sliding_window_turns` | `6` | ⚠ Keep last N turns verbatim (fewer = cheaper, less recent context) |
| `skills_top_k` | `2` | ⚠ Skills retrieved per task |
| `skills_similarity_threshold` | `0.7` | ⚠ Min score to inject a skill |
| `skills_qdrant_enabled` | `true` | `false` → non-Qdrant heuristic skill-injection fallback |
| `summary_model` | `gemini-flash-lite` | Cheap model for history summarisation |

### G11_output
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable output format control |
| `enforce_max_tokens` | `true` | Auto-set max_tokens if not provided |
| `default_max_tokens_multiplier` | `2.0` | ⚠ max_tokens = 2× estimated output (lower = tighter caps) |
| `absolute_default_max_tokens` | `1024` | ⚠ Absolute cap on the heuristic max_tokens (raise if long answers get cut) |
| `tighten_quantile` / `tighten_multiplier` | `0.95` / `1.2` | ⚠ Historical-p95 auto-tightening of max_tokens |
| `verbosity_steering.enabled` | `false` | Append a terseness suffix to steer shorter output (biggest uncovered savings axis; folded into the G05 cache key so terse/verbose answers never mix) |
| `verbosity_steering.level` | `''` | Bundled preset: `lite` \| `full` \| `ultra` (adapted from caveman-shrink, MIT). Safety carve-outs keep security/destructive-action text in normal prose. ⚠ SAVINGS feature — prove with a pitch-test-plan quality-gate run before enabling by default |
| `verbosity_steering.default_suffix` / `per_tenant_suffix` | `''` / `{}` | Explicit suffix overrides (per-tenant wins > default_suffix > preset) |
| `output_holdout.enabled` | `false` | A3 holdout: route a % of traffic to a control cohort that **skips** G11 shaping, so the real output-token reduction can be measured (treatment vs holdout) via the `g11_output_holdout_completion_tokens` metric. Opt-in — control traffic is intentionally un-optimised. |
| `output_holdout.fraction` | `0.05` | Share of requests in the control cohort (0.0–1.0) |
| `output_holdout.sticky_key` | `workflow_id` | Stable cohort key (sticky per conversation); falls back to `user_id` then `request_id` |
| `validate_output` | `off` | Validate a **structured-output** answer (only when the request set `response_format` `json_object`/`json_schema`, or a `json_schema` param). `off` (no-op) / `flag` (record + annotate `_token_opt.output_validation`, non-mutating) / `repair` (one bounded corrective re-ask) / `block` (withhold with a content-filter 200, not cached). Emits `token_opt_output_schema_failures_total`. |
| `repair_fallback` | `flag` | When `repair`'s single re-ask is still invalid: `flag` (annotate + return) or `block` (withhold). Exactly one re-ask — never loops. |
| `repair_max_tokens` | `null` | Cap on the corrective re-ask (`null` → reuse the request's `max_tokens`). |
| `validate_block_message` | *(default text)* | Message returned when a malformed answer is withheld in `block` mode. |

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

### G14_tool_output
Minimises tool-result payloads before they re-enter context.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable tool-output minimisation |
| `field_whitelist.<tool>` | `{}` | ⚠ Keep only these fields per tool (all others dropped — whitelist everything the model needs downstream) |
| `spreadsheet_compression` | `true` | ⚠ Apply Headroom SmartCrusher to CSV/JSON arrays |
| `max_field_tokens` | `200` | ⚠ Truncate any single tool-result text field above this |
| `max_result_tokens` | `500` | ⚠ Truncate/compact an entire tool result above this |

### G15_server_compute
Server-side compute dispatch + Headroom MCP tool hosting.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable server-side compute hooks |
| `headroom_mcp_server` | `true` | Host Headroom MCP tools (`headroom_compress`/`retrieve`/`stats`) |
| `hooks` | `[]` | ⚠ Config-driven transforms (filter/sort/project) applied to tool results before they return |

### G16_agent_arch
Agent-architecture enforcement — bounds system-prompt size and tool count.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable agent-architecture enforcement |
| `max_system_prompt_tokens` | `4096` | ⚠ Truncate oversized system prompts to this budget (too low silently strips instructions; code fallback when the key is absent is also 4096) |
| `max_tools_per_agent` | `20` | ⚠ Prune tools above this count |
| `tool_selection_strategy` | `relevance` | When over the cap: `relevance` (keep most-relevant) vs `order` (first N) |

### G17_loop
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable loop control |
| `max_iterations` | `10` | ⚠ Hard iteration limit per workflow (too low → workflow stops before completing) |
| `starting_budget_tokens` | `10000` | ⚠ Initial token budget per workflow |
| `compact_output_below_tokens` | `500` | ⚠ Inject compact-mode when budget < this |
| `confidence_stop_threshold` | `0.95` | ⚠ Stop early when confidence ≥ this (needs `x_confidence_score`) |
| `wall_clock_timeout_seconds` | `300` | Hard wall-clock stop for a workflow |

### G18_observability
| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable G18 observability (Prometheus counters + savings metrics) |
| `langfuse_enabled` | `false` | Emit Langfuse traces. Requires `enabled` **and** Langfuse keys. Gates only trace emission (Prometheus/savings metrics run regardless). OSS default off; the commercial deploy sets it true. |
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

### g20_prompt_optimizer
Inline application of prompts tuned by the offline optimiser (`scripts/run_prompt_optimization.py`). The heavy optimisation runs out-of-band; the middleware applies the learned templates.

> **Key casing note:** the inline middleware reads this block at `groups.g20_prompt_optimizer` (lowercase). `config/config.yaml.template` still ships the block under the older `G20_prompt_optimization` key, which the middleware never reads — so `enabled: true` there is currently a no-op. That template fix is deliberately deferred (flipping the real key on is a savings-affecting behavior change that needs pitch-test-plan validation, not a docs-only change); until then, set `groups.g20_prompt_optimizer.enabled: true` explicitly to actually turn G20 on in a self-hosted deployment. The portal catalog already uses the correct key.

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
| `min_bytes` | `4096` | ⚠ Skip images smaller than this (raw bytes) |
| `quality` | `75` | ⚠ JPEG quality target (1–95; lower = more compression, less detail) |
| `provider` | `null` | Optional Headroom provider hint; `null` = auto-detect from the active adapter |

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

### G30_guardrails
**Trust & Safety.** Prompt-injection / jailbreak scanner over the user prompt, using a precision-biased heuristic ruleset (`guardrails/injection.py`). Runs **unconditionally right after G24** — before the G04-bypass / G05-cache stage and outside the `skip_groups` loops — so it can't be disabled by adaptive bypass, still guards bypass/cache traffic, and refuses malicious prompts before optimisation spends tokens. A `block` match short-circuits the whole pipeline with an OpenAI content-filter response (HTTP 200, `finish_reason: "content_filter"`), billed once like a bypass. Metrics: `token_opt_guardrail_events_total{category,action}`; PII-free audit rows (`guardrail.flagged` / `guardrail.blocked`). Per-tenant via `tenants.<id>.groups.G30_guardrails`.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Run the guardrail scanner |
| `mode` | `flag` | `allow` (passthrough, no scan) · `flag` (detect + record, pass) · `block` (refuse on match) |
| `threshold` | `0.5` | Minimum rule severity to fire; raise to require higher confidence |
| `scan_roles` | `[user]` | Message roles scanned (untrusted content only by default) |
| `metrics_enabled` | `true` | Emit the Prometheus counter |
| `extra_rules` | `[]` | `[[id, category, severity, regex], …]` operator/managed additions (Enterprise ships a managed red-team ruleset feed) |
| `block_message` | *(built-in)* | Optional custom refusal text |
| `scan_response` | `false` | Opt-in: also scan the **model's output** for injection/jailbreak content (a model echoing an attack payload or emitting unsafe instructions). Default off = shipped behaviour unchanged. Non-streaming responses only. |
| `response_mode` | `flag` | When `scan_response`: `flag` (detect + record) · `block` (withhold the unsafe answer with a content-filter 200; the LLM already ran, but the caller never sees the flagged output, and it is not cached). |
| `response_block_message` | *(built-in)* | Optional custom text for a withheld response. |

### G31_context_trust
**Trust & Safety (Context Quality).** Indirect / RAG prompt-injection defence. G30 scans the untrusted **user** prompt, but retrieval (G07) and memory (G10) then append retrieved documents and stored memories into the prompt as `system` / `tool` messages **after** G30 has already run — so a poisoned document in the vector store or a poisoned stored memory would otherwise reach the model un-inspected. G31 re-runs the same `guardrails/injection.py` scanner over the **assembled** context (the `system` / `tool` roles), right after the G07/G10/G22 stages and **outside the `skip_groups` loops** (non-bypassable). A `block` match short-circuits with an OpenAI content-filter response (HTTP 200, `finish_reason: "content_filter"`) exactly like G30; `strip` mode drops only the offending injected message (or multimodal text part) and continues. Optionally (`pii_mode`) it also runs the **same G29 `PiiDetector`** over that retrieved content, so a PII-laden retrieved document can't reach the model or cache either — G29 runs *before* retrieval so it never sees it. Retrieved-context masking is **irreversible** (`[EMAIL]`, no vault): retrieved PII is not the caller's data to restore, and adding it to the reversible vault would let the model echo it back and have G29's response path rehydrate it. Metrics: `token_opt_context_trust_events_total{category,action}` (PII events use `category=pii:<ENTITY>`); the PII pass records on the dedicated `context_trust_pii_*` context fields + a `source:"retrieved"` audit row, separate from G29's request-side redaction. Per-tenant via `tenants.<id>.groups.G31_context_trust`.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Run the context-trust scanner |
| `mode` | `flag` | Injection: `allow` (passthrough) · `flag` (detect + record, pass) · `block` (refuse on match) · `strip` (drop the poisoned injected content, continue) |
| `threshold` | `0.5` | Minimum rule severity to fire |
| `scan_roles` | `[system, tool]` | Roles that retrieval/memory inject into (`user` is G30's job) — shared by the injection and PII passes |
| `metrics_enabled` | `true` | Emit the Prometheus counter |
| `extra_rules` | `[]` | `[[id, category, severity, regex], …]` additions (Enterprise ships a managed red-team ruleset feed) |
| `block_message` | *(built-in)* | Optional custom injection-refusal text |
| `pii_mode` | `off` | PII pass over retrieved content: `off` · `flag` (detect + record) · `mask` (irreversible `[EMAIL]` placeholders) · `block` (refuse). Uses the G29 engine. |
| `pii_entities` | `[]` (unset) | Subset of `EMAIL/US_SSN/CREDIT_CARD/PHONE/IP_ADDRESS` (+ `phi`); unset = the PII default set. A `phi` token expands to the PHI set. |
| `pii_phi` | `false` | Also scan health identifiers (DEA/NPI/MRN/ICD10) in retrieved content (same precision-biased detectors as G29) |
| `pii_use_presidio` | `false` | Augment the regex tier with Presidio recognisers when installed |
| `pii_block_message` | *(built-in)* | Optional custom refusal text for a `pii_mode: block` |

### G29_pii_redaction
**Trust & Safety.** PII detection + redaction (`guardrails/pii.py`): email, US SSN (separated forms only), Luhn-validated credit card, North-American phone, IPv4 — plus an optional Microsoft Presidio backend for higher recall. Runs **right after G30, before G04/G05** so cache keys/embeddings, RAG, memory, and CCR only ever see redacted content; in `mask` mode it also scrubs the raw `rag_query` snapshot and the `original_messages` copy. Reversible masking (default) shows the model numbered placeholders (`[PII:EMAIL:1]`) and the **non-streaming** response restores them for the data owner (response-side redaction is non-streaming-only in this version). Metrics: `token_opt_pii_redactions_total{entity_type,action}`; PII-free audit rows (`redaction.flagged` / `redaction.applied` — entity types + counts only, never the matched value). Per-tenant via `tenants.<id>.groups.G29_pii_redaction`.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `true` | Run the detector |
| `mode` | `flag` | `off` · `flag` (detect + record) · `mask` (replace in place) · `block` (refuse a request containing PII) |
| `reversible` | `true` | Mask mode: numbered placeholders + response-side restore for the caller |
| `scan_roles` | `[user, assistant, tool]` | Roles scanned (the developer's system prompt is left untouched) |
| `entities` | `[]` | Empty = all built-in **PII**; or narrow, e.g. `[EMAIL, CREDIT_CARD]`. **PHI is opt-in** — add health entities explicitly (`[DEA, NPI, MRN, ICD10]`) or the `phi` shortcut token. |
| `phi` | `false` | `true` = **also** scan health identifiers (DEA & NPI checksum-gated; MRN & ICD-10 require a medical context cue, so a bare number / dotted code isn't flagged). Precision-biased, opt-in — default off keeps PII-only behaviour. Flows through the same `flag`/`mask`/`block` modes. |
| `use_presidio` | `false` | Optional higher-recall backend (needs `presidio-analyzer`; regex tier is the default) |
| `metrics_enabled` | `true` | Emit the Prometheus counter |
| `block_message` | *(built-in)* | Optional custom refusal text |

### webhooks  *(#4 outbound events — [Enterprise])*

Core signal sites emit **PII-free** events through the OSS `events.py` seam; the **[Enterprise]**
delivery product delivers them to a tenant's registered HTTPS endpoints (portal `/portal/webhooks`).
On an OSS/self-host deploy no dispatcher is installed, so emission is a no-op. Event types:
`spend_cap.reached`, `budget.threshold`, `guardrail.block`, `pii.detected`. Delivery is
HMAC-SHA256 signed (`X-TokenLean-Signature: sha256=<hex>` over the raw body, per-endpoint secret
shown once at registration, stored Fernet-encrypted) with bounded exponential-backoff retry and a
Redis dead-letter on final failure.

The `budget.threshold` event is triggered by an **OSS** knob — `rate_limit.spend_cap.warn_pct`
(default `0` = off): when >0, a one-shot event fires the first time monthly spend crosses that
percentage of the cap (de-duped per tenant per month). The delivery-tuning block is read by the
commercial dispatcher:

| Parameter | Default | Description |
|---|---|---|
| `webhooks.timeout_seconds` | `5` | Per-attempt HTTP timeout for a delivery POST |
| `webhooks.max_attempts` | `3` | Total delivery attempts before dead-lettering (bounded exponential backoff) |
| `webhooks.retry_base_delay` | `0.5` | Backoff base (seconds); attempt *n* waits `base × 2ⁿ` |
| `rate_limit.spend_cap.warn_pct` | `0` | **(OSS)** >0 → emit a one-shot `budget.threshold` when spend first crosses this %% of the cap |

### orchestration  *(F2 intent-based multi-agent orchestration — engine OSS, console [Enterprise])*

Routes a request to a registered **downstream agent** (any OpenAI-compatible endpoint) by intent,
instead of the normal LLM. Default **off / no-op** (no agents → byte-identical path). Runs after G06,
before the Stage 3 optimisations; a match short-circuits the LLM call (the agent's answer still runs
response-side groups + billing). Per-tenant override `tenants.<id>.orchestration.*` — a tenant's
`agents` list **replaces** the global one (never merges) so agents never leak across tenants.

| Parameter | Default | Description |
|---|---|---|
| `orchestration.enabled` | `false` | Master switch; `false` → no-op |
| `orchestration.confidence_threshold` | `1` | Min matched `match` keywords to dispatch (else fall back to the LLM) |
| `orchestration.agents` | `[]` | List of `{id, url, match:[keywords], description?, model?, api_key_env?, max_tokens?, timeout_seconds?}`. `url` = the agent's OpenAI-compatible endpoint; `match` = heuristic intent keywords; `max_tokens` = optional per-agent output budget |

The managed registry console (declare/govern agents in the portal), routing-decision audit, and a
managed ML intent classifier are the **[Enterprise]** layer — <https://tokenlean.cbeyond.cloud/>.

### learning  *(F1 agentic learning loop — [Enterprise] managed)*

A managed background job that mines the OSS-core `usage_events` ledger and emits per-tenant G24
adaptive-bypass rules for optimisation groups that run but realise ≈no tokens. No-op in OSS (the miner
ships only in the commercial image). Default **off**. Per-tenant override under
`tenants.<id>.learning.signal_miner.*`.

| Parameter | Default | Description |
|---|---|---|
| `learning.signal_miner.enabled` | `false` | Master switch; `false` → total no-op |
| `learning.signal_miner.interval_seconds` | `900` | Minutes between mining passes (min 60; hot-reloadable) |
| `learning.signal_miner.window_hours` | `168` | Lookback window over `usage_events` |
| `learning.signal_miner.min_samples` | `200` | Ignore cohorts thinner than this (noise floor) |
| `learning.signal_miner.max_avg_saving_per_run` | `5.0` | Tokens/run at/below which a group is "unproductive" |
| `learning.signal_miner.skippable_groups` | `[G01, G20, G22]` | Groups the loop may auto-skip (a hard code denylist protects cache/routing/safety/observability) |

## Appendix — knob coverage caveats

A source audit (2026-07-03) found four classes of knob where the template surface and the code
don't fully line up. Documented here so the reference stays honest; the first group is safe to use
today, the rest are follow-up work.

### A. Surfaced into the template on 2026-07-04
These knobs are read by **wired** middleware via `cfg.get(...)` and were previously code-only defaults;
they were added to `config.yaml.template` on 2026-07-04 so they are visible and tunable. Listed here
for reference (all now appear in their group's section above):

| Group | Key | Default | Effect |
|---|---|---|---|
| `G1_compression` | `kompress_enabled` / `kompress_model` / `kompress_max_new_tokens` | `true` / `microsoft/Kompress-v2-base` / `256` | Kompress-v2 fallback compression for logs/errors |
| `G2_template_registry` | `budget.truncate_enabled` / `budget.truncate_strategy` / `budget.min_keep_user_turns` | `false` / `tail_system` / `1` | ⚠ Truncate over-budget prompts (`budget` singular ≠ `budgets` registry) |
| `G4_bypass` | `db_cache_ttl_seconds` | `60` | DB-rule cache TTL (was a hardcoded constant) |
| `G10_memory` | `skills_qdrant_enabled` | `true` | `false` → non-Qdrant skill-injection fallback |
| `G11_output` | `absolute_default_max_tokens` | `1024` | ⚠ Absolute cap on the heuristic `max_tokens` |
| `G14_tool_output` | `max_field_tokens` / `max_result_tokens` | `200` / `500` | ⚠ Per-field / whole-result truncation caps (were module constants) |
| `G16_agent_arch` | *(fallback alignment)* | — | `_MAX_SYSTEM_PROMPT_TOKENS`/`_MAX_TOOLS_COUNT` absent-key fallbacks realigned 800→4096 / 10→20 to match the template |
| `G27_multimodal` | `provider` | `null` | Override the Headroom provider hint (else auto-detected) |

### B. Config-first knobs — config wins, env is the fallback (item 83a)
These read from `groups.<GROUP>.*` in the hot-reloaded proxy config **first**; if a key is absent
they fall back to the matching env var (or its built-in default). Setting them in `config.yaml` now
takes effect, and existing env-var deployments keep working unchanged. Resolution is global
(`config_loader.get_proxy_config()`) — these are infra knobs (cache TTLs / timeouts / pruning), not
per-tenant quality knobs.

| Group | Config key(s) | Env fallback | Default |
|---|---|---|---|
| `G8_tools` | `registry_cache_ttl_seconds`, `mcp_manifest_cache_ttl_seconds`, `mcp_http_timeout_seconds`, `tool_usage_ttl_days`, `pruning.inactivity_threshold_days` | `TOOL_REGISTRY_CACHE_TTL_SECONDS`, `MCP_MANIFEST_CACHE_TTL_SECONDS`, `MCP_HTTP_TIMEOUT_SECONDS`, `TOOL_USAGE_TTL_DAYS`, `TOOL_INACTIVITY_THRESHOLD_DAYS` | `300 / 300 / 10.0 / 90 / 30` |
| `G2_template_registry` | `deprecation_warn_days`, `template_history_ttl_days`, `max_history_per_version` | `TEMPLATE_DEPRECATION_WARN_DAYS`, `TEMPLATE_HISTORY_TTL_DAYS`, `TEMPLATE_MAX_HISTORY_PER_VERSION` | `30 / 90 / 1000` |

> `G5_cache.temporal_replay_enabled` was **removed** (2026-07-04) — it had no reader (a knob for the
> unwired `G05TemporalActivity` alternate runtime), so it silently did nothing; the key was deleted
> from the template rather than left as a phantom.

### C. Env-only knobs (by design)
| Group | Env var | Default | Purpose |
|---|---|---|---|
| `G13_batch` | `G13_USE_KAFKA`, `KAFKA_BROKERS`, `KAFKA_BATCH_TOPIC`, `KAFKA_CONSUMER_GROUP` | `false` / `localhost:9092` / … | Kafka batch backend (else Redis Streams) |
| `G7_retrieval` | `QDRANT_LOCAL_NOAUTH` | `0` | Skip GCP token fetch on local/non-GCP |
| `G3_doc_pipeline` | `OOD_SIMILARITY_THRESHOLD`, `OOD_MAX_RETRIES` | `0.65`, `3` | ⚠ Out-of-distribution detection for RAG fallback |

### D. Pending wiring (source-audit finding — verify before relying)
The following knobs are read by classes that the audit reports are **not registered in
`pipeline.py`**, so they are inert until the class is wired: `G4` `fuzzy_similarity_threshold`
(`G04DBResolution`), `G5` `temporal_activity_cache` / `idempotent_activities` /
`activity_cache_ttl_seconds` (`G05TemporalActivity`), `G8` `mcp_enabled` (`G08MCPLoader`), `G14`
`combine_tool_calls` (`G14ToolCombining`), `G16` `langgraph_enabled` (`G16LangGraphRuntime`).
