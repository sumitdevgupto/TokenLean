# Release Notes — TokenLean

Newest entries first. Each entry is dated (`YYYY-MM-DD`). Bug fixes and feature
enhancements are logged here as they ship. Enterprise-only items are labelled
**[Enterprise]** and link to <https://tokenlean.cbeyond.cloud/>.

<!--
Entry format (add new entries directly BELOW this comment, newest at top):

## YYYY-MM-DD — <one-line summary>
**Type:** Bug fix | Enhancement (OSS) | Enhancement (OSS + Enterprise) | Enhancement [Enterprise]
<details of what changed and why. For Enterprise items, state it explicitly and link
https://tokenlean.cbeyond.cloud/ >
-->

## 2026-07-18 — PII/PHI redaction at RAG ingest (opt-in, G03)
**Type:** Enhancement (OSS + Enterprise)

The document-ingestion pipeline (G03) can now **mask PII/PHI before a document is chunked, embedded, and stored** — so the vector store never holds raw personal data and G07 retrieval can never inject it into a prompt. Scanning the full text before chunking also prevents a value being split across a chunk boundary and evading the scan. Opt-in via `INGEST_PII_MODE=flag|mask` (default `off`, ingestion unchanged) and `INGEST_PII_PHI=true` for the health-entity set; it reuses the same precision-biased OSS `guardrails` engine as G29. Covered by an end-to-end test proving the stored chunk payload carries placeholders, not the original PII.

- **OSS:** the ingest-time masking ships in the engine.
- **[Enterprise]:** managed medical-NER recognisers + compliance (HIPAA/PCI) attestation over ingested corpora — <https://tokenlean.cbeyond.cloud/>.

## 2026-07-18 — PHI detection (opt-in) added to PII redaction (G29)
**Type:** Enhancement (OSS + Enterprise)

G29 can now detect **health identifiers** in addition to PII — US **DEA** and **NPI** numbers (checksum-validated) and, behind a required medical context cue, **MRN** and **ICD-10** codes. It is **opt-in** (`phi: true` or listing the entities explicitly) and precision-biased so it does not fire on look-alikes — a bare 10-digit number, an order id, or a version string like "B20.1" stays clean. PHI flows through G29's existing `flag`/`mask`/`block` modes and PII-free metrics/audit. Default off → existing tenants are unchanged. Config: `groups.G29_pii_redaction.phi` (see `docs/config-reference.md`); shipped with a dedicated false-positive corpus and 20+ tests.

- **OSS:** the checksum/context-gated regex detectors ship in every tier.
- **[Enterprise]:** higher-recall medical NER (Presidio recognisers) + HIPAA/PCI policy mapping and attestation — <https://tokenlean.cbeyond.cloud/>.

## 2026-07-18 — Malformed OpenAI requests return a clean 400
**Type:** Bug fix

The `/v1/chat/completions` (OpenAI) route now validates the request envelope and returns a clean, OpenAI-shaped **400** for a malformed body — a non-JSON body, or `messages` that isn't a non-empty array of role-bearing objects. Previously such requests surfaced as a 500 (or were forwarded to the provider only to 400 there); the Anthropic (`/v1/messages`) and Gemini routes already returned a proper 400, so this brings the OpenAI route to parity. The check is deliberately light (envelope only) — semantic validation still belongs to litellm/the provider. Covered by 8 new unit tests.

## 2026-07-18 — RAG retrieval fails closed (relevance floor hardening)
**Type:** Bug fix

Two RAG relevance gaps in retrieval (G07) closed so low-relevance context can't slip into the prompt:

- **Reranker now fails *closed*.** Previously, if the cross-encoder reranker errored it returned the *unfiltered* candidate set — injecting chunks the reranker was meant to drop. It now re-applies the retrieval cosine floor to cosine-scored chunks on failure (RRF-fused chunks keep their fusion ranking, since a cosine floor is meaningless on reciprocal-rank scores), so a transient reranker hiccup no longer degrades relevance.
- **Dense Qdrant search now has a score floor.** The dense-only Qdrant paths now pass `score_threshold` (matching the pgvector path and the relaxed-fallback chain), so weakly-matching chunks are dropped at retrieval rather than relying solely on the reranker.

No config change; behaviour is strictly more conservative. Covered by 4 new unit tests.

## 2026-07-18 — G30 response-side injection/moderation scan
**Type:** Enhancement (OSS + Enterprise)

G30 gained an opt-in **response-side scan** (`scan_response`, default off) that applies the injection engine to the model's **output** — catching a model that echoes an attack payload or emits unsafe instructions a downstream agent might act on. Modes: `flag` (detect + record, non-mutating) or `block` (withhold the unsafe answer with a content-filter 200; not cached). Non-streaming responses only; shipped behaviour is unchanged until enabled. New response verdict on the existing guardrail metric (`action=response_flag|response_block`). Config: `groups.G30_guardrails.scan_response` / `response_mode` (see `docs/config-reference.md`).

- **OSS:** the output-scan engine + static ruleset ship in every tier.
- **[Enterprise]:** the managed moderation ruleset feed (`extra_rules`) raises recall on novel output-safety patterns — <https://tokenlean.cbeyond.cloud/>.

## 2026-07-18 — GCP cost-inventory script + teardown status wiring + `--nuke`
**Type:** Enhancement (OSS)

Operator tooling for cleanly exiting / auditing a GCP deployment:

- **New `scripts/gcp/gcp-running-inventory.sh`** — a read-only, project-wide sweep across **all regions/zones** of every cost-bearing resource (Cloud SQL, Compute VMs, Memorystore, Serverless VPC connectors, reserved external IPs, load balancers, Cloud NAT, Cloud Run services/jobs, disks, buckets, Artifact Registry, secrets, KMS), grouped by cost behaviour (bills-continuously / scale-to-zero / storage) and ending in a two-tier **COST SUMMARY**. Exits non-zero if anything bills continuously. Runs from any shell (forces `CLOUDSDK_CORE_DISABLE_PROMPTS` so a disabled-API prompt can't hang it). Optional `--asset` adds a full Cloud Asset Inventory dump.
- **`teardown-gcp.sh` consolidated status** — teardown now ends by running `check-gcp-status.sh` + `gcp-running-inventory.sh` for one consolidated post-teardown view; skip with `--no-status`.
- **`teardown-gcp.sh --nuke`** — "exit the project" mode: everything `--full` does **plus** deleting the tf-state and Cloud Build buckets, emptying the project to the GCP floor while keeping the **project** and the **KMS key ring** (GCP forbids deleting key rings; keeping it lets `terraform apply` reattach on rebuild). Residual cost ≈ $0.06/mo. Rebuildable via `run-gcp-commercial-lifecycle.sh` (infra only — data is not restored); requires typing `nuke` to confirm. The summary prints the `gcloud projects delete` command for the literal-$0 path.

## 2026-07-18 — Test-harness doctrine, Security Suite & deploy-readiness gating
**Type:** Enhancement (OSS + Enterprise)

Clarified and enforced the change-completion doctrine, and expanded deployment verification:

- **Harness routing by feature type.** `examples/benchmark` (and the internal pitch-test-plan) are now savings-validation only — a non-savings change no longer touches them, protecting the calibrated benchmark number and the reproducible savings headline. Non-savings validation (trust & safety, protocols, auth, billing, portal) lives in the deployment-readiness harness. The misplaced `--security-smoke` benchmark mode added earlier was removed; the benchmark is savings-only again.
- **[Enterprise] Security Suite** — a standalone, non-destructive security posture check (auth/authz, endpoint-exposure, BYOK/402, and a trust-safety engine proof) that also runs as a gating section of the deployment-readiness harness. Operator tooling — see <https://tokenlean.cbeyond.cloud/>.
- **[Enterprise] Deployment-readiness tiers + gating** — the readiness harness gained `--quick` (cheap deploy gate) and `--full` (deep pre-release) tiers; every deploy now auto-runs the quick gate and a NOT-READY verdict blocks the deploy, so a broken stack is never declared customer-ready. Commercial-portal checks gate on a detected commercial deploy. See <https://tokenlean.cbeyond.cloud/>.
- **Commit-time enforcement (OSS):** a change under `src/` must ship a `release-notes.md` entry and a matching `tests/` change, or the commit is blocked (override with `[skip-relnotes]` / `[skip-tests]` tokens for genuine no-logic commits). A guard test keeps trust-safety groups out of the savings registry.

## 2026-07-15 — G31 Context-Trust: indirect (RAG) prompt-injection defence
**Type:** Enhancement (OSS + Enterprise)

New **G31** middleware closes the indirect prompt-injection gap. G30 scans the untrusted user prompt, but retrieval (G07) and memory (G10) append retrieved documents / stored memories into the prompt **after** G30 runs — so a poisoned document in the vector store could previously reach the model un-inspected. G31 re-scans the *assembled* context (`system` / `tool` roles) with the existing `guardrails/injection.py` engine, runs non-bypassably right after the G07/G10/G22 stages, and supports `allow` / `flag` (default, non-mutating) / `block` (content-filter 200) / `strip` (drop only the poisoned injected content) modes. New metric `token_opt_context_trust_events_total{category,action}`. Config: `groups.G31_context_trust` (see `docs/config-reference.md`).

- **OSS:** the scanner engine + static default ruleset ship in every tier; default `flag` mode is non-mutating (savings/token accounting unchanged).
- **[Enterprise]:** the continuously-updated managed red-team ruleset feed (via `extra_rules`) and the Security dashboards/console — <https://tokenlean.cbeyond.cloud/>.
