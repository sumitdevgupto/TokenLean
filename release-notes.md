# Release Notes — TokenLean

Newest date first. All changes that shipped on the same day are grouped under **one**
`## YYYY-MM-DD` header. Enterprise-only items are labelled **[Enterprise]** and link to
<https://tokenlean.cbeyond.cloud/>.

<!--
Format (newest date at the TOP; ONE date header per day):

## YYYY-MM-DD
### <one-line summary> — <Type>
<what changed and why — keep to ~5-7 lines where possible; don't force-fit a genuinely
large change. For Enterprise items, state it explicitly and link the URL below.>
- **OSS:** <what ships in every tier>            (omit both bullets for a pure bug fix)
- **[Enterprise]:** <the managed depth> — <https://tokenlean.cbeyond.cloud/>

Type = Bug fix | Enhancement (OSS) | Enhancement (OSS + Enterprise) | Enhancement [Enterprise].
Add a new `###` item under today's date header; only start a new `## YYYY-MM-DD` when the
date changes.
-->

## 2026-07-20

### Semantic cache could serve an answer produced under a different system prompt — Bug fix
The G05 L2 semantic cache embeds **user turns only** — deliberately, because a long system prompt would dominate and truncate the embedding window and collapse distinct questions onto one point. The side effect was that the cache key was blind to the system prompt: the same user question asked under a **restrictive** system prompt could be served an answer cached under a **laxer** one, silently bypassing the scope, persona, or output-format constraint that prompt encodes. Caught by the ablation harness (DS8), where the baseline correctly declined off-topic questions while the cached arm answered them at 0.95–0.96 similarity without calling the model. Isolation was never broken across tenants (`tenant_id` has always been in the L2 filter) — this bit a single tenant running several personas, apps, or agents through one key. Fixed by folding a **system-prompt fingerprint into the cache scope** rather than into the embedding, so the vector still keys on query intent with no truncation regression: set `groups.G5_cache.cache_scope` to `tenant+system` (or `tenant+model+system`), globally or per tenant. The default `tenant` scope is unchanged and keys stay byte-identical, so upgrading invalidates nothing. Marketing: *"Cache scoping can now include the system prompt, so an assistant with a restricted scope is never served an answer generated under a different one."*

### Per-tenant free trials — days and requests, whichever first — Enhancement (OSS + Enterprise)
Enterprise prospects can now be put on a real production free trial limited by **N days AND M served requests, whichever is hit first** (either dimension optional). The counting basis is exactly the billable unit — every served 2xx, cache hits and bypasses included — so trial usage previews precisely what a paid invoice would count, and trial-period requests are flagged and **excluded from invoices** ($0 for a trial-only period). On expiry the proxy returns a clean **HTTP 402 `trial_expired`** (not billed, doesn't consume allowance) until an operator converts or extends. Operators start / set / extend / convert / cancel a trial per tenant from the admin console at runtime (no redeploy; effective within ~60 s), with an audited operator actor and a fleet view of trials that are active / expiring / awaiting action. Tenants see their own trial burn-down and 80/90% banners in the portal, and both thresholds plus expiry emit optional `trial.threshold` / `trial.expired` webhooks. Enforcement is OSS-core and default-off, so the self-host tier and the reproducible savings baseline are byte-identical. Marketing: *"Give prospects a real production trial — a configurable number of days or requests on the full optimisation pipeline, with automatic 80/90% warnings, webhook alerts, a clean cut-over to paid, and trial traffic never billed."*
- **OSS:** the free-trial gate (G00 `_check_trial` + `trial.threshold`/`trial.expired` event types), the `usage_events.trial` billing exclusion, and the tenant-facing `/portal/trial` status all ship in every tier (default-off).
- **[Enterprise]:** the admin-console trial lifecycle (start/extend/convert/cancel + audit), the fleet trials tile + per-tenant badge, the portal trial card/banner, and webhook delivery — <https://tokenlean.cbeyond.cloud/>

### Webhook/G06 code-review fixes — SSRF, notification misattribution, cross-tenant routing state — Bug fix
A multi-angle code review of the OmniRoute enterprise-roadmap work (response headers, G31 PII pass, per-model lockout, outbound webhooks, G06 routing strategies — shipped 2026-07-19) surfaced ten confirmed defects, all fixed: (1) **SSRF** — the webhook `_validate_url` only checked the `https://` scheme, so a tenant could register an endpoint pointed at the cloud metadata service or an internal address, and the `/test` button served as an on-demand probe. Now reuses the existing `intent_orchestration.validate_outbound_url` host check at both registration and delivery time. (2) The outbound `guardrail.block`/`pii.detected` webhook payloads picked their `categories`/`action` fields by "whichever is non-empty" rather than by which guardrail actually fired — a non-blocking G30 flag could mask a real G31 block and misreport severity to a SIEM. Now attributed to whichever guardrail(s) actually triggered, with block > mask > flag severity precedence. (3) G06's `least_latency` strategy fed its EWMA from every LLM call outcome including failures — a model failing fast looked "fast" and got preferentially routed to, undermining the sibling per-model-lockout feature. Now gated on a genuine success. (4) G06's round-robin state was a single process-global counter keyed only by tier name — two tenants configured differently for the same tier perturbed each other's rotation. Now tenant-scoped. (5) A G29/G31 PII **block** was mislabeled `redaction.applied` in the audit trail and SOC2 attestation, implying content was masked and served when the request was actually refused — added a distinct `redaction.blocked` action, threaded through the portal security-events taxonomy and the attestation evidence pack's new `pii_blocks` counter. Plus five lower-severity fixes: no new HTTP client per webhook delivery retry; a Redis outage no longer silently disables the webhook dead-letter queue (now logged); the duplicated hash-bucket formula (G06 canary/weighted vs. G11's A3 holdout) is now one shared `stable_bucket` helper; and the `least_latency` EWMA smoothing factor is a hot-reloadable config knob instead of a hardcoded constant. 90+ new/updated unit tests.

### F1/F2/F3 code-review fixes — SSRF, savings misattribution, cache/routing correctness — Bug fix
A multi-angle code review of the F1 learning loop / F2 intent orchestration / F3 agent registry console (shipped 2026-07-19) plus the savings-header fix surfaced ten confirmed defects, all fixed: (1) **SSRF** — a registered agent `url` reaching the cloud metadata service or an internal address is now rejected (literal-IP check, both at registration and dispatch); (2) an F2-dispatched request with no `usage` block in the agent's response could misreport ~100% savings — `final_tokens_sent`/`proxy_optimised_tokens` are now set on dispatch, mirroring the pipeline's own accounting step; (3) `routed_model` now reflects the agent that actually served the request, not G06's now-skipped pick, fixing both billing pricing and the `x-tokenlean-routed-model` header; (4) G05 no longer caches an agent-dispatched answer (it was being replayed on later matching prompts, bypassing intent classification entirely); (5) a learned F1 rule scoped to the post-routing model could never match under G06 tiered/cascade routing — G24 now runs a second, narrower pass right after G06; (6) an agent's `timeout_seconds` is now capped (300s) so a hanging agent can't tie up a request indefinitely; (7) the batch-results poller (`GET /v1/batch/results`) now attaches best-effort `x-tokenlean-*` headers too, closing the last gap the header fix didn't cover; (8) the Agents-tab save now writes via an atomic single-key `jsonb_set` instead of a read-modify-write, closing a lost-update race with the Groups tab; (9) the portal now detects and surfaces a static config.yaml tenant override that would silently make an Agents-tab Save have no effect on live routing; (10) the F1 miner's GCS mirror now uploads to the configured `rules_file` path instead of a hardcoded literal, plus an atomic (write-then-rename) local write. 60+ new/updated unit tests.

### Tune every optimisation from the portal — full toggle + savings-vs-quality knob coverage — Enhancement [Enterprise]
The portal **Optimisation Settings** tab now exposes an enable toggle and the key savings-vs-quality dials for **every** implemented step, closing two gaps: **G00 Rate Limiting** (enable + requests/minute, requests/hour, monthly-quota) and **G31 Context Trust** (RAG/indirect-injection + retrieved-PII policy) were previously invisible in the UI. Existing groups gained their primary missing dials — G01 user/system-prompt compression toggles, G05 semantic-cache TTL + scope, G06 routing strategy, G07 retrieved-context budget, G11 output validation, G13 TOON + native-batch, G16 tool-selection, G29 PHI, G30 response scanning. Trust & safety groups (G29/G30/G31) are now **operator-safe**: tenants tune the policy mode/threshold but the hard on/off — including the specific `mode` values (`off`/`allow`) that are functionally equivalent to disabling the group — is operator-only, so security can't be silently switched off by any route, and a rejected field in the save response tells the caller exactly what didn't apply. A self-healing migration clears any legacy per-tenant override that had disabled a safety group before this lock existed, so no tenant is left stuck. Every knob stays whitelisted + clamped server-side and hot-reloads within ~60 s. Marketing: *"Tune every optimisation for savings vs. quality from one dashboard — with safety controls locked to your operators."*
- **[Enterprise]:** the portal catalog + operator-locked safety toggles (mode-value bypass closed) + legacy-override self-heal + readiness coverage probe — <https://tokenlean.cbeyond.cloud/>

### Portal toggle correctness — off-by-default groups, inert G27/G20 knobs, dead G20 key — Bug fix
Three portal fixes surfaced during the coverage audit: (1) the settings UI showed a group's toggle as **ON** whenever the base config omitted `enabled`, even though most stages default OFF — the toggle now mirrors each stage's real default (ON only for G24/G29/G30/G31). (2) G27's image `quality`/`min_bytes` knobs were displayed but never passed to the compressor — they are now forwarded when the installed optimiser accepts them (signature-checked, so legacy builds are unaffected). (3) G20's catalog key was `G20_prompt_optimization`, but the middleware reads `g20_prompt_optimizer` — so the G20 toggle never took effect; the key is corrected and G20's offline-only knobs (which never applied per-request) were removed from the tenant UI.

### Deterministic prose compression + terse-output steering — three new savings levers — Enhancement (OSS)
Three opt-in, default-off savings features built on a new zero-LLM, zero-latency prose compressor (`prose_compress.py`) that strips filler/hedging/pleasantries while preserving code, URLs, paths, identifiers and version numbers **byte-for-byte** (regex engine ported from caveman-shrink, MIT — attribution in `docs/oss-licenses.md`). (1) **G08 tool-description compression** trims the prose in tool/function `description`s, which ride *every* agentic request and were previously passed verbatim (`G8_tools.compress_descriptions`). (2) **G01 deterministic fallback** engages only when the LLMLingua sidecar (and Kompress) reduced nothing — so a compression outage degrades to *some* savings instead of pass-through (`G1_compression.deterministic_fallback`). (3) **G11 terse-output steering** ships bundled `lite`/`full`/`ultra` presets that steer the model toward shorter answers — the biggest uncovered savings axis, since the 54.1% headline is input-only and output tokens cost far more per token — with safety carve-outs keeping security/destructive-action text in normal prose, and the active level folded into the G05 cache key so terse and verbose answers never mix (`G11_output.verbosity_steering.level`). Plus an offline `scripts/compress_prompts.py` to shrink prompt/memory files at rest. All default-off → the reproducible savings baseline stays byte-identical; each is a SAVINGS feature to be proven with a pitch-test-plan quality-gate run before enabling by default. 40+ unit tests (protection invariants, resolver priority, cache-scope, deep-copy isolation, fallback gating).
- **OSS:** all three levers + the shared compressor + the offline script ship in every tier — one-word marketing: *"cut output tokens with a terseness dial, and shrink tool manifests for free."*

## 2026-07-19

### Savings headers now emitted on cache-hit / bypass responses — Bug fix
The per-call `x-tokenlean-*` attribution headers (and the `x-savings-usd` alias) were only attached on the full-LLM / cascade / agent responses — the cache-hit, bypass, and content-filter short-circuits returned a header-less response. That silently broke the advertised always-on FinOps attribution exactly where it matters most (`x-tokenlean-cache` was absent on the highest-volume cache-hit traffic) and failed the deployment-readiness header gate. The header builder is now shared and applied to every served 2xx path, so a cache hit returns `x-tokenlean-cache: hit:<level>`. Streamed responses remain a documented exception.

### Agent Registry Console — declare & govern your orchestration agents from the portal — Enhancement [Enterprise]
A portal **Agents** tab to manage intent-orchestration (F2) without editing config: declare downstream agents (id, OpenAI-compatible URL, intent keywords, optional model / key / output budget), toggle orchestration on/off, and set the match threshold — all self-serve, per-tenant, validated server-side, effective within ~60 s. Plus a **routing-decisions** view — which agent handled each request, joined to model, cost, and latency — for audit and change-control. Persisted in the existing per-tenant config store (no new table); routing decisions are backed by a new `agent_id` column on the usage ledger.
- **OSS:** `usage_events.agent_id` (observability — which agent served a request; never billed) ships in every tier.
- **[Enterprise]:** the portal registry console + routing-decisions audit view — <https://tokenlean.cbeyond.cloud/>

### Intent-based multi-agent orchestration — one endpoint, every agent — Enhancement (OSS + Enterprise)
Point one proxy endpoint at TokenLean and it routes each request to the right **downstream agent** by intent — "refund my invoice" → your billing agent, "the server is down" → your SRE agent — with no routing code in your app. An agent is any OpenAI-compatible chat endpoint you run; register it per tenant with intent keywords and TokenLean forwards matching requests there (its answer still runs response-side groups + billing), falling back to the normal LLM on no match. Opt-in / default-off (no agents registered → byte-identical path), per-tenant isolated (a tenant's agent list never leaks to another), with an optional per-agent output budget. First increment is single-agent routing; multi-intent fan-out follows.
- **OSS:** the orchestration engine — config-driven agent registry, heuristic intent classifier, dispatch + short-circuit — ships in every tier (`orchestration.*`).
- **[Enterprise]:** the managed registry console (declare/govern agents in the portal), routing-decision audit, and a managed ML intent classifier — <https://tokenlean.cbeyond.cloud/>

### Agentic learning loop — the proxy self-tunes per tenant — Enhancement [Enterprise]
A managed background job mines your own `usage_events` ledger and, for each `(tenant, routed_model)`, finds savings-optimisation groups that keep running but realise ≈no tokens — then writes **per-tenant** adaptive-bypass rules into the very artifact G24 already hot-reloads. Within one reload cycle (~60 s) the proxy stops paying for that group for that cohort, with zero engineer effort; bills keep falling as more rules accrue. Conservative by design: opt-in / default-off, a minimum-sample floor, a hard **never-skip denylist** (cache, routing, rate-limit, observability, trust & safety), and any operator-authored rules are always preserved.
- **OSS:** the G24 adaptive-bypass engine that consumes the rules ships in every tier.
- **[Enterprise]:** the managed miner that generates them per tenant, and the portal to review/override — <https://tokenlean.cbeyond.cloud/>

### G06 routing strategies — canary, weighted, round-robin, least-latency — Enhancement (OSS + Enterprise)
G06 gains a `strategy` layer that picks **which model of a chosen tier's list** to use (the complexity classifier still picks the tier; the strategy picks within it). Options: `priority` (**default — the tier's first model, byte-identical to today, so the 54.1%% savings baseline is unchanged**), `round_robin`, `weighted` (`strategy_weights`), `least_latency` (routes to the tier model with the lowest observed served-latency EWMA, fed from real calls), and `canary` (`canary_pct`% to the tier's second model — ramp a new model 5→50→100% and compare cost/quality via the `x-tokenlean-routed-model` header). All strategies are **deterministic** (request-id hash / per-worker counter / EWMA, never random) so the ablation stays reproducible. Per-tenant, opt-in, default off. 14 tests.

- **OSS:** the strategy engine + all five modes ship in every tier (`groups.G6_routing.strategy`).
- **[Enterprise]:** portal strategy config + canary A/B comparison dashboards — <https://tokenlean.cbeyond.cloud/>.

### Outbound event webhooks — push budget/security events to your Slack, PagerDuty, SIEM — Enhancement [Enterprise]
Tenants can register HTTPS endpoints (portal `/portal/webhooks`) to receive **PII-free** TokenLean events in real time: `spend_cap.reached`, `budget.threshold` (a one-shot warning when monthly spend first crosses a configurable `warn_pct` of the cap), `guardrail.block` (G30/G31 injection), and `pii.detected` (G29/G31). Each delivery is **HMAC-SHA256 signed** (`X-TokenLean-Signature`) with a per-endpoint secret shown once at registration and stored Fernet-encrypted; delivery uses bounded exponential-backoff retry with a Redis dead-letter on final failure. The emit seam is OSS core (`events.py`, a no-op without a dispatcher) so the barricade holds; the delivery product + portal CRUD are commercial. Payloads carry event metadata only (counts / entity types / categories) — never content. 24 tests (8 core seam + 6 spend-emit + 10 delivery/CRUD).

- **[Enterprise]:** endpoint registration, signed delivery, retry/dead-letter, and the portal Webhooks surface — <https://tokenlean.cbeyond.cloud/>.

### Per-model lockout — quarantine one degraded model without blacking out the provider — Enhancement (OSS + Enterprise)
The resilience layer gains a third, finer gate alongside the per-provider circuit breaker and per-tenant cooldown: a **per-(provider,model) lockout**. When a single model racks up `model_failure_threshold` model-scoped 5xx/timeout failures, it's skipped on subsequent requests for `model_lockout_seconds` (then one probe re-tests) — so a deprecated or degraded model (e.g. `gpt-4o` flaking while `gpt-4o-mini` is fine) is quarantined and failover routes around **just that model**, not the whole provider. The threshold is deliberately lower than the provider breaker's, so a fallback model's success resets the provider breaker and the provider stays live. Opt-in via `resilience.model_lockout` (default off → provider-breaker behaviour byte-identical); gauge `token_opt_model_lockout_state{provider,model}`. 8 unit + 1 integration test.

- **OSS:** the lockout primitive + config + metric ship in every tier.
- **[Enterprise]:** the SLA-dashboard model-lockout panel + managed alerting on quarantined models — <https://tokenlean.cbeyond.cloud/>.

### G31 now scans retrieved context for PII, not just injection — Enhancement (OSS + Enterprise)
G31 Context-Trust already re-scanned RAG/memory-injected `system`/`tool` context for indirect prompt-injection; it now optionally runs the **same G29 PII engine** over that assembled context too. This closes the gap where a poisoned or PII-laden retrieved document (an SSN in a support ticket, an email in a KB doc) reached the model or cache — G29 runs *before* retrieval, so it never saw it. Opt-in via `groups.G31_context_trust.pii_mode`: `off` (default) / `flag` / `mask` / `block`. Masking here is **irreversible** by design (`[EMAIL]`, no vault) — retrieved PII is never the caller's data to restore, and restoring it would let the model echo it back. Recorded on dedicated `context_trust_pii_*` fields + `token_opt_context_trust_events_total` (category `pii:<ENTITY>`) + a `source:"retrieved"` audit row, kept separate from G29's request-side redaction. DS20 gains a `ctxpii` block-proof; 8 tests.

- **OSS:** the retrieved-context PII pass + `flag`/`mask`/`block` modes ship in every tier.
- **[Enterprise]:** managed medical-NER / Presidio recognisers + the context-quality/trust-safety dashboards over retrieved-corpus PII — <https://tokenlean.cbeyond.cloud/>.

### Per-call savings exposed as `x-tokenlean-*` response headers — Enhancement (OSS + Enterprise)
Every served 2xx response now carries a machine-readable header family so a customer's FinOps/observability pipeline can attribute cost per request **without parsing the body**: `x-tokenlean-routed-model`, `x-tokenlean-cache` (`miss`/`hit`/`hit:<level>`), `x-tokenlean-tokens-saved`, `x-tokenlean-pct-saved`, `x-tokenlean-cost-saved-usd`, `x-tokenlean-latency-ms`, and `x-tokenlean-request-id`. Emitted on the normal and G06 cascade short-circuit paths alike, and carried through unchanged to Anthropic/Gemini clients by the protocol egress passthru. The existing `x-savings-usd` is retained as a back-compat alias of the cost header. Streamed responses are unaffected (documented limitation). Always-on, no config. 6 tests.

- **OSS:** the full `x-tokenlean-*` header suite ships in every tier.
- **[Enterprise]:** portal/dashboard drill-down and FinOps cost-attribution built on the same per-call fields — <https://tokenlean.cbeyond.cloud/>.

## 2026-07-18

### Grounding-coverage metric now emitted live (G07 → response path) — Enhancement (OSS + Enterprise)
The grounding-coverage heuristic shipped earlier today is now **wired to emit**. G07 stashes the injected chunk texts, and once the answer is produced the pipeline computes the fraction of answer sentences supported by the retrieved context and records `token_opt_grounding_coverage{tenant_id}`. No-op for non-RAG requests and tool-call answers; never breaks the response path. This lights up the last dark metric in the application-quality surface. 5 tests.

- **OSS:** the metric emits at `/metrics`.
- **[Enterprise]:** grounding-coverage trends + low-grounding anomaly alerting in the context-quality dashboards — <https://tokenlean.cbeyond.cloud/>.

### PII/PHI ingest masking now runs in the GCP doc-pipeline Job — Bug fix
The opt-in ingest masking shipped earlier today worked locally but **silently no-op'd in the GCP Cloud Run Job** — that container's build context copies only `pipeline.py`, so the `guardrails` engine wasn't importable and the defensive import fell through. The build now stages the 3 public `guardrails` files into the doc-pipeline image (never the commercial `ruleset_feed.py`), so `INGEST_PII_MODE=mask` actually masks before embedding in production. Verified with a local image build. Default off → no behaviour change unless enabled.

### Output JSON-schema validation (G11) — Enhancement (OSS + Enterprise)
When a request asks for **structured output** (OpenAI `response_format` `json_object`/`json_schema`, or a `json_schema` param), G11 now validates the answer is parseable JSON and schema-conformant — closing the malformed-JSON / missing-field gap on the response path. Opt-in via `groups.G11_output.validate_output`: `off` (default) / `flag` (record + annotate, non-mutating) / `repair` (one bounded re-ask — never loops; `repair_fallback: flag|block`) / `block` (withhold with a content-filter 200, not cached). Tool-call and multimodal answers are untouched. Emits `token_opt_output_schema_failures_total`; 11 tests.

- **OSS:** the JSON/schema validator + `flag`/`repair`/`block` modes ship in every tier.
- **[Enterprise]:** `output-reliability` dashboards + anomaly alerting over schema-failure rates — <https://tokenlean.cbeyond.cloud/>.

### Application-quality metrics surface — Enhancement (OSS + Enterprise)
A new metrics module (`middleware/quality_metrics.py`), kept **separate** from the operational/savings metrics (G18) so reasoning-quality is never confused with gateway health. PII-free (labels are `tenant_id` only): **Context Quality** — retrieval hit-rate, chunks-returned, context freshness, and a cheap grounding-coverage heuristic; **Output Reliability** — schema failures, tool-eligibility denials, inline-judge scores. This release wires the retrieval metrics live from G07 (hit or miss) and ships the grounding heuristic tested; the reliability counters are defined for later features to emit. 13 tests.

- **OSS:** the metric emission ships in every tier at `/metrics`.
- **[Enterprise]:** `context-quality` + `output-reliability` dashboards, trends, and anomaly alerting — <https://tokenlean.cbeyond.cloud/>.

### RAG context freshness (ingest timestamps + max-age filter) — Enhancement (OSS + Enterprise)
RAG chunks now carry freshness metadata: ingestion (G03) stamps `ingested_at` (and `source_date` when supplied via `SOURCE_DATE`), and retrieval (G07) can **soft-filter stale context** with `max_age_days`, dropping chunks older than the window before they reach the model. Fails safe: `max_age_days: null` (default) is off, and a chunk with no timestamp is never dropped, so existing corpora keep working. Chunk age is surfaced on the retrieval trace. Config: `groups.G7_retrieval.max_age_days`; 10 tests.

- **OSS:** the freshness stamp + max-age filter ship in every tier.
- **[Enterprise]:** freshness/staleness dashboards + alerting over the retrieval corpus — <https://tokenlean.cbeyond.cloud/>.

### PII/PHI redaction at RAG ingest (opt-in, G03) — Enhancement (OSS + Enterprise)
The ingestion pipeline (G03) can now **mask PII/PHI before a document is chunked, embedded, and stored** — so the vector store never holds raw personal data and G07 can't inject it into a prompt. Scanning the full text before chunking also stops a value split across a chunk boundary from evading the scan. Opt-in via `INGEST_PII_MODE=flag|mask` (default `off`) and `INGEST_PII_PHI=true`; it reuses the same precision-biased OSS `guardrails` engine as G29. An end-to-end test proves the stored chunk payload carries placeholders, not the original PII.

- **OSS:** the ingest-time masking ships in the engine.
- **[Enterprise]:** managed medical-NER recognisers + HIPAA/PCI attestation over ingested corpora — <https://tokenlean.cbeyond.cloud/>.

### PHI detection (opt-in) added to PII redaction (G29) — Enhancement (OSS + Enterprise)
G29 can now detect **health identifiers** as well as PII — US **DEA** and **NPI** numbers (checksum-validated) and, behind a required medical context cue, **MRN** and **ICD-10** codes. It is **opt-in** (`phi: true`) and precision-biased so it does not fire on look-alikes — a bare 10-digit number, an order id, or a version like "B20.1" stays clean. PHI flows through G29's existing `flag`/`mask`/`block` modes and PII-free metrics/audit. Default off. Config: `groups.G29_pii_redaction.phi`; shipped with a false-positive corpus and 20+ tests.

- **OSS:** the checksum/context-gated regex detectors ship in every tier.
- **[Enterprise]:** higher-recall medical NER (Presidio) + HIPAA/PCI policy mapping and attestation — <https://tokenlean.cbeyond.cloud/>.

### G30 response-side injection/moderation scan — Enhancement (OSS + Enterprise)
G30 gained an opt-in **response-side scan** (`scan_response`, default off) that applies the injection engine to the model's **output** — catching a model that echoes an attack payload or emits unsafe instructions a downstream agent might act on. Modes: `flag` (detect + record, non-mutating) or `block` (withhold with a content-filter 200; not cached). Non-streaming responses only; behaviour is unchanged until enabled. New verdict on the existing guardrail metric (`action=response_flag|response_block`). Config: `groups.G30_guardrails.scan_response` / `response_mode`.

- **OSS:** the output-scan engine + static ruleset ship in every tier.
- **[Enterprise]:** the managed moderation ruleset feed (`extra_rules`) raises recall on novel output-safety patterns — <https://tokenlean.cbeyond.cloud/>.

### Malformed OpenAI requests return a clean 400 — Bug fix
The `/v1/chat/completions` (OpenAI) route now validates the request envelope and returns a clean, OpenAI-shaped **400** for a malformed body — a non-JSON body, or `messages` that isn't a non-empty array of role-bearing objects. Previously such requests surfaced as a 500 (or 400'd at the provider); the Anthropic (`/v1/messages`) and Gemini routes already returned a proper 400, so this brings the OpenAI route to parity. The check is envelope-only — semantic validation still belongs to litellm/the provider. 8 tests.

### RAG retrieval fails closed (relevance floor hardening) — Bug fix
Two RAG relevance gaps in retrieval (G07) closed so low-relevance context can't slip into the prompt: (1) the cross-encoder **reranker now fails *closed*** — on error it re-applies the retrieval cosine floor to cosine-scored chunks (RRF-fused chunks keep their fusion ranking, where a cosine floor is meaningless) instead of returning the unfiltered set; (2) the **dense-only Qdrant paths now pass `score_threshold`**, matching the pgvector path, so weak matches are dropped at retrieval rather than relying on the reranker. No config change; strictly more conservative. 4 tests.

### GCP cost-inventory script + teardown status wiring + `--nuke` — Enhancement (OSS)
Operator tooling for cleanly exiting / auditing a GCP deployment:
- **New `scripts/gcp/gcp-running-inventory.sh`** — a read-only, project-wide sweep across all regions of every cost-bearing resource, grouped by cost behaviour (bills-continuously / scale-to-zero / storage) and ending in a two-tier **COST SUMMARY**; exits non-zero if anything bills continuously. Optional `--asset` adds a Cloud Asset Inventory dump.
- **`teardown-gcp.sh` consolidated status** — teardown now ends by running the status + inventory scripts for one post-teardown view (skip with `--no-status`).
- **`teardown-gcp.sh --nuke`** — "exit the project" mode: everything `--full` does **plus** deleting the tf-state and Cloud Build buckets, emptying the project to the GCP floor while keeping the project + KMS key ring (GCP forbids deleting rings, and keeping it lets `terraform apply` reattach on rebuild). Residual ≈ $0.06/mo; rebuildable (infra only — data is not restored); requires typing `nuke`.

### Test-harness doctrine, Security Suite & deploy-readiness gating — Enhancement (OSS + Enterprise)
Clarified and enforced the change-completion doctrine, and expanded deployment verification:
- **Harness routing by feature type.** `examples/benchmark` (and the internal pitch-test-plan) are now savings-validation only — a non-savings change no longer touches them, protecting the calibrated benchmark number and the reproducible savings headline. Non-savings validation (trust & safety, protocols, auth, billing, portal) lives in the deployment-readiness harness.
- **[Enterprise] Security Suite** — a standalone, non-destructive security posture check (auth/authz, endpoint-exposure, BYOK/402, trust-safety engine proof) that also runs as a gating section of the readiness harness — <https://tokenlean.cbeyond.cloud/>.
- **[Enterprise] Deployment-readiness tiers + gating** — `--quick` (cheap deploy gate) and `--full` (deep pre-release) tiers; every deploy auto-runs the quick gate and a NOT-READY verdict blocks it — <https://tokenlean.cbeyond.cloud/>.
- **Commit-time enforcement (OSS):** a change under `src/` must ship a `release-notes.md` entry and a matching `tests/` change, or the commit is blocked (override with `[skip-relnotes]` / `[skip-tests]` tokens). A guard test keeps trust-safety groups out of the savings registry.

## 2026-07-15

### G31 Context-Trust: indirect (RAG) prompt-injection defence — Enhancement (OSS + Enterprise)
New **G31** middleware closes the indirect prompt-injection gap. G30 scans the untrusted user prompt, but retrieval (G07) and memory (G10) append retrieved documents / stored memories into the prompt **after** G30 runs — so a poisoned document could previously reach the model un-inspected. G31 re-scans the *assembled* context (`system` / `tool` roles) with the `guardrails/injection.py` engine, runs non-bypassably right after the G07/G10/G22 stages, and supports `allow` / `flag` (default, non-mutating) / `block` (content-filter 200) / `strip` (drop only the poisoned content). New metric `token_opt_context_trust_events_total{category,action}`. Config: `groups.G31_context_trust`.

- **OSS:** the scanner engine + static default ruleset ship in every tier; default `flag` mode is non-mutating.
- **[Enterprise]:** the continuously-updated managed red-team ruleset feed (via `extra_rules`) and the Security dashboards/console — <https://tokenlean.cbeyond.cloud/>.
