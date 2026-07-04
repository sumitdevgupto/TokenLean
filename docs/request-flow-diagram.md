# Token Optimisation Proxy — Complete Request Flow Diagram

## Overview

This document illustrates the complete end-to-end flow when a developer sends a prompt to the token optimisation proxy and receives a response. The framework implements **28 optimisation slots (G0–G28); G26 is reserved, so 27 groups are fully operational** across the files in `src/proxy/middleware/`.

The authoritative ordering lives in `src/proxy/middleware/pipeline.py` (`OptimisationPipeline`). G24 runs **first** so it can populate `ctx.skip_groups`, letting every later stage skip itself per request.

## Visual Request Flow

```
                        ┌─────────────────────────────────────────────────────────────┐
                        │                    DEVELOPER APPLICATION                     │
                        │                                                              │
                        │  POST /v1/chat/completions                                   │
                        │  Headers: Authorization: Bearer <proxy-key>              │
                        │  Body: { messages: [...], model: "...", ... }              │
                        └──────────────────────────────┬──────────────────────────────┘
                                                       │
                                                       ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                         FASTAPI PROXY (local Docker / Cloud Run / GKE)                          │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                                  │
│  ┌──────────────────────┐                                                                       │
│  │  Auth & Context      │  1. Validate proxy key (hash compare)                               │
│  │  ─────────────────    │  2. Resolve tenant (redis_prefix, qdrant_collection, pricing_tier)   │
│  │  api_key_manager     │  3. Create RequestContext (UUID, baseline tokens, savings record)     │
│  │  tenancy.resolver    │  4. Resolve provider adapter; start OTel + Langfuse trace             │
│  └──────────┬───────────┘                                                                       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  STAGE 1 — GATEKEEPING                                                               │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  G00 Rate Limit       → Token bucket (Redis) → 429 if exceeded                        │       │
│  │  G24 Adaptive Bypass  → load learned rules → populate ctx.skip_groups (runs first)  │       │
│  │  ────────────────────────────────────────────────────────────────────────────────────│       │
│  │  G04 Bypass Rules     → DB-first PostgreSQL → confidence score → zero-cost response │       │
│  │  G05 Cache            → L1 Redis → L2 pgvector → L3 headroom SemanticCache → cached  │       │
│  │  G06 Routing          → cascade / heuristic / RouteLLM → ctx.routed_model           │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  STAGE 2 — TOKEN REDUCTION (Into the LLM)   [each stage honours ctx.skip_groups]     │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  G01 Compression   → base→role→task→dynamic + Selective Context + LLMLingua-2       │       │
│  │  G27 Multimodal    → compress inline base64 images (headroom.compress_image + LRU)  │       │
│  │  G02 Templates     → versioning, deprecation, per-template budget tracking            │       │
│  │  G20 Prompt Opt    → apply offline-optimised prompts/templates inline                 │       │
│  │  G07 Retrieval     → hybrid dense+sparse Qdrant RRF → ChunkGuard → injected context │       │
│  │  G08 Tool Loading  → intent-based filtering + MCP lazy-load manifest + pruning      │       │
│  │  G28 CCR (req)     → replace repeated blocks with [CCR:sha256] + expose headroom MCP │       │
│  │  G19 Headroom (req)→ AST-aware pruning of code/JSON/logs/text                        │       │
│  │  G09 Schema        → prose detection → Instructor typed output → compact handoffs    │       │
│  │  G10 Memory        → sliding window + Mem0 + Zep + Qdrant skills → injected recall │       │
│  │  G22 Dedup         → collapse near-duplicate conversation turns (cosine / n-gram)    │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  STAGE 3 — PARAMETER INJECTION (Inside the LLM)   [honours ctx.skip_groups]          │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  G16 Agent Arch       → anti-pattern advisories (LangGraph/Temporal guidance)       │       │
│  │  G11 Output Format    → max_tokens enforcement + JSON schema + p95 feedback prep    │       │
│  │  G25 Adaptive Reason. → classify complexity → set reasoning_effort (before G12)     │       │
│  │  G12 Reasoning Budget → effort=low/med/high → provider-specific budget params       │       │
│  │  G13 Batch/TOON       → code-substitution (#C1) + batch queue → 202 if deferred    │       │
│  │  G17 Loop Control     → budget + max_iters + confidence_stop + InterAgentState      │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  STAGE 4 — FINAL ALIGNMENT + LLM CALL                                               │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  G21 Cache Alignment → reorder for provider prefix-caching (final pre-send stage)   │       │
│  │  Record final_tokens_sent → litellm.acompletion() → provider response                │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  STAGE 5 — RESPONSE OPTIMISATION                                                     │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  G14 Tool Output    → field projection + truncation + parallel combining            │       │
│  │  G28 CCR (resp)     → compress repeated response blocks for downstream reuse         │       │
│  │  G23 Streaming Comp.→ collapse repeated n-grams → response["x_compressed_content"]  │       │
│  │  G19 Headroom (resp)→ AST-aware pruning of response / tool outputs                   │       │
│  │  G15 Server Compute → hook-based filter/sort/project + headroom MCP dispatch        │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  STAGE 6 — FEEDBACK & OBSERVABILITY                                                  │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  G11 Feedback Loop → record output tokens → Redis p95 → auto-tighten future          │       │
│  │  G18 Observability → Prometheus counters + Langfuse trace + usage records           │       │
│  │  G05 Store Cache   → save to L1 Redis + L2 pgvector (skip if bypass/cache-hit)    │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐       │
│  │  RESPONSE ENRICHMENT                                                                 │       │
│  ├─────────────────────────────────────────────────────────────────────────────────────┤       │
│  │  • Attach _token_opt metadata (baseline/final tokens, savings by step, cost*)       │       │
│  │  • x-token-opt-state header (G17 InterAgentState base64 JSON)                      │       │
│  │  (* cost fields are config-priced estimates — see Savings Calculation)               │       │
│  └─────────────────────────────────────────────────────────────────────────────────────┘       │
│             │                                                                                    │
│             ▼                                                                                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
                        │
                        │  HTTP 200 OK (or 202 if batch deferred, 429 if rate limited,
                        │               401 if auth failed, 502 if provider error)
                        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                              DEVELOPER APPLICATION                                               │
│                              Response + _token_opt metadata                                        │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Pipeline Order (authoritative — `pipeline.py`)

Request path:
`G0 → G24 → G4 → G5 → G6 → G1 → G27 → G2 → G20 → G7 → G8 → G28 → G19 → G9 → G10 → G22 → G16 → G11 → G25 → G12 → G13 → G17 → G21`

Response path:
`G14 → G28(resp) → G23 → G19(resp) → G15 → G11(feedback) → G18 → G5(store)`

> **G24 runs first** to populate `ctx.skip_groups`. Every Stage-2/Stage-3 group is wrapped in a
> `if _group in ctx.skip_groups: continue` guard, so G24 can disable any of them per request.
> **G26 is a reserved slot** with no implementation.

## End-to-End Flow

### 1. Request Entry
Developer application sends `POST /v1/chat/completions` with `Authorization: Bearer <proxy-key>` and request body `{ messages, model, ... }`.

### 2. Authentication (`auth/api_key_manager.py`)
- Extract Bearer token from Authorization header
- SHA256 hash the API key
- Validate hash (Secret Manager on GCP; local key file in dev — both hash-only, never raw keys)
- Return user_id if valid, else 401 Unauthorized

### 3. Tenant Resolution (`tenancy/resolver.py`, `tenancy/config.py`)
- Resolve tenant from headers / API-key hash → `tenant_id`, `redis_prefix`, `qdrant_collection`, `pricing_tier`
- Shallow-merge per-tenant config overrides into `ctx.config`; load Postgres-stored overrides
- Degrades to the `"default"` tenant when no multi-tenant context is present

### 4. Request Context Creation (`middleware/__init__.py`)
- Generate unique request_id (UUID)
- Extract messages, model, params from request body
- Load config (GCS on GCP, local file in dev — hot-reloaded every 60s)
- Calculate baseline token count (before any optimisation)
- Create `SavingsRecord`; initialise `RequestContext` with original + mutable messages

### 5. Observability Start (`middleware/langfuse_tracing.py`, `tracing/otel.py`)
- Resolve `ctx.provider_adapter` for the routed model
- Start the pipeline-level OTel span and the Langfuse trace before any middleware runs

### 6. Request Pipeline (`middleware/pipeline.py`)

**STAGE 1 — Gatekeeping**

**G00: Rate Limit** (`g00_rate_limit.py`)
- Token-bucket algorithm with Redis backend; per-minute and per-hour limits
- Scope: per-user, per-team, default fallback; fails open if Redis unavailable
- If exceeded: raise `RateLimitExceeded` → 429 with Retry-After

**G24: Adaptive Bypass** (`g24_adaptive_bypass.py`) — *runs first after rate limiting*
- Loads learned rules (`config/adaptive_bypass_rules.yaml`) generated + reviewed offline
  via `scripts/analyse_savings_patterns.py` → `scripts/review_bypass_candidates.py`
  (see *Tuning the rules* in `docs/config-reference.md`)
- Populates `ctx.skip_groups` so groups with historically negative savings for this request
  pattern are skipped downstream

**G04: Bypass Rules** (`g04_bypass.py` + `g04_db_resolution.py`)
- Database-first resolution: PostgreSQL rules (`pg_trgm` fuzzy + exact hash, 60s cached) with
  config-file fallback (`bypass-rules.yaml`)
- Confidence scoring: keyword (40%) + pattern (60%); dispatch `static_response` or `backend_url`
- If match: set `ctx.bypassed=True`, skip LLM call entirely

**G05: Cache** (`g05_cache.py` + `g05_cache_gptcache.py` + `g05_temporal_activity.py`)
- L1: exact-match Redis (SHA256 of normalised prompt)
- L2: semantic pgvector cosine similarity (threshold from config)
- L3: headroom `SemanticCache` (hybrid scorer; falls back gracefully when headroom is absent)
- Auto-TTL: dynamic TTL from hit rates; Temporal activity replay for idempotent steps
- If hit: set `ctx.cache_hit=True`, return cached response

**G06: Routing** (`g06_routing.py`)
- Classifier modes: `heuristic`, `llm_judge`, `cascade`, `routellm`
- Optional true 3-tier cascade (cheap → confidence check → escalate) with cost-bounded escalation
- Sets `ctx.routed_model` (may differ from the requested model)

**STAGE 2 — Token Reduction (Into the LLM)** *(every group honours `ctx.skip_groups`)*

**G01: Compression** (`g01_compression.py`)
- Layered composition (base → role → task → dynamic), build-time pre-compression of static layers
- `SelectiveContextPruner` relevance-based pruning before LLMLingua-2 sidecar compression
- Compresses system/assistant messages only by default (user messages opt-in)

**G27: Multimodal Optimizer** (`g27_multimodal_optimizer.py`)
- Compresses inline base64 image blocks via `headroom.compress_image()`
- SHA256-keyed in-process LRU cache avoids re-compressing identical images
- No-op when headroom is absent or no image blocks are present

**G02: Template Registry** (`g02_template_registry.py`)
- `TemplateMetadata`: versioning, author, sunset; 30-day deprecation auto-flag
- Per-version token-count history in Redis; per-template budget validation

**G20: Prompt Optimizer** (`g20_prompt_optimizer.py`)
- Applies prompts/templates tuned by the offline optimiser inline
- Heavy optimisation runs out-of-band (`scripts/run_prompt_optimization.py`, Opik/DSPy)

**G07: Retrieval** (`g07_retrieval.py` + `g07_pgvector_fallback.py`)
- JIT toggle via config or `x_jit_retrieval` param
- Hybrid dense+sparse (SPLADE/BM25) via Qdrant RRF fusion; pgvector fallback option
- ChunkGuard size limits; cross-encoder rerank → top-1/2 before injection

**G08: Tool Loading** (`g08_tool_loading.py` + `g08_mcp_loader.py`)
- Intent-based tool filtering; MCP manifest lazy-load from MCP servers (Redis-cached)
- Tool-usage analytics; scheduled pruning of tools inactive for 30 days

**G28: Context Compression & Reuse — request side** (`g28_ccr.py`)
- Replaces repeated verbatim blocks (≥ `min_tokens`) with a compact `[CCR:sha256]` reference token
- Stores full text in Redis (TTL) and injects `headroom_compress/retrieve/stats` MCP tools so the
  model can fetch original text on demand
- Falls back gracefully when `headroom.ccr` or Redis is unavailable

**G19: Headroom — request side** (`g19_headroom.py`)
- AST-aware structured pruning of code / JSON / logs / text (additive to G1's NL compression)
- Per-type strategies: drop empty JSON fields, strip code comments/whitespace, dedupe log lines

**G09: Context Schema** (`g09_context_schema.py`)
- Detect prose-heavy inter-agent context; Instructor-driven typed extraction with heuristic fallback
- Enforce compact typed schema for structured handoffs

**G10: Memory** (`g10_memory.py` + `g10_mem0_adapter.py`)
- Sliding window (last N turns verbatim, summarise older); Qdrant-backed skills retrieval
- Optional Mem0 long-term entity memory and Zep conversation-graph memory (off by default)

**G22: Deduplication** (`g22_deduplication.py`)
- Collapses near-duplicate conversation turns by cosine similarity
- Falls back to character n-gram similarity when sentence-transformers is unavailable

**STAGE 3 — Parameter Injection (Inside the LLM)** *(honours `ctx.skip_groups`)*

**G16: Agent Architecture** (`g16_agent_arch.py` + `g16_langgraph_runtime.py` + `g16_temporal_runtime.py`)
- Anti-pattern advisories (role stacking, oversized system prompts, tool sprawl)
- Optional `LangGraphRuntime` / `TemporalRuntime` for durable, budget-aware agent execution

**G11: Output Format** (`g11_output_format.py`)
- Enforce `max_tokens` (default 2× expected); inject JSON schema / `response_format`
- Provider-specific structured output via `ctx.provider_adapter`

**G25: Adaptive Reasoning** (`g25_adaptive_reasoning.py`)
- Classifies request complexity (HIGH/MEDIUM/LOW) and sets `reasoning_effort` before G12
- Only fires on reasoning-capable models (o1/o3/o4, Claude); explicit `reasoning_effort` bypasses it

**G12: Reasoning Budget** (`g12_reasoning_budget.py`)
- Config-driven effort levels mapped to provider-specific params:
  - OpenAI o1/o3: `reasoning_effort`; Anthropic: `thinking.budget_tokens`; Gemini: `thinking_config`
- Optional reasoning-suppression prompt injected at low/medium effort

**G13: Batch Processing** (`g13_batch.py` + `g13_kafka.py` + `g13_toon.py`)
- TOON compact notation (`#C1`/`#P1` code substitution, legend transmitted once)
- Batch accumulation via Redis Streams (Kafka alternative); if batchable → `ctx.batch_deferred=True`, return 202
- Optional **provider-native batch lane** (`provider_native: true`, default off): a flushed batch is grouped by provider and submitted to a native Batch API for the 50% discount — **OpenAI** via direct SDK, **Anthropic/Gemini** via litellm's unified batch API; `poll_batch_jobs`/`start_batch_poller` map results back to each `request_id` for `/v1/batch/results/{id}`. Missing key / unsupported provider / errors fall back to the per-item loop (failed provider memoised per process)

**G17: Loop Control** (`g17_loop_control.py`)
- Per-workflow token budget; stop on max-iters / confidence threshold / wall-clock timeout
- Compact-output instruction injected below budget; `InterAgentState` propagated via HTTP header

**STAGE 4 — Final Alignment + LLM Call**

**G21: Cache Alignment + Cache Policy v2** (`g21_cache_alignment.py`) — *final pre-send stage*
- Reorders messages so shared prefixes are contiguous for provider prompt-caching
- Emits a deterministic, tenant-scoped OpenAI `prompt_cache_key` (raises hit rate) via the provider adapter; skipped on `bypassed`/`cache_hit`
- On the response path, G18 credits the **real** `cached_tokens` discount into `cost_actual_usd` using the adapter's per-provider `cache_read_multiplier` (OpenAI 0.5 / Anthropic 0.1 / Gemini 0.25) — replacing the old static estimate
- Zero latency / zero quality risk; cost saving only (50–90% discount on the cached prefix)

**Final Token Count Recording**
- `ctx.savings.final_tokens_sent = ctx.current_token_count` — count AFTER all G0–G21 request-side optimisations

**LLM Provider Key Resolution + Call** (`auth/api_key_manager.py`, `main.py`)
- Map `ctx.routed_model` → provider, fetch provider key (Secret Manager / `LLM_KEY_{PROVIDER}` env in dev)
- `litellm.acompletion(model=ctx.routed_model, messages=ctx.messages, api_key=..., **filtered_params)`
- Filtered params strip `_`/`x_`-prefixed internals; reasoning params and `service_tier` (Flex) are stripped for providers that don't support them (via the adapter); AuthenticationError → 401, RateLimitError → 429, other → 502

### 7. Response Pipeline (`middleware/pipeline.py`)

**STAGE 5 — After the Response**

**G14: Tool Output** (`g14_tool_output.py` + `g14_tool_combining.py`)
- Field projection (whitelist) + truncation (`max_field_tokens`, `max_result_tokens`)
- `ToolCallBatcher` runs independent tool calls in parallel with dependency-graph ordering

**G28: CCR — response side** (`g28_ccr.py`)
- Compresses repeated verbatim blocks in the response for downstream reuse / memory

**G23: Streaming Compression** (`g23_streaming_compression.py`)
- Collapses repeated n-gram patterns in response text → `response["x_compressed_content"]`

**G19: Headroom — response side** (`g19_headroom.py`)
- AST-aware pruning of responses / tool outputs (same strategies as the request side)

**G15: Server-Side Compute** (`g15_server_compute.py` + `g15_mcp_dispatch.py`)
- Hook-based `filter_fn` / `sort_key` / `field_project` / `top_n`; headroom MCP tool dispatch
- Offloads filter/sort/project to the server before the LLM re-ingests results

**STAGE 5b — Feedback Loop**

**G11: Output Format** (`process_response()`)
- Record actual output token count in a Redis ZSET; auto-tighten future `max_tokens` from p95

**STAGE 6 — Observability + Cache Store**

**G18: Observability** (`g18_observability.py` + `langfuse_tracing.py`)
- Prometheus counters (`token_opt_*_tokens_total` by model/team/feature, tenant-labelled)
- Langfuse trace completion with full savings metadata; usage records written for export
- Optional OpenLLMetry OTLP export

**Cache Storage (G05)**
- Store response in L1 Redis (Auto-TTL) + L2 pgvector (semantic); skip if `cache_hit` or `bypassed`

### 8. Response Enrichment (`main.py`)
- Attach `_token_opt` metadata: request_id, user_id, model_requested, routed_model,
  baseline_tokens, final_tokens_sent, response_tokens, total_abs_saving, total_pct_saving,
  cost_baseline_usd / cost_actual_usd / cost_saving_usd*, effective_token_et,
  cache_hit / cache_level / bypassed, routing fields, and `step_savings: { G01: {...}, ... }`
- Set `x-token-opt-state` header (G17 `InterAgentState`, base64-encoded JSON)
- *Cost fields are **config-priced estimates**, not invoice-grade — see Savings Calculation.

### 9. HTTP 200 OK with JSON response
Response includes LLM output + `_token_opt` metadata.

## Short-Circuit Paths

### Path A: Rate Limited (G00)
```
Request → Auth → Context → G00 (rate exceeded) → 429 Too Many Requests + Retry-After
```

### Path B: Bypass (G04)
```
Request → Auth → Context → G00 → G24 → G04 (bypass match) → Return bypassed response
```
- No LLM call; zero token cost; immediate response (static or backend API)

### Path C: Cache Hit (G05)
```
Request → Auth → Context → G00 → G24 → G04 (no bypass) → G05 (L1/L2/L3 hit) → Return cached
```
- No LLM call; sub-ms (L1), ~10ms (L2), ~50ms (L3)

### Path D: Batch Deferred (G13)
```
Request → Auth → Context → G00 … G13 (batchable) → Return 202 Accepted
```
- LLM call deferred to batch consumer; poll `GET /v1/batch/results/{request_id}`

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/chat/completions` | POST | Main proxy endpoint (OpenAI-compatible) |
| `/v1/models` | GET | List available models from configured providers |
| `/v1/batch/results/{request_id}` | GET | Poll for deferred batch results |
| `/ingest-doc` | POST | GCS pub/sub webhook for document ingestion (G03) |
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus scrape endpoint |
| `/admin/alert-webhook` | POST | Alertmanager budget alert receiver |
| `/admin/budget-status` | GET | Team/feature token usage and remaining budget |
| `/admin/usage-export` | POST | JSONL export of usage records (over Postgres `usage_events`)* |
| `/admin/tool-governance` | GET | List stale tools with no recent calls |

> \* **`/admin/usage-export`** is a read-only data right: a JSONL export of your own usage records
> from the `usage_events` table. Exported `cost_saved_usd` is a **config-priced estimate, not
> provider-reconciled billing** (see Savings Calculation).

## Document Ingestion Pipeline (G03)

G03 is a **companion pipeline, not an inline request stage** — it populates the RAG vector store
that G07 later retrieves from. It runs **asynchronously, outside the `/v1/chat/completions` path**,
as a Cloud Run **Job** triggered by a GCS object notification.

### End-to-end flow

```
                        ┌──────────────────────────────┐
   1. Upload doc  ────► │  GCS bucket (docs/…)          │
                        └───────────────┬──────────────┘
                                        │ 2. Object-finalize notification
                                        ▼  (Pub/Sub push)
                        ┌──────────────────────────────┐
                        │  Proxy  POST /ingest-doc      │  main.py:442
                        │  body: { bucket, name }       │
                        └───────────────┬──────────────┘
                                        │ 3. trigger_doc_ingestion(bucket, name)
                                        ▼   (g03_doc_pipeline.py:40 → run_v2 JobsAsyncClient)
                        ┌──────────────────────────────┐
                        │  Cloud Run Job                │  src/doc-pipeline/pipeline.py
                        │  env: GCS_BUCKET, GCS_OBJECT  │
                        └───────────────┬──────────────┘
                                        │ 4. download → extract → chunk → embed → upsert
                                        ▼
                        ┌──────────────────────────────┐
                        │  Qdrant collection            │  read later by G07 retrieval
                        └──────────────────────────────┘
```

### Job steps (`src/doc-pipeline/pipeline.py:run()`)

1. **Download** the object from GCS (`GCS_BUCKET` / `GCS_OBJECT`).
2. **Extract text** — Apache Tika sidecar when `USE_TIKA=true`, else Unstructured, else UTF-8 decode.
3. **Strip boilerplate** — headers/footers, base64 blobs, residual HTML, page numbers.
4. **Tables → CSV** — markdown tables compacted to CSV rows (≈40–60% fewer tokens on table-heavy docs).
5. **Chunk** — 256–512-token overlapping chunks (`CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS`).
6. **Summarise oversized chunks** — any chunk over `MAX_CHUNK_TOKENS` (4,000) is condensed with a cheap model.
7. **Embed** — dense (MiniLM-L6-v2) + sparse (BM25/SPLADE via fastembed).
8. **Upsert** to the Qdrant collection as named `dense`/`sparse` vectors.

### Key environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GCS_BUCKET` / `GCS_OBJECT` | — | The object to ingest (injected per-run as container overrides) |
| `QDRANT_URL` / `QDRANT_COLLECTION` | `…:6333` / `rag_docs` | Target vector store + collection |
| `USE_TIKA` / `TIKA_SIDECAR_URL` | `false` / `http://tika-svc:9998` | Prefer Tika extraction |
| `CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS` | `400` / `50` | Chunk sizing |
| `MAX_CHUNK_TOKENS` | `4000` | Oversized-chunk summarisation threshold |
| `DOC_PIPELINE_JOB_NAME` / `GCP_REGION` | `token-opt-doc-pipeline` / `us-central1` | Which Cloud Run Job the webhook launches |

> **Scope of what's implemented.** The proxy does **not** expose a raw file-upload API — a document
> must already be in GCS; ingestion is triggered by the object-finalize notification hitting
> `POST /ingest-doc`. The webhook has no auth/tenant guard, and the Job's `QDRANT_COLLECTION`
> defaults to a shared `rag_docs` collection (not the per-tenant `ctx.qdrant_collection` used on the
> read path). Multi-tenant deployments must set `QDRANT_COLLECTION` per ingestion run accordingly.

## Key Data Structures

### RequestContext (`middleware/__init__.py`)
```python
@dataclass
class RequestContext:
    request_id: str
    user_id: str
    tenant_id: str                     # resolved tenant ("default" if single-tenant)
    redis_prefix: str                  # tenant-scoped Redis key prefix
    qdrant_collection: str             # tenant-scoped Qdrant collection
    pricing_tier: str                  # tenant pricing tier
    original_messages: List[Dict]      # Immutable snapshot (deep copy)
    messages: List[Dict]               # Mutable (optimised in place)
    model: str                         # Requested by developer
    routed_model: str                  # After G06 routing
    params: Dict                       # LLM parameters
    config: Dict                       # Full config (+ per-tenant overrides)
    savings: SavingsRecord

    bypassed: bool = False             # G04 set True → skip LLM call
    cache_hit: bool = False            # G05 set True → return cached response
    cache_level: Optional[str] = None  # "L1" | "L2" | "L3"
    cache_response: Optional[Dict] = None
    batch_deferred: bool = False       # G13 batched this request
    skip_groups: List[str] = []        # G24 populates → later stages skip themselves
    otel_span: Optional[Any] = None
    langfuse_trace: Optional[Any] = None
    provider_adapter: Optional[Any] = None

    @property
    def current_token_count(self) -> int:
        return count_messages_tokens(self.messages, self.model)
```

### InterAgentState (`middleware/g17_loop_control.py`)
```python
class InterAgentState(BaseModel):
    token_budget_remaining: int
    workflow_turn: int
    max_iterations: int
    confidence_score: Optional[float] = None
    wall_clock_elapsed_seconds: Optional[float] = None
    stop_reason: Optional[str] = None

    def to_header_value(self) -> str: ...   # Base64-encoded JSON
    def from_header_value(cls, v) -> Self: ...
```

## External Dependencies

| Service | Purpose | Called By |
|---------|---------|-----------|
| **Secret Manager / local key file** | Proxy API key validation, LLM provider keys | `auth/api_key_manager.py` |
| **GCS / local file** | config.yaml storage, tool registry | `config_loader.py`, `g08_tool_loading.py` |
| **Redis** | L1 cache, sessions, rule stats, template meta, tool usage, rate limits, budgets, batch streams, CCR store, usage records | G00, G02, G04, G05, G08, G10, G11, G13, G17, G18, G28 |
| **PostgreSQL + pgvector** | L2 cache, embeddings, bypass rules DB, RAG fallback, `usage_events`, `audit_events`, tenant config | G04, G05, G07, G18, tenancy |
| **Qdrant** | RAG vectors (dense+sparse), Mem0 memories, agent skills | G07, G10 |
| **LLMLingua-2 sidecar** | Runtime prompt compression | G01 |
| **Tika sidecar** | Document text extraction | G03 |
| **RouteLLM sidecar** | Model routing classifier | G06 |
| **headroom (optional)** | L3 semantic cache, structured pruning, image/CCR compression | G05, G19, G27, G28 |
| **LiteLLM** | LLM provider abstraction | `main.py` |
| **Langfuse / Prometheus / OTLP** | Observability & tracing | G18, `langfuse_tracing.py`, `tracing/otel.py` |
| **Kafka / Temporal (optional)** | Enterprise batch queue / durable workflows | G13, G16, G05 |
| **Mem0 / Zep / Instructor (optional)** | Long-term memory / typed output | G10, G09 |

## Configuration Hot-Reload

Every 60 seconds, a daemon thread in `config_loader.py`:
1. Fetches `config.yaml` (GCS on GCP, local file in dev) + merges `config/params/*.yaml`
2. Updates in-memory config under a thread lock
3. On fetch failure, continues with the last known good config

## Background Processes

- **Config Hot-Reload** — every 60s
- **G03 Doc Pipeline** — GCS pub/sub → `POST /ingest-doc` (Tika extraction, RAG fallback, optional fine-tune)
- **G05 Auto-TTL** — `AutoTTLManager` adjusts TTLs from hit-rate stats
- **G08 Scheduled Pruning** — removes MCP tools inactive for 30 days
- **G02 Template Deprecation** — scheduled stale-template checks
- **G13 Batch Consumer** — Redis Streams / Kafka background consumers (TOON legend amortisation)
- **G20 Prompt Optimisation** — offline optimiser feeding G2/G20 learned templates
- **G24 Rule Generation** — offline savings-pattern analysis feeds adaptive-bypass rules
- **Langfuse / OTLP ingestion + Prometheus `/metrics`** — observability export
- **Grafana dashboards** — per-call → quarterly views over the open metrics/`usage_events` schema

## Savings Calculation

```
baseline_tokens      = token count of original request (before any optimisation)
final_tokens_sent    = token count after the G0–G21 request pipeline
total_absolute_saving = baseline_tokens - final_tokens_sent
total_pct_saving      = (total_absolute_saving / baseline_tokens) * 100
cost_baseline_usd     = (baseline_tokens/1000)*input_price + (response_tokens/1000)*output_price
cost_actual_usd       = (final_tokens_sent/1000)*input_price + (response_tokens/1000)*output_price
cost_saving_usd       = cost_baseline_usd - cost_actual_usd
```

> **Fair disclosure.** Token counts are exact. **Cost figures are config-priced estimates** — they
> use the static `pricing:` table in `config.yaml` and do **not** model provider discounts,
> prompt-cache/batch credits, or reasoning surcharges; `baseline_tokens` is a counterfactual.
> Treat `cost_*_usd` as directional, **not invoice-grade**.

### Two-track model (savings vs billing)

The savings numbers above are a **confidence/value layer**, kept strictly separate from billing:

- **Track 2 — confidence (this section).** `x` = `baseline_tokens`, `y` = `proxy_optimised_tokens` (post-optimisation estimate), `z` = `provider_prompt_tokens` (provider-reported, when available). Persisted on `usage_events`, surfaced in `_token_opt` / portal / dashboards. **Disclosed estimates — never billed.**
- **Track 1 — billing.** One served HTTP-2xx request (including cache hits and bypasses) = one billable unit. Invoice = `flat + max(0, requests − included) × overage`. **Tokens are never billed** (the `chars/4`-style estimate over an already-optimised prompt isn't provider-verifiable); the customer independently reproduces the bill by counting their own requests. Provider-token *reconciliation* is intentionally **not** implemented — there is nothing to reconcile when tokens aren't billed.

Each G-group records its own `StepSaving`:
```python
StepSaving(group="G01", description="LLMLingua-2 prompt compression",
           tokens_before=1000, tokens_after=600)
```

## Middleware File Inventory

| Group | File(s) | Core Purpose |
|-------|---------|--------------|
| **G00** | `g00_rate_limit.py` | Token-bucket rate limiting |
| **G01** | `g01_compression.py` | Prompt compression, layered composition |
| **G02** | `g02_template_registry.py` | Template management, deprecation, budget |
| **G03** | `g03_doc_pipeline.py` (+ `src/doc-pipeline/`, `src/finetune-pipeline/`, `src/tika-sidecar/`) | Document ingestion, RAG fallback, fine-tuning |
| **G04** | `g04_bypass.py`, `g04_db_resolution.py` | Rules-based bypass with DB-first resolution |
| **G05** | `g05_cache.py`, `g05_cache_gptcache.py`, `g05_temporal_activity.py` | L1/L2/L3 caching, Auto-TTL, activity replay |
| **G06** | `g06_routing.py` | Model routing/cascade with confidence scoring |
| **G07** | `g07_retrieval.py`, `g07_pgvector_fallback.py` | Hybrid RAG retrieval, pgvector fallback |
| **G08** | `g08_tool_loading.py`, `g08_mcp_loader.py` | Intent-based tool loading, MCP lazy manifest |
| **G09** | `g09_context_schema.py` | Prose detection, Instructor schema enforcement |
| **G10** | `g10_memory.py`, `g10_mem0_adapter.py` | Conversation memory, Mem0, Zep, skills |
| **G11** | `g11_output_format.py` | max_tokens enforcement, p95 feedback loop |
| **G12** | `g12_reasoning_budget.py` | Provider-specific reasoning budget, effort levels |
| **G13** | `g13_batch.py`, `g13_kafka.py`, `g13_toon.py` | Batch processing, TOON notation, Kafka |
| **G14** | `g14_tool_output.py`, `g14_tool_combining.py` | Tool output projection, parallel combining |
| **G15** | `g15_server_compute.py`, `g15_mcp_dispatch.py` | Server-side hooks, MCP handler dispatch |
| **G16** | `g16_agent_arch.py`, `g16_langgraph_runtime.py`, `g16_temporal_runtime.py` | Agent advisories, LangGraph, Temporal |
| **G17** | `g17_loop_control.py` | Loop control, InterAgentState, budget propagation |
| **G18** | `g18_observability.py`, `langfuse_tracing.py` | Prometheus metrics, Langfuse tracing, usage records |
| **G19** | `g19_headroom.py` | Structured (AST-aware) pruning — request + response |
| **G20** | `g20_prompt_optimizer.py` | Inline prompt optimisation (Opik/DSPy-fed) |
| **G21** | `g21_cache_alignment.py` | Provider prefix-cache alignment (final pre-send) |
| **G22** | `g22_deduplication.py` | Semantic deduplication of near-duplicate turns |
| **G23** | `g23_streaming_compression.py` | Streaming output compression (response path) |
| **G24** | `g24_adaptive_bypass.py` | Adaptive bypass — populates `ctx.skip_groups` (runs first) |
| **G25** | `g25_adaptive_reasoning.py` | Adaptive reasoning-mode selection |
| **G26** | *(reserved — not implemented)* | — |
| **G27** | `g27_multimodal_optimizer.py` | Multimodal image optimisation |
| **G28** | `g28_ccr.py` | Context Compression & Reuse — request + response |
| — | `pipeline.py` | Orchestrates the G0–G28 request/response pipeline |
| — | `__init__.py` | RequestContext dataclass definition |

## Implementation Status: 27/28 slots implemented (G26 reserved)

All 28 slots are accounted for; **G26 is intentionally reserved**, leaving **27 groups fully
operational**. Optional integrations (headroom, Mem0, Zep, Kafka, Temporal, Instructor) degrade
gracefully when their packages or backing services are absent.

---

*Pipeline source of truth: `src/proxy/middleware/pipeline.py` — `OptimisationPipeline`*
