# GCP Deployment Guide

Complete guide for deploying the TokenLean — Token Optimisation Framework on Google Cloud Platform (GCP).

---

## Overview

The GCP deployment uses Terraform to provision managed services, Cloud Build for Docker image builds, and Cloud Run for serverless container execution. All optimisation groups (G0–G28, G26 reserved — 27 implemented) are supported with zero cost when paused.

| Component | GCP Service | Purpose |
|-----------|-------------|---------|
| **Proxy** | Cloud Run | Main LiteLLM proxy + G0–G28 middleware |
| **G1 Compression** | Cloud Run (llmlingua-svc) | LLMLingua-2 sidecar |
| **G3 Doc Pipeline** | Cloud Run (tika-svc) | Apache Tika document extraction |
| **G4 Bypass** | Cloud SQL (PostgreSQL + pgvector) | Rules-based bypass cache |
| **G5 Cache** | Memorystore Redis | L1 exact-match + L2 semantic cache |
| **G6 Routing** | Cloud Run (routellm-svc) | RouteLLM model cascade |
| **G7 Retrieval** | Cloud Run (token-opt-qdrant) | Qdrant vector search |
| **G10 Memory** | Redis + Qdrant | Mem0 long-horizon memory |
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

---

## Lifecycle Management

### Pause (Zero Cost)

```bash
./scripts/gcp/stop-gcp.sh --project YOUR_PROJECT_ID
```

**What stops billing:**
- Memorystore Redis: deleted (data backed up to GCS)
- Cloud SQL: stopped (~$2/month storage only)
- Cloud Run: scales to zero automatically (no cost when idle)

**What persists:**
- GCS bucket with config and backups
- Cloud SQL storage (data intact)

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

---

## Cost Reference

| Resource | Running Cost | Paused Cost |
|----------|-------------|-------------|
| Cloud Run (idle) | $0 | $0 |
| Cloud SQL | ~$15-50/mo | ~$2/mo |
| Memorystore Redis | ~$15-30/mo | $0 (deleted) |
| GCS Storage | ~$0.02/GB/mo | ~$0.02/GB/mo |
| Secret Manager | ~$0.06/secret/mo | ~$0.06/secret/mo |

*Actual costs vary by region and usage. Use `stop-gcp.sh` to minimize spend during development.*
