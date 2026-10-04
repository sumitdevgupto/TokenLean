# GCP Deployment Guide

Complete guide for deploying the TokenLean — Token Optimisation Framework on Google Cloud Platform (GCP).

---

## Overview

The GCP deployment uses Terraform to provision managed services, Cloud Build for Docker image builds, and Cloud Run for serverless container execution. All optimisation groups (G0–G28, G27 reserved — 27 implemented) are supported with zero cost when paused.

| Component | GCP Service | Purpose |
|-----------|-------------|---------|
| **Proxy** | Cloud Run | Main LiteLLM proxy + G0–G28 middleware |
| **G1 Compression** | Cloud Run (llmlingua-svc) | LLMLingua-2 sidecar (G01 uses it only with `LLMLINGUA_ON_GCP=true`) |
| **G3 Doc Pipeline** | Cloud Run (tika-svc) | Apache Tika document extraction for the doc-pipeline job, for files Unstructured cannot read |
| **G4 Bypass** | Cloud SQL (PostgreSQL + pgvector) | Rules-based bypass cache |
| **G5 Cache** | Memorystore Redis | L1 exact-match + L2 semantic cache |
| **G6 Routing** | Cloud Run (routellm-svc) | RouteLLM model cascade |
| **G7 Retrieval** | Cloud Run (token-opt-qdrant) | Qdrant vector search |
| **G10 Memory** | Redis + Qdrant | Session summaries + agent skills |
| **G18 Observability** | Cloud Run (langfuse-svc, grafana-svc) | Tracing + dashboards |
| **Config** | GCS Bucket | Hot-reloaded config.yaml |
| **Secrets** | Secret Manager | LLM keys, DB passwords |

---

## Prerequisites

> **Deploy host: Linux, WSL Ubuntu, macOS, or GCP Cloud Shell — NOT Windows Git Bash / cmd.**
> The deploy runs Terraform `local-exec` schema migrations written in bash that connect to
> Cloud SQL via the Cloud SQL Auth Proxy + `psql`. On native Windows, Terraform launches
> `local-exec` through `cmd.exe` and `psql` is typically absent — the migrations fail.
> On Windows, use WSL Ubuntu (your checkout is visible at `/mnt/<drive>/...`).
>
> This is **enforced in-script**: every WSL-only GCP script (deploy, start/stop, teardown,
> pre-deploy-check, the in-VPC migration/key-sync jobs, secrets backup/restore) aborts with
> exit 2 if launched from Git Bash (`MINGW*/MSYS*/CYGWIN*`). Dual-mode scripts that also run
> against the local Docker stack (e.g. `create-operator.sh`, `key-security-harness.sh`) guard
> **only their `--gcp` path**, so local use from any shell still works. Read-only status checks
> (`check-gcp-status.sh`, `post-deploy-check.sh`) and env sourcing (CRLF-tolerant) are shell-
> agnostic. Env files edited on Windows are auto-stripped of CRLF at `source` time so a
> Windows-saved `.env.gcp` never corrupts gcloud args.

1. **gcloud CLI** installed and authenticated — including **Application Default Credentials**
   (`gcloud auth application-default login`; the Cloud SQL Auth Proxy used by the schema
   migrations authenticates with ADC, not your gcloud login)
2. **Docker** installed and running
3. **Terraform** >= 1.8 installed
4. **cloud-sql-proxy** (Cloud SQL Auth Proxy v2) — used by the Terraform schema migrations
5. **psql** (PostgreSQL client) — runs the migration SQL
6. **Python 3** with PyYAML: `pip install pyyaml`
7. **redis-cli** (optional, for backup/restore)

**One-shot setup (recommended):** `scripts/gcp/prepare-gcp-deploy-host.sh` installs items
1–5 if missing (idempotent), drives the interactive gcloud logins, verifies Docker + config
files, runs the pre-deploy check, and prints an explicit ✅ ALL OK / ❌ NOT READY verdict:

```bash
bash scripts/gcp/prepare-gcp-deploy-host.sh              # install + login + verify
bash scripts/gcp/prepare-gcp-deploy-host.sh --no-install # verify only
```

### GCP Project Setup

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### Required IAM Roles

Your account needs: `roles/editor`, `roles/cloudsql.admin`, `roles/redis.admin`, `roles/storage.admin`, `roles/secretmanager.admin`, `roles/run.admin`, `roles/cloudbuild.builds.builder`, `roles/serviceusage.serviceUsageAdmin`, `roles/artifactregistry.admin`

---

## Environment Setup

```bash
# 1. Copy the GCP environment template
cp .env.gcp.template .env.gcp

# 2. Edit with your project details
# .env.gcp
GCP_PROJECT_ID=your-gcp-project-id
GCP_REGION=asia-south1
```

All scripts automatically source `.env.gcp` if present, falling back to `.env`.

---

## Quick Deploy

```bash
# Full deploy (first time) — ~15-20 minutes
./scripts/gcp/gcp-deploy.sh --project YOUR_PROJECT_ID --region asia-south1

# Re-deploy after code changes — ~5 minutes
./scripts/gcp/gcp-deploy.sh --skip-infra --project YOUR_PROJECT_ID
```

## Step-by-Step Deployment

### Step 1: Validate Environment

```bash
./scripts/gcp/pre-deploy-check.sh --project YOUR_PROJECT_ID --region asia-south1
```

### Step 2: Configure Templates

```bash
cp infra/terraform.tfvars.template infra/terraform.tfvars
cp config/keys.yaml.template config/keys.yaml
# Edit both files with your real values
```

### Step 3: Deploy

```bash
source .env.gcp
./scripts/gcp/gcp-deploy.sh
```

### Step 4: Verify Health

```bash
./scripts/gcp/post-deploy-check.sh
```

### Step 5: Issue Proxy API Keys

```bash
./scripts/issue-key.sh issue --user developer@example.com
```

### Optional: Prompt quality eval

To run the Promptfoo quality eval against the deployed proxy (pass a **GCP-issued** key — local keys won't authenticate):
```bash
PROXY_URL=$(gcloud run services describe token-proxy \
  --region=asia-south1 --project=<GCP_PROJECT_ID> --format='value(status.url)')
export PROXY_API_KEY=tok-…           # from scripts/issue-key.sh
PROXY_URL="$PROXY_URL" bash ci/promptfoo-eval.sh
```
See **Build-Time Quality Gates & Optional Evals** in [DEPLOYMENT.md](../DEPLOYMENT.md) for the full reference (prerequisites, `--promptfoo` deploy flag, key model).

### Step 6: Redis requires its password and speaks only TLS

Redis holds every tenant's cached prompts and answers, sessions and counters, and the network
rules let any private address reach it. By default Redis requires a password and serves only
TLS (`REDIS_AUTH_ENFORCE` and `REDIS_TLS` in `.env.gcp`, both `true` unless set to `false`):

- Terraform keeps the password in the `redis-auth` secret, and every deploy mounts it into
  token-proxy and the fine-tune job as `REDIS_PASSWORD` (never inside `REDIS_URL`, a plain env
  var). They log in as the `default` user.
- The clients get a `rediss://` URL (Terraform output `redis_url`) and, as `REDIS_CA_CERT`, the
  CA that signs Redis's certificate (secret `redis-ca`), and trust that CA only. Memorystore
  uses its own CA, on port 6378. For the VM backend (`redis_backend = docker`) Terraform makes
  a CA and a certificate; the VM reads the certificate's key, like the password, with its own
  service account at boot, so neither sits in the instance metadata.
- The VM applies both settings only at boot. When it runs anything else than the deploy
  configures (the first deploy with them, a switch, a renewed certificate), the deploy
  restarts it and waits until it does, before updating the clients. A restart empties Redis:
  the cache, sessions and rate-limit counters start over.
- On Memorystore, switching TLS recreates the instance (it starts empty, and Redis is down
  until the deploy has updated the clients); switching AUTH on makes the running services lose
  Redis until then. Run such a deploy with `--skip-build` to keep the gap short.

### Service accounts

The proxy and its jobs run as `token-opt-proxy-sa`, which reads only the secrets it uses
(`least_privilege_secret_iam`, on by default; `false` restores its project-wide secret and
storage access). Every other service runs as an account of its own holding only what that
service uses, so a compromised dashboard or third-party image cannot use the proxy's provider
keys, KMS key or database access: `token-opt-qdrant-sa`, `token-opt-prometheus-sa`,
`token-opt-alertmanager-sa`, `token-opt-grafana-sa` and `token-opt-langfuse-sa`, all created by
Terraform, and the LLMLingua and Tika sidecars' accounts, which hold no roles.

A `--skip-infra` deploy reads the Grafana and Langfuse accounts from the Terraform state, so on
a project deployed before they existed, run one deploy without it first. If you point a bucket
of your own at `/ingest-doc`, grant `token-opt-proxy-sa` `roles/storage.objectViewer` on it.

---

## Lifecycle Management

### Pause (Minimum Cost)

```bash
./scripts/gcp/stop-gcp.sh --project YOUR_PROJECT_ID
```

**What stops billing:**
- Memorystore Redis: deleted (data backed up to GCS)
- Cloud SQL: stopped (~$2/month storage only)
- Cloud Run: scales to zero automatically (no cost when idle), except Qdrant (below)

**What persists:**
- GCS bucket with config and backups
- Cloud SQL storage (data intact)
- Qdrant's documents. Its one instance keeps running, so it never scales to zero (billed at
  Cloud Run's idle rate), and its collections live in that instance's own storage. Every
  ingest also writes the changed collection's snapshot to the `<project>-qdrant-snapshots`
  bucket, and a new Qdrant revision or a Cloud Run restart restores the newest snapshot of each
  collection before it serves. Only an ingest still running at that moment has to run again.
  The demo `rag_docs` collection is not snapshotted: the deploy and `start-gcp.sh` seed it
  again when it is empty. An erase deletes a tenant's snapshots with its collections.

### Resume

```bash
# Start Cloud SQL
./scripts/gcp/start-gcp.sh --project YOUR_PROJECT_ID

# Recreate Redis (takes ~10 min)
cd infra && terraform apply

# Verify
./scripts/gcp/post-deploy-check.sh
```

### Complete Teardown

```bash
./scripts/gcp/teardown-gcp.sh --project YOUR_PROJECT_ID
```

**Keeps:** GCS bucket, Artifact Registry images, Secret Manager secrets.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `No GCP project set` | Run `gcloud config set project YOUR_PROJECT_ID` or use `--project` flag |
| `terraform.tfvars not found` | Copy from template: `cp infra/terraform.tfvars.template infra/terraform.tfvars` |
| `keys.yaml not found` | Copy from template: `cp config/keys.yaml.template config/keys.yaml` and fill in real keys |
| Redis backup fails | Install redis-cli: `brew install redis` (macOS) or `apt-get install redis-tools` (Linux) |
| Cloud Run service not found | Check with `gcloud run services list --region=asia-south1` |
| Requests hang ~120s then **504** (pipeline stalls) | Almost always a **Redis dead-connection hang**: Cloud Run Direct VPC egress silently drops idle TCP connections, so a stale pooled connection blocks until the kernel gives up (~240s). The proxy's Redis pool ships with `socket_timeout`/`health_check_interval` set (`cache/redis_pool.py`), which bounds this — tune via `REDIS_SOCKET_TIMEOUT` / `REDIS_HEALTH_CHECK_INTERVAL`. Look for `STAGE GXX SLOW: <ms>` in the proxy logs to confirm the stalling stage. |
| `couldn't connect to huggingface.co ... couldn't find them in the cached files` on GCP | The embedding model was baked **revision-pinned** but its `refs/main` was missing, so an offline/egress-restricted runtime cache-misses. Fixed in `src/proxy/Dockerfile` (writes `refs/main` after the pinned bake) — **rebuild the proxy image** if you see this after a Dockerfile bump. |
| G01 compression or G06 RouteLLM calls fail with **403** on GCP | `llmlingua-svc`, `routellm-svc` and `tika-svc` require Cloud Run IAM: the caller needs `run.invoker` (the proxy SA holds it project-wide) and must send an identity token, which the proxy does for `https://*.run.app` URLs when it runs on GCP. `scripts/gcp/post-deploy-check.sh` fails if any of them answers an anonymous call. |
| G07 retrieval slow / Qdrant unreachable on GCP | If Qdrant is `ingress=internal`, Cloud Run→Cloud Run calls to its `run.app` URL are rejected unless the caller uses `--vpc-egress=all-traffic`. Either set the proxy to full VPC egress (needs Cloud NAT for LLM egress) or run Qdrant `ingress=all` + IAM (`run.invoker`) so the proxy's identity token authenticates. |
| Qdrant calls time out (`ConnectTimeout`, empty error strings) despite the service being reachable via curl | **qdrant-client dials port 6333 by default even for `https://` URLs without an explicit port** — Cloud Run serves 443 only, so every request hits a closed port. The shared `ml_models.qdrant_client_kwargs()` pins `port=443` for portless https URLs; if you construct a Qdrant client directly, pass `port=443` (or put the port in the URL). |
| `G05 L2 pgvector error: type "vector" does not exist` | The pgvector extension is required for the G05 L2 semantic cache **even when Qdrant serves G07**. Re-run the schema migrations (`scripts/gcp/run-migrations-job.sh` on the private-IP path) — `pgvector.sql` now applies unconditionally. |

---

## Cost Reference

| Resource | Running Cost | Paused Cost |
|----------|-------------|-------------|
| Cloud Run (idle) | $0 | $0 |
| Qdrant (one always-on Cloud Run instance) | Cloud Run idle rate | Cloud Run idle rate |
| Cloud SQL | ~$15-50/mo | ~$2/mo |
| Memorystore Redis | ~$15-30/mo | $0 (deleted) |
| GCS Storage | ~$0.02/GB/mo | ~$0.02/GB/mo |
| Secret Manager | ~$0.06/secret/mo | ~$0.06/secret/mo |

*Actual costs vary by region and usage. Use `stop-gcp.sh` to minimize spend during development.*
