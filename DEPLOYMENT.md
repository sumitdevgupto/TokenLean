# Deployment Guide

Complete step-by-step guide for deploying, managing, and tearing down the Token Optimisation Framework.

This project supports **two deployment modes** with identical G1-G18 functionality:

- **GCP Deployment** — Managed services, production-grade, pay-per-use. See [`docs/deployment-gcp.md`](docs/deployment-gcp.md) for a focused GCP guide.
- **Local Deployment** — Docker Compose on your machine, zero GCP cost. See [`docs/deployment-local.md`](docs/deployment-local.md) for a focused local guide.

The document below focuses on **GCP deployment**. For local deployment, see the link above.

---

## Step-by-Step Deployment Workflow

Follow these steps in order to deploy the framework. Each step includes the script to run and what it accomplishes.

### Step 0: Prerequisites (One-Time Setup)
**Before running any scripts:**
1. Install required tools: gcloud CLI, Docker, Terraform ≥ 1.5, Python with PyYAML
2. Authenticate with GCP: `gcloud auth login && gcloud auth application-default login`
3. Set your project: `gcloud config set project YOUR_PROJECT_ID`
4. Copy and fill templates:
   ```bash
   cp .env.gcp.template .env.gcp
   cp infra/terraform.tfvars.template infra/terraform.tfvars
   cp config/keys.yaml.template config/keys.yaml
   # Edit all three files with your real values
   ```
   > **Note:** The deploy script automatically creates the Terraform remote state bucket (`${PROJECT_ID}-tf-state`) if it doesn't exist. No manual setup required.

### Step 1: Validate Environment
**Script:** `scripts/gcp/pre-deploy-check.sh`
```bash
./scripts/gcp/pre-deploy-check.sh --project YOUR_PROJECT_ID --region asia-south1
```
**What it checks:**
- All CLI tools installed (gcloud, docker, terraform, redis-cli)
- GCP authentication and project access
- Required IAM roles granted
- terraform.tfvars and keys.yaml properly configured
- Docker daemon running
- Python dependencies available

**Proceed only if validation passes.**

### Step 2: Clean Up Any Zombie Infrastructure (Optional but Recommended)
**Script:** `scripts/gcp/stop-gcp.sh`
```bash
./scripts/gcp/stop-gcp.sh --project YOUR_PROJECT_ID --region asia-south1
```
**What it does:**
- Backs up any existing Redis data to GCS
- Deletes Memorystore Redis instance (stops billing)
- Stops Cloud SQL instance (stops compute billing)
- Ensures clean state before fresh deployment

**Use this if:**
- Previous deployment failed partially
- You have old infrastructure running from earlier attempts
- You want to ensure a completely fresh start

**Skip if:** This is your first deployment and no infrastructure exists yet.

### Step 3: Deploy Infrastructure and Services (Full Deploy)
**Script:** `scripts/gcp/gcp-deploy.sh`
```bash
./scripts/gcp/gcp-deploy.sh --project YOUR_PROJECT_ID --region asia-south1
```
**What it does:**
1. **Terraform provisioning:**
   - VPC network and subnets
   - Artifact Registry (Docker images)
   - GCS bucket (config + backups)
   - Cloud SQL (PostgreSQL + pgvector)
   - Memorystore Redis
   - Secret Manager (LLM keys, DB password)
   - Service accounts with IAM bindings
   - Cloud Tasks queue

2. **Docker builds:**
   - Builds proxy image
   - Builds llmlingua-sidecar image
   - Builds routellm-sidecar image
   - Builds tika-sidecar image (if `src/tika-sidecar/` exists — G03 document extraction)

3. **Secret provisioning:**
   - Uploads LLM keys from keys.yaml to Secret Manager
   - Generates random secrets (DB password, Grafana admin, etc.)

4. **Cloud Run deployments:**
   - Deploys llmlingua-svc (G1 compression)
   - Deploys routellm-svc (G6 routing)
   - Deploys langfuse-svc (G18 observability)
   - Deploys grafana-svc (dashboards)
   - Deploys token-proxy (main proxy - PUBLIC ENDPOINT)
   - Configures Qdrant, Prometheus, Alertmanager

5. **Config upload:**
   - Patches config.yaml with real sidecar URLs (llmlingua, routellm, tika)
   - Uploads to GCS bucket

6. **Prometheus/Alertmanager patch (`patch_prometheus`):**
   - Reads the real proxy and alertmanager Cloud Run URLs
   - Writes them back into `infra/terraform.tfvars` (`proxy_service_url`, `alertmanager_url`)
   - Re-applies Terraform targeting only the two config secrets
   - Forces new Cloud Run revisions so services mount the updated secrets
   - **This resolves the bootstrap chicken-and-egg**: Prometheus config needs the proxy URL, which only exists after Cloud Run deploy

**Duration:** ~15-20 minutes (mostly Terraform and Docker builds)

### Step 4: Verify Health
**Script:** `scripts/gcp/post-deploy-check.sh`
```bash
./scripts/gcp/post-deploy-check.sh --project YOUR_PROJECT_ID --region asia-south1
```
**What it checks:**
- Cloud SQL is RUNNABLE
- Memorystore Redis is READY
- All 8 Cloud Run services are deployed and responding
- HTTP endpoints return 200 OK
- Secrets are accessible
- GCS bucket and config.yaml exist

**Expected output:** `✓ ALL SYSTEMS HEALTHY`

### Step 5: Test the Proxy
```bash
# Get proxy URL
PROXY_URL=$(gcloud run services describe token-proxy \
  --region=asia-south1 --project=YOUR_PROJECT_ID \
  --format="value(status.url)")

# Test with your proxy API key
curl -H "Authorization: Bearer YOUR_PROXY_KEY" \
  "${PROXY_URL}/v1/models"
```

### For Code Changes (After Initial Deploy)
**Script:** `scripts/gcp/gcp-deploy.sh --skip-infra`
```bash
./scripts/gcp/gcp-deploy.sh --skip-infra --project YOUR_PROJECT_ID --region asia-south1
```
**Use this when:**
- You modified code in `src/`
- You updated config files
- You changed Dockerfiles

**What it skips:** Terraform infrastructure (faster, ~5 minutes)

### To Pause (Zero Cost)
**Script:** `scripts/gcp/stop-gcp.sh`
```bash
./scripts/gcp/stop-gcp.sh --project YOUR_PROJECT_ID
```
**Result:** ~$2/month billing only (Cloud SQL storage)

### To Resume
**Script:** `scripts/gcp/start-gcp.sh` + Terraform
```bash
# Start Cloud SQL
./scripts/gcp/start-gcp.sh --project YOUR_PROJECT_ID

# Recreate Redis (takes ~10 min)
cd infra && terraform apply

# Restore Redis from backup (optional, if you had data)
gcloud redis instances import gs://<bucket>/backups/redis-xxx.rdb \
  --instance=token-opt-redis --region=asia-south1

# Verify
./scripts/gcp/post-deploy-check.sh
```

### Summary of Scripts

| Script | Purpose | When to Run |
|--------|---------|-------------|
| `pre-deploy-check.sh` | Check prerequisites | Before any deployment |
| `stop-gcp.sh` | Backup & delete Redis, stop SQL | Before fresh deploy OR pause billing |
| `gcp-deploy.sh` | Full deployment | First deploy OR infra changes |
| `gcp-deploy.sh --skip-infra` | Service-only redeploy | Code/config changes only |
| `start-gcp.sh` | Start Cloud SQL | Resume after pause |
| `post-deploy-check.sh` | Verify all services | After deploy OR before testing |
| `issue-key.sh issue --user` | Issue a proxy API key to a developer | After deploy, for each developer/team |
| `issue-key.sh revoke --user` | Revoke a developer's proxy key | Offboarding or key rotation |
| `issue-key.sh list` | List all key holders | Audit / access review |

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Pre-Deployment Validation](#pre-deployment-validation)
3. [Configuration Setup](#configuration-setup)
4. [Initial Deployment](#initial-deployment)
5. [Re-deployment After Code Changes](#re-deployment-after-code-changes)
6. [Proxy API Keys (Local vs GCP)](#proxy-api-keys-local-vs-gcp)
7. [Build-Time Quality Gates & Optional Evals](#build-time-quality-gates--optional-evals)
8. [Cost Management](#cost-management)
9. [Complete Teardown](#complete-teardown)
10. [Troubleshooting](#troubleshooting)
11. [Quick Reference](#quick-reference-deployment-workflow)
   - [GCP First-Time Deployment](#first-time-deployment)
   - [Daily Development Loop](#daily-development-loop)
   - [Pause/Resume for Cost Savings](#pauseresume-for-cost-savings)
   - [Local Development (Zero GCP Cost)](#local-development-zero-gcp-cost)

---

## Prerequisites

### Required Tools

Install the following tools on your local machine:

- **gcloud CLI** - [Install guide](https://cloud.google.com/sdk/docs/install)
- **Docker** - [Install guide](https://docs.docker.com/get-docker/)
- **Terraform** (>= 1.8) - [Install guide](https://developer.hashicorp.com/terraform/install)
- **Python 3** with PyYAML - `pip install pyyaml` (also `tiktoken` for exact G02 token counts: `pip install tiktoken`)
- **redis-cli** (optional but recommended for backup/restore)
  - macOS: `brew install redis`
  - Ubuntu/Debian: `sudo apt-get install redis-tools`
  - Windows (Git Bash): Download from https://redis.io/download
- **Node.js** (`^20.20 || >=22.22`) — **only** needed for the optional Promptfoo quality eval (`--promptfoo`). Not required for deploying the proxy itself.
  - Recommended on Windows via [nvm-windows](https://github.com/coreybutler/nvm-windows):
    ```powershell
    winget install CoreyButler.NVMforWindows
    # open a NEW terminal, then:
    nvm install lts
    nvm use lts            # needs an elevated/admin shell (creates the version symlink)
    node -v                # confirm >= 22.22 (or >= 20.20)
    ```
  - If an older Node is first on your PATH (e.g. a manual `D:\nodejs` install), it can shadow the nvm-managed Node — open a fresh terminal, and remove the stale entry from PATH if `node -v` still shows the old version.

### Running Shell Scripts on Windows

The deployment scripts are written in Bash. On Windows, use one of these methods:

**Option 1: Git Bash (Recommended)**
```powershell
# Install Git for Windows from https://git-scm.com/download/win
# Then run scripts using Git Bash:
bash ./scripts/gcp/pre-deploy-check.sh
```

**Option 2: WSL (Windows Subsystem for Linux)**
```powershell
# Install WSL from Microsoft Store
# Then run scripts:
wsl bash ./scripts/gcp/pre-deploy-check.sh
```

**Option 3: Git Bash Terminal**
- Right-click in your project folder
- Select "Git Bash Here"
- Run scripts directly: `./scripts/gcp/pre-deploy-check.sh`

### GCP Project Setup

1. Create a GCP project or use an existing one
2. Enable billing for the project
3. Set your active project:
   ```bash
   gcloud config set project YOUR_PROJECT_ID
   ```

### Configure Environment File (Optional)

Copy the GCP environment template and configure your settings:
```bash
cp .env.gcp.template .env.gcp
```

Edit `.env.gcp`:
```bash
GCP_PROJECT_ID=your-gcp-project-id
GCP_REGION=asia-south1
```

**Note:** All GCP scripts automatically load `.env.gcp` if present, falling back to `.env`. You can also pass `--project` and `--region` as command-line arguments, or let scripts use your active gcloud configuration.

### Recommended Asia Regions

Choose a region based on your location:
- `asia-south1` (Mumbai) - Recommended for India
- `asia-southeast1` (Singapore) - Southeast Asia
- `asia-northeast1` (Tokyo) - Japan
- `asia-east1` (Taiwan) - East Asia
- `asia-northeast3` (Seoul) - South Korea

### Required IAM Roles

Your GCP account must have the following roles on the project:

- `roles/editor` - Base project access
- `roles/cloudsql.admin` - Manage Cloud SQL instances
- `roles/redis.admin` - Manage Memorystore Redis
- `roles/storage.admin` - Manage GCS buckets
- `roles/secretmanager.admin` - Manage Secret Manager
- `roles/run.admin` - Manage Cloud Run services
- `roles/cloudbuild.builds.builder` - Build Docker images
- `roles/serviceusage.serviceUsageAdmin` - Enable required APIs
- `roles/artifactregistry.admin` - Push Docker images

**Grant roles to yourself:**
```bash
PROJECT_ID=your-project-id
EMAIL=your-email@example.com

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="user:$EMAIL" \
  --role="roles/editor"

# Add other roles as needed...
```

### Authentication

Authenticate with GCP:
```bash
gcloud auth login
gcloud auth application-default login
```

---

## Pre-Deployment Validation

Before deploying, run the validation script to check all prerequisites:

```bash
# Option 1: Use .env file (recommended)
./scripts/gcp/pre-deploy-check.sh

# Option 2: Pass parameters explicitly
./scripts/gcp/pre-deploy-check.sh --project YOUR_PROJECT_ID --region asia-south1
```

This script checks:

**Local Dependencies:**
- ✓ Required CLI tools installed (gcloud, docker, terraform)
- ✓ Python with PyYAML installed
- ✓ redis-cli available (optional, for backup/restore)
- ✓ Docker daemon running and can access Google registries
- ✓ Network connectivity to GCP

**GCP Configuration:**
- ✓ GCP authentication and project access
- ✓ Billing enabled for project (required for deployment)
- ✓ Required IAM roles granted on project
- ✓ Required GCP APIs enabled (or will be auto-enabled by Terraform)

**Project Files:**
- ✓ terraform.tfvars configured with valid project_id
- ✓ config/keys.yaml exists with real API keys (not placeholders)
- ✓ Required config files present (bypass-rules.yaml, tool-registry.yaml)

**Fix any errors before proceeding with deployment.**

---

## Configuration Setup

### 1. Configure Environment

```bash
cp .env.gcp.template .env.gcp
# Edit .env.gcp with your GCP_PROJECT_ID and GCP_REGION
```

### 2. Configure Terraform Variables

Copy the template and edit it:
```bash
cp infra/terraform.tfvars.template infra/terraform.tfvars
```

Edit `infra/terraform.tfvars`:
```hcl
project_id             = "YOUR_GCP_PROJECT_ID"
region                 = "asia-south1"
db_tier                = "db-g1-small"       # upgrade to db-n1-standard-2 for production
redis_tier             = "BASIC"             # upgrade to STANDARD_HA for production
redis_memory_size_gb   = 1
artifact_registry_repo = "token-opt"
config_bucket_name     = ""                  # leave empty to auto-generate

# These two are set AUTOMATICALLY by gcp-deploy.sh after first deploy (patch_prometheus step).
# Leave blank here — the script fills them in.
proxy_service_url      = ""
alertmanager_url       = ""
```

> **Note:** `proxy_service_url` and `alertmanager_url` are written back into this file automatically by `gcp-deploy.sh` after services are deployed. Do not fill them in manually.

### 3. Configure LLM API Keys

Copy the template and fill in real API keys:
```bash
cp config/keys.yaml.template config/keys.yaml
```

Edit `config/keys.yaml` with your actual LLM provider keys:
```yaml
llm_keys:
  openai:    "sk-proj-YOUR_REAL_OPENAI_KEY"
  anthropic: "sk-ant-YOUR_REAL_ANTHROPIC_KEY"
  google:    "AIza-YOUR_REAL_GOOGLE_KEY"
  mistral:   "YOUR_REAL_MISTRAL_KEY"
  routellm:  "sk-proj-YOUR_REAL_OPENAI_KEY"  # Can reuse OpenAI key
```

**Important:** This file is gitignored and never committed. The deployment script automatically provisions these keys to GCP Secret Manager.

### 4. Review Config Files

Review and customize if needed:
- `config/config.yaml.template` - Main proxy configuration
- `config/bypass-rules.yaml` - G4 bypass rules
- `config/tool-registry.yaml` - G8 tool definitions

---

## Initial Deployment

Run the full deployment script:

```bash
# Option 1: Use .env.gcp file (recommended)
source .env.gcp && ./scripts/gcp/gcp-deploy.sh

# Option 2: Pass parameters explicitly
./scripts/gcp/gcp-deploy.sh --project YOUR_PROJECT_ID --region asia-south1
```

### What Gets Deployed

**Infrastructure (Terraform):**
- VPC network and subnets
- VPC connector for Cloud Run
- Artifact Registry (Docker images)
- GCS bucket (config + Redis backups)
- Cloud SQL (PostgreSQL 15 + pgvector)
- Memorystore Redis 7
- Secret Manager (LLM keys + DB password)
- Service accounts with IAM bindings
- Cloud Tasks queue (G13 batching)

**Cloud Run Services:**
- `token-proxy` - Main proxy (public endpoint)
- `llmlingua-svc` - G1 compression sidecar (internal)
- `routellm-svc` - G6 routing sidecar (internal)
- `tika-svc` - G3 document extraction sidecar (internal, deployed if `src/tika-sidecar/` exists)
- `langfuse-svc` - G18 observability & trace UI (public if `LANGFUSE_UI_PUBLIC=1`)
- `grafana-svc` - Dashboards (public)
- `token-opt-qdrant` - Vector database for RAG (internal)
- `token-opt-prometheus` - Metrics collection (internal)
- `token-opt-alertmanager` - Alert routing (internal)

### Post-Deployment

The script outputs:
```
╔══════════════════════════════════════════════════════╗
║        Token Optimisation Proxy — DEPLOYED           ║
╠══════════════════════════════════════════════════════╣
║ Proxy endpoint:  https://token-proxy-xxx.run.app
║ Grafana:         https://grafana-xxx.run.app
║ Langfuse:        https://langfuse-svc-xxx.run.app
║
║ Developer integration (one-line change):
║   base_url = https://token-proxy-xxx.run.app/v1
║   api_key  = <proxy-key>
╚══════════════════════════════════════════════════════╝
```

1. **Check health of all components:**
   ```bash
   ./scripts/gcp/post-deploy-check.sh
   ```
   This verifies all services are running and healthy before testing.

2. **Test the proxy:**
   ```bash
   curl -H "Authorization: Bearer YOUR_PROXY_KEY" \
     https://token-proxy-xxx.run.app/v1/models
   ```

3. **Access Grafana:**
   - URL: `https://grafana-xxx.run.app`
   - Default credentials: `admin` (password from Secret Manager)
   - Get password: `gcloud secrets versions access latest --secret=grafana-admin-password`

4. **Access Langfuse:**
   - URL: `https://langfuse-svc-xxx.run.app`
   - Note: If private, use: `gcloud run services proxy langfuse-svc --region=asia-south1`

5. **Configure Langfuse API keys (optional but recommended for G18 observability):**
   The proxy needs Langfuse API keys to send traces. Complete this after Langfuse is deployed:
   ```bash
   # 1. Open the Langfuse URL printed at the end of deploy
   # 2. Log in → Settings → API Keys → Create new key
   # 3. Copy the public key and secret key
   # 4. Add them to your local config/keys.yaml:
   langfuse_keys:
     public_key: "pk-lf-..."   # From Langfuse UI
     secret_key: "sk-lf-..."   # From Langfuse UI
   # 5. Re-deploy to provision the keys to Secret Manager:
   ./scripts/gcp/gcp-deploy.sh --skip-infra
   ```
   > **Note:** This step is optional. If skipped, Langfuse tracing will be disabled but the proxy will still function. You can complete this later when you're ready to enable observability.

6. **Issue developer API keys:**
   ```bash
   # Issue a key for a developer or team
   ./scripts/issue-key.sh issue --user alice@example.com

   # List all current key holders
   ./scripts/issue-key.sh list

   # Revoke a user's key(s)
   ./scripts/issue-key.sh revoke --user alice@example.com
   ```
   Keys are stored as SHA-256 hashes in Secret Manager (`token-proxy-api-keys`). Developers receive only the raw key — never LLM provider keys.

7. **Validate Framework Efficiency (Post-Deployment Test):**

   Run the reproducible savings benchmark (`examples/benchmark/`) against the
   deployed proxy:

   ```bash
   # Get the deployed proxy URL
   PROXY_URL=$(gcloud run services describe token-proxy \
     --region=asia-south1 --format='value(status.url)')

   # Issue a test key if needed
   TEST_KEY=$(./scripts/issue-key.sh issue --user test@example.com | grep -o 'Key: [^[:space:]]*' | cut -d' ' -f2)

   # Run the benchmark against the deployed proxy
   python examples/benchmark/run_benchmark.py \
     --proxy-url "$PROXY_URL" \
     --api-key "$TEST_KEY"
   ```

   **What this does:**
   - Sends a realistic single-tenant support-assistant workload (`examples/benchmark/dataset.jsonl`).
   - Prints total token savings, an estimated cost saving, and a per-group breakdown.
   - Writes full detail to `examples/benchmark/last_run.json`.

   Savings are measured with the same `_token_opt` metric as the project's headline
   result. See `examples/benchmark/README.md` for methodology and scope. This
   confirms the G0–G28 optimisations are working against your deployment.

---

## Re-deployment After Code Changes

When you make code changes (not infrastructure changes), use the `--skip-infra` flag:

```bash
# Option 1: Use .env.gcp file (recommended)
source .env.gcp && ./scripts/gcp/gcp-deploy.sh --skip-infra

# Option 2: Pass parameters explicitly
./scripts/gcp/gcp-deploy.sh --skip-infra --project YOUR_PROJECT_ID --region asia-south1
```

This skips Terraform and only:
- Rebuilds Docker images
- Pushes to Artifact Registry
- Re-deploys Cloud Run services
- Updates config.yaml with new sidecar URLs

**Full workflow for code changes:**
```bash
# 1. Make code changes
# 2. Validate (optional but recommended)
./scripts/gcp/pre-deploy-check.sh

# 3. Re-deploy services
./scripts/gcp/gcp-deploy.sh --skip-infra

# 4. Check health
./scripts/gcp/post-deploy-check.sh
```

**Use `--skip-infra` when:**
- Modified Python/Go/Java code in `src/`
- Updated config files
- Changed Dockerfiles

**Do NOT use `--skip-infra` when:**
- Modified `infra/` Terraform files
- Need to change infrastructure (e.g., upgrade DB tier, increase Redis memory)

---

## Proxy API Keys (Local vs GCP)

The proxy authenticates every caller with a **proxy key** (`tok-…`) in the
`Authorization: Bearer` header — never your raw LLM provider key. The provider
keys stay server-side (Secret Manager on GCP, `.env`/`config/keys.yaml` locally)
and are used by the proxy to call OpenAI/Anthropic/etc. on your behalf.

**Local and GCP use separate key stores**, so a key minted for one environment
will **not** authenticate against the other (you'll get a `401`):

| | Local (Docker) | GCP (Cloud Run) |
|---|---|---|
| Validation store | `config/local-keys.json` (SHA-256 hashes) | Secret Manager secret `token-proxy-api-keys` |
| Raw `tok-…` keys live in | `.env` as `ROI_PROXY_API_KEY_<TENANT>` | **not persisted** — only the hash is stored; the raw key is shown **once** at issue time |

A request is authenticated by hashing the incoming `tok-…` and checking that the
hash exists in *that environment's* store. The two stores are independent, which
is why a local key returns `401` on the deployed proxy and vice-versa.

### Issuing / managing GCP proxy keys

[`scripts/issue-key.sh`](scripts/issue-key.sh) mints a key, stores its SHA-256 hash
in `token-proxy-api-keys`, and prints the raw key **once**:

```bash
./scripts/issue-key.sh issue  --user <user-id> --project <GCP_PROJECT_ID>
./scripts/issue-key.sh list                    --project <GCP_PROJECT_ID>   # who holds keys
./scripts/issue-key.sh revoke --user <user-id> --project <GCP_PROJECT_ID>
```

- On the **first** `gcp-deploy.sh` run, if the secret has no version, an `admin`
  key is auto-issued and printed in the deploy logs. Subsequent deploys skip this.
- The `--user` value is bookkeeping only — auth is purely by hash, so **any**
  issued key authenticates. Store the raw key securely; it cannot be retrieved again.
- `--project` defaults to `GCP_PROJECT_ID` (from `.env`) or your active `gcloud` config.

---

## Build-Time Quality Gates & Optional Evals

The framework ships three build-time checks. The G02 budget gate runs on every
build; the Promptfoo eval and DSPy optimiser are opt-in via flags.

### G02 prompt-template token-budget gate (always on)

[`scripts/ci/validate-templates.sh`](scripts/ci/validate-templates.sh) **fails the
build** if any registered prompt template exceeds its token budget — preventing
prompts from silently bloating.

- **Budgets** are declared in `config/config.yaml.template` under
  `groups.G2_template_registry.budgets.<template-id>` (`system_prompt_max`,
  `total_input_max`).
- **Template content** lives in `config/templates/<template-id>.yaml` (fields
  `system_prompt`, `example_input`). Shipped example: `customer-support-classifier`.
- **Runs in:** local build ([`build-local.sh`](scripts/local/build-local.sh)), GCP
  build ([`gcp-deploy.sh`](scripts/gcp/gcp-deploy.sh), before any image is built),
  Cloud Build (`ci/cloudbuild*.yaml`), and GitHub Actions (`.github/workflows/ci.yml`).
- Soft-skips with a warning if Python/PyYAML is missing on the host; it only
  **fails** on a real budget violation. Uses `tiktoken` if available, else a
  ~4-chars/token fallback.

**To add a budgeted template:** add a budget block in `config.yaml.template` and a
matching `config/templates/<id>.yaml`, then run the gate (or any build) to verify.

### Optional: Promptfoo quality eval (`--promptfoo`)

[`ci/promptfoo-eval.sh`](ci/promptfoo-eval.sh) sends the `customer-support-classifier`
prompt **through the proxy** and asserts the responses are well-formed JSON with
valid category/priority enums — a regression gate for output quality. Opt-in and
**non-fatal** (runs after the stack is up / deploy completes; a failure is reported
but doesn't tear down the stack or fail the deploy).

```bash
# Local — stack must be up; targets http://localhost:4000
bash scripts/local/build-local.sh --promptfoo

# GCP — targets the deployed Cloud Run proxy URL automatically
./scripts/gcp/gcp-deploy.sh --skip-infra --promptfoo
```

**Prerequisites:**
- **Node.js** `^20.20 || >=22.22` (see [Required Tools](#required-tools)).
- **A proxy key the target accepts.** Resolution order in the script:
  `PROXY_API_KEY` (env, highest) → `ROI_PROXY_API_KEY_<TENANT>` (default tenant
  `NOVA_MED`; override with `PROMPTFOO_TENANT=SHOP_BOT`) → direct scan of
  `.env`/`.env.gcp`.
  - **Local:** already satisfied — `build-local.sh` sources `.env`, whose
    `ROI_PROXY_API_KEY_*` keys match `config/local-keys.json`.
  - **GCP:** you must supply a **GCP-issued** key (local keys won't authenticate —
    see [Proxy API Keys](#proxy-api-keys-local-vs-gcp)):
    ```bash
    export PROXY_API_KEY=$(./scripts/issue-key.sh issue --user promptfoo \
      --project <GCP_PROJECT_ID> | grep -o 'Key: [^[:space:]]*' | cut -d' ' -f2)
    ./scripts/gcp/gcp-deploy.sh --skip-infra --promptfoo
    ```
    Or persist it: add `export PROXY_API_KEY=tok-…` to `.env.gcp` (sourced by `gcp-deploy.sh`).

**Privacy & output:** results stay **local** — the script does not use promptfoo's
`--share` (no upload of prompts/responses) and disables telemetry/update pings.
Results are written to `reports/promptfoo/` (gitignored); inspect with
`npx promptfoo view`. Fixtures shipped: `tests/promptfoo-config.yaml`,
`tests/data/prompts/customer-support.txt`, `tests/data/promptfoo-tests.yaml`.
On older Node, pin a compatible release with `PROMPTFOO_VERSION=<version>`.

### Optional: DSPy prompt optimiser (`--dspy`)

[`ci/dspy-optimize.sh`](ci/dspy-optimize.sh) reads prompt templates from
`templates/prompts/*.yaml`, applies optimisation (currently heuristic filler-phrase
stripping / whitespace collapse), and writes trimmed copies to `optimized/prompts/`
(gitignored). Opt-in, **non-fatal**, and a purely local transform (it does not call
or modify the deployed proxy).

```bash
bash scripts/local/build-local.sh --dspy
./scripts/gcp/gcp-deploy.sh --skip-infra --dspy
bash scripts/local/build-local.sh --with-evals   # run both --promptfoo and --dspy locally
```

Shipped sample input: `templates/prompts/customer-support-classifier.yaml`. Compare
it against the generated `optimized/prompts/` copy to see what was trimmed.

### Run a live eval standalone (no rebuild / redeploy)

The flags above run the eval *as part of* a build/deploy. Once the proxy is up you
can also run [`ci/promptfoo-eval.sh`](ci/promptfoo-eval.sh) on its own — handy for
iterating without rebuilding or redeploying.

**Local** (stack already running; the proxy key auto-resolves from `.env`):
```bash
bash scripts/local/deploy-local.sh                  # only if the stack isn't up yet
PROXY_URL=http://localhost:4000 bash ci/promptfoo-eval.sh
```

**GCP** (against an already-deployed proxy — you **must** pass a GCP-issued key):
```bash
PROXY_URL=$(gcloud run services describe token-proxy \
  --region=asia-south1 --project=<GCP_PROJECT_ID> --format='value(status.url)')
export PROXY_API_KEY=tok-…           # issue via scripts/issue-key.sh (a GCP key)
PROXY_URL="$PROXY_URL" bash ci/promptfoo-eval.sh
```

> On GCP, always set `PROXY_API_KEY` explicitly. If unset, the fallback scan picks
> up a *local* `.env` key, which returns `401` against the deployed proxy.

Results are written to `reports/promptfoo/results.json` (gitignored); open the local
viewer with `npx promptfoo view`. Ensure Node is `>= 22.22` on PATH (see
[Required Tools](#required-tools)); on older Node, pin `PROMPTFOO_VERSION=<version>`.

---

## Cost Management

Achieve **near-zero cost** (~$2/month) when not using the framework with our automated backup/delete/restore cycle.

### Stop Infrastructure (Zero Cost)

```bash
# Option 1: Use .env.gcp file (recommended)
source .env.gcp && ./scripts/gcp/stop-gcp.sh

# Option 2: Pass parameters explicitly
./scripts/gcp/stop-gcp.sh --project YOUR_PROJECT_ID --region asia-south1
```

**What happens:**
1. **Redis backup** → Exported to GCS (`gs://<bucket>/backups/redis-TIMESTAMP.rdb`)
2. **Redis deleted** → Stops all Memorystore billing (~$20-50/month saved)
3. **Cloud SQL stopped** → Stops compute billing (~$2/month storage only)
4. **Cloud Run** → Already scales to zero automatically ($0)

**Confirmation required:** Script asks for confirmation before deleting Redis.

**Note:** If `redis-cli` is not installed, backup is skipped and Redis data will be lost.

### Start Infrastructure (Restore)

```bash
# Option 1: Use .env.gcp file (recommended)
source .env.gcp && ./scripts/gcp/start-gcp.sh

# Option 2: Pass parameters explicitly
./scripts/gcp/start-gcp.sh --project YOUR_PROJECT_ID --region asia-south1
```

**What happens:**
1. **Cloud SQL started** → Resumes compute billing
2. **Redis check** → Warns if missing (needs Terraform to recreate)
3. **Restore guidance** → Shows commands to restore Redis from backup
4. **Cloud Run** → Auto-starts on first request

**To restore Redis after `start-gcp.sh`:**
```bash
# Recreate Redis via Terraform
cd infra
terraform apply

# After Redis is created (takes ~10 min), restore from backup:
gcloud redis instances import gs://<your-bucket>/backups/redis-YYYYMMDD-HHMMSS.rdb \
  --project=PROJECT_ID \
  --region=REGION \
  --instance=token-opt-redis
```

### Cost Summary

| Service | Running | Stopped | Savings |
|---------|---------|---------|---------|
| Cloud Run | $0 (scales to zero) | $0 | — |
| Cloud SQL (db-g1-small) | ~$15/month | ~$2/month (storage) | ~$13/month |
| Memorystore Redis (1GB BASIC) | ~$20-25/month | $0 | ~$20-25/month |
| GCS Storage (backup) | ~$0.02/GB | ~$0.02/GB | — |
| Prometheus/Alertmanager | Minimal usage | $0 (scales to zero) | — |
| **TOTAL** | **~$35-40/month** | **~$2-3/month** | **~$30-35/month** |

**Redis recreation time:** ~5-10 minutes via Terraform

---

## Complete Teardown

To completely remove all resources from GCP:

### 1. Stop Infrastructure (Optional but Recommended)

First, stop infrastructure to ensure clean state:
```bash
./scripts/gcp/stop-gcp.sh
```

### 2. Destroy Terraform Resources

```bash
cd infra
terraform destroy
```

This removes:
- VPC network and subnets
- VPC connector
- Artifact Registry
- GCS bucket (if `force_destroy = true` in Terraform)
- Cloud SQL instance
- Redis instance
- Secret Manager secrets
- Service accounts
- Cloud Tasks queue

### 3. Manual Cleanup (If Needed)

If Terraform destroy fails or leaves resources:

**Delete GCS bucket manually:**
```bash
gsutil -m rm -r gs://your-bucket-name
```

**Delete secrets manually:**
```bash
gcloud secrets delete llm-key-openai --project=PROJECT_ID
gcloud secrets delete llm-key-anthropic --project=PROJECT_ID
gcloud secrets delete llm-key-google --project=PROJECT_ID
gcloud secrets delete llm-key-mistral --project=PROJECT_ID
gcloud secrets delete routellm-openai-key --project=PROJECT_ID
gcloud secrets delete token-opt-db-password --project=PROJECT_ID
gcloud secrets delete token-proxy-api-keys --project=PROJECT_ID
```

**Delete service accounts manually:**
```bash
gcloud iam service-accounts delete token-opt-proxy-sa@PROJECT_ID.iam.gserviceaccount.com
gcloud iam service-accounts delete routellm-sidecar-sa@PROJECT_ID.iam.gserviceaccount.com
```

### 4. Disable APIs (Optional)

To stop billing for enabled APIs:
```bash
gcloud services disable run.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  cloudtasks.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  vpcaccess.googleapis.com \
  --project=PROJECT_ID
```

---

## Troubleshooting

### Validation Fails

**Error:** "terraform.tfvars not found"
```bash
cp infra/terraform.tfvars.template infra/terraform.tfvars
# Edit with your project ID
```

**Error:** "config/keys.yaml contains placeholder values"
```bash
cp config/keys.yaml.template config/keys.yaml
# Fill in real API keys
```

**Error:** "Docker daemon not running"
- Start Docker Desktop (Windows/Mac) or Docker service (Linux)

### Deployment Fails

**Error:** "Not authenticated"
```bash
gcloud auth login
gcloud auth application-default login
```

**Error:** "Project not accessible"
```bash
gcloud config set project YOUR_PROJECT_ID
gcloud projects describe YOUR_PROJECT_ID
```

**Error:** "Missing IAM roles"
- Contact your GCP administrator to grant required roles
- See [Prerequisites](#required-iam-roles) for the full list

**Error:** "Terraform init fails — bucket not found"
```bash
# Create the remote state bucket first (one-time)
gsutil mb -p YOUR_PROJECT_ID gs://YOUR_PROJECT_ID-tf-state

# Then retry
cd infra
terraform init -upgrade
```

**Error:** "Terraform init fails — other reason"
```bash
cd infra
terraform init -upgrade
```

**Error:** "Docker build fails"
- Check Docker daemon is running
- Verify sufficient disk space
- Check for network connectivity

**Error:** "Cloud Run deployment fails"
- Check VPC connector is ready: `gcloud vpc-access connectors describe token-opt-connector`
- Check service account has correct IAM roles
- View logs: `gcloud run services logs tail token-proxy`

**Error:** `routellm-svc` deploy fails with "caller does not have permission"
```bash
# Grant iam.serviceAccountUser on the routellm SA to your deploying identity
gcloud iam service-accounts add-iam-policy-binding \
  routellm-sidecar-sa@PROJECT_ID.iam.gserviceaccount.com \
  --member="user:YOUR_EMAIL" \
  --role="roles/iam.serviceAccountUser"
# Then re-apply Terraform to persist: cd infra && terraform apply
```

**Proxy starts but Redis features are disabled (G00 rate limit, G05 cache silent miss)**
```bash
# Verify REDIS_URL is set on the proxy
gcloud run services describe token-proxy --region=REGION \
  --format='value(spec.template.spec.containers[0].env)' | grep REDIS

# If empty, Redis host was not in Terraform output. Check:
cd infra && terraform output redis_host
```

**Prometheus showing no targets / Alertmanager webhook 404**
- This is the bootstrap issue. The `patch_prometheus` step at the end of `gcp-deploy.sh` resolves it automatically.
- If it was skipped (e.g. deploy failed partway), re-run:
  ```bash
  ./scripts/gcp/gcp-deploy.sh --skip-infra
  ```
  The `patch_prometheus` function is idempotent and safe to re-run.

### Redis Export/Import Fails

**Error:** "Redis export timeout"
- Export can take 2-5 minutes for large datasets
- Check Redis instance size and data volume
- Verify GCS bucket has sufficient permissions

**Error:** "Redis import fails"
- Verify backup file exists in GCS
- Check Redis instance is in READY state before import
- Ensure backup file is a valid RDB format

### Cloud Run Services Not Starting

**Check service status:**
```bash
gcloud run services describe token-proxy --region=asia-south1
```

**View logs:**
```bash
gcloud run services logs tail token-proxy --region=asia-south1
```

**Common issues:**
- VPC connector not ready
- Secret Manager secrets not accessible
- Database connection string incorrect
- Config bucket not accessible

### Cost Management Issues

**Redis backup not found:**
```bash
# List available backups
gsutil ls gs://your-bucket/redis-backups/

# Use specific backup when starting
./scripts/gcp/start-gcp.sh --bucket gs://your-bucket/redis-backups/redis-20240101.rdb
```

**Cloud SQL won't start:**
```bash
# Check instance state
gcloud sql instances describe token-opt-pg

# Force start
gcloud sql instances patch token-opt-pg --activation-policy=ALWAYS
```

---

## Quick Reference: Deployment Workflow

### First-Time Deployment

```bash
# 1. Prerequisites
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# 2. Copy and fill templates
cp .env.gcp.template .env.gcp
cp infra/terraform.tfvars.template infra/terraform.tfvars
cp config/keys.yaml.template config/keys.yaml
# Edit all three files with your real values

# 3. Validate setup
source .env.gcp && ./scripts/gcp/pre-deploy-check.sh

# 4. Deploy everything (auto-creates Terraform state bucket if missing, includes patch_prometheus post-deploy step)
source .env.gcp && ./scripts/gcp/gcp-deploy.sh
# Takes ~15-20 minutes

# 5. Check health
source .env.gcp && ./scripts/gcp/post-deploy-check.sh

# 6. Issue a proxy API key for your first developer
./scripts/issue-key.sh issue --user YOUR_EMAIL --project YOUR_PROJECT_ID

# 7. Test the proxy
curl -H "Authorization: Bearer YOUR_PROXY_KEY" \
  https://token-proxy-xxx.run.app/v1/models
```

### Daily Development Loop

```bash
# Make code changes...

# Re-deploy services (fast, ~5 minutes)
source .env.gcp && ./scripts/gcp/gcp-deploy.sh --skip-infra

# Check health
source .env.gcp && ./scripts/gcp/post-deploy-check.sh
```

### Pause/Resume for Cost Savings

```bash
# Stop (achieve ~$2/month cost)
source .env.gcp && ./scripts/gcp/stop-gcp.sh

# Start (restore functionality)
source .env.gcp && ./scripts/gcp/start-gcp.sh

# If Redis was deleted, restore it:
cd infra && terraform apply
gcloud redis instances import gs://<bucket>/backups/redis-xxx.rdb \
  --instance=token-opt-redis --region=asia-south1

# Verify everything is up
source .env.gcp && ./scripts/gcp/post-deploy-check.sh
```

### Storage Backend Selection

The proxy exports per-request JSONL records and config via a pluggable storage backend.
Select it with the `STORAGE_BACKEND` environment variable:

| Value | Behaviour | When to use |
|-------|-----------|-------------|
| `gcs` (default) | Uploads to GCS bucket set in `jsonl_gcs_bucket` config | GCP deployments |
| `local` | Writes to `STORAGE_LOCAL_PATH` (default `./token-usage-logs`) | Local dev / CI — no GCP credentials needed |

The proxy starts successfully with `STORAGE_BACKEND=local` even when `google-cloud-storage`
is not installed, so contributors can run the full stack without a GCP project.

Config loading from GCS is controlled separately by `CONFIG_GCS_BUCKET` (empty = use local file).

---

### Local Development (Zero GCP Cost)

```bash
# 1. Copy local env template
cp .env.template .env
# Edit .env with DB_PASSWORD, OPENAI_API_KEY, etc.

# 2. Deploy locally with seeding
source .env && ./scripts/local/deploy-local.sh --seed

# 3. Access services
# Proxy:    http://localhost:4000
# Grafana:  http://localhost:3000
# Langfuse: http://localhost:3100
# Qdrant:   http://localhost:6333

# 4. Stop local stack
source .env && ./scripts/local/stop-local.sh
```

### Emergency Commands

```bash
# View proxy logs
gcloud run services logs tail token-proxy --region=asia-south1

# Get Grafana password
gcloud secrets versions access latest --secret=grafana-admin-password

# Force start Cloud SQL
gcloud sql instances patch token-opt-pg --activation-policy=ALWAYS

# Check all service URLs
gcloud run services list --region=asia-south1 --format='table(metadata.name,status.url)'
```

---

## Additional Resources

- [GCP Deployment Guide](docs/deployment-gcp.md) — Focused GCP-only reference
- [Local Deployment Guide](docs/deployment-local.md) — Docker Compose local setup
- [Developer Onboarding Guide](docs/developer-onboarding.md)
- [Configuration Reference](docs/config-reference.md)
- [Token Optimisation Playbook v7](token_optimization_playbook_v7.md)
- [GCP Cloud Run Documentation](https://cloud.google.com/run/docs)
- [Terraform GCP Provider](https://registry.terraform.io/providers/hashicorp/google/latest/docs)
- [Memorystore Redis Import/Export](https://cloud.google.com/memorystore/docs/redis/import-export)
