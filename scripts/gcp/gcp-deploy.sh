#!/usr/bin/env bash
# =============================================================================
# gcp-deploy.sh — TokenLean — Token Optimisation Framework: one-stop GCP deployment
# =============================================================================
# Usage:
#   ./scripts/gcp/gcp-deploy.sh [OPTIONS]
#
# Options:
#   --skip-infra       Skip Terraform infra provisioning (use on re-deploys)
#   --project ID       GCP project ID (default: current gcloud config)
#   --region  REGION   GCP region (default: asia-south1)
#   --promptfoo        After deploy, run the Promptfoo quality eval against the
#                      deployed proxy URL (needs fixtures + a proxy API key; non-fatal)
#   --dspy             After deploy, run the DSPy prompt-template optimiser over
#                      templates/prompts (local transform; non-fatal)
#   --help             Show this help
#
# PRE-REQUISITE (admin only):
#   Copy config/keys.yaml.template → config/keys.yaml and fill in real LLM API keys.
#   This file is gitignored and never committed. The script automatically
#   provisions these keys to Secret Manager on each deploy if missing.
#
# First run (fresh GCP project):
#   ./scripts/gcp/gcp-deploy.sh
#
# Re-deploy after code change:
#   ./scripts/gcp/gcp-deploy.sh --skip-infra
# =============================================================================
set -euo pipefail

# ── Host-shell guard ────────────────────────────────────────────────────────────
# The GCP deploy must run from WSL Ubuntu / Linux / macOS / Cloud Shell — NEVER Git
# Bash or any Windows shell: Terraform launches local-exec provisioners via cmd.exe
# there (bash heredocs fail), psql is absent for the public-IP migrations, and CRLF
# corrupts sourced env files. Abort up front with the fix (docs/deployment-gcp.md).
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "ERROR: run this from WSL Ubuntu (or Linux/macOS/Cloud Shell), NOT Git Bash/Windows." >&2
    echo "  In WSL:  cd /mnt/d/<repo> && bash scripts/gcp/gcp-deploy.sh $*" >&2
    exit 2 ;;
esac

# ─── Defaults ─────────────────────────────────────────────────────────────────
SKIP_INFRA=false
SKIP_BUILD=false
RUN_PROMPTFOO=false
RUN_DSPY=false
PROJECT_ID=""
REGION="asia-south1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ─── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Load .env.gcp or .env file if exists ────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env.gcp"
if [[ ! -f "$ENV_FILE" ]]; then
  ENV_FILE="${REPO_ROOT}/.env"
fi
if [[ -f "$ENV_FILE" ]]; then
  info "Loading environment variables from ${ENV_FILE}..."
  set -a
  # Strip CRLF on the fly: env files edited on Windows carry \r, which bash would
  # append to every value (e.g. GCP_REGION=asia-south1$'\r') and corrupt gcloud args.
  source <(tr -d '\r' < "$ENV_FILE")
  set +a
  success "Loaded env file"
fi

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-infra)  SKIP_INFRA=true;  shift ;;
    --skip-build)  SKIP_BUILD=true;  shift ;;
    --promptfoo)   RUN_PROMPTFOO=true; shift ;;
    --dspy)        RUN_DSPY=true;      shift ;;
    --project)     PROJECT_ID="$2";  shift 2 ;;
    --region)      REGION="$2";      shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Resolve whether Cloud SQL is private-IP-only ─────────────────────────────
# private_cloud_sql=true means the instance has no public IPv4, so the off-VPC
# deploy host cannot tunnel to it — migrations run from an in-VPC Cloud Run Job
# instead of the Terraform local-exec provisioners (which are count=0 in that
# case). Resolution order: TF_VAR_private_cloud_sql env (set by the commercial
# deploy) → an uncommented private_cloud_sql line in infra/terraform.tfvars →
# false (OSS public-IP default).
resolve_private_sql() {
  if [[ -n "${TF_VAR_private_cloud_sql:-}" ]]; then
    PRIVATE_SQL="${TF_VAR_private_cloud_sql}"
  elif [[ -f "${REPO_ROOT}/infra/terraform.tfvars" ]] \
       && grep -Eq '^[[:space:]]*private_cloud_sql[[:space:]]*=[[:space:]]*true' "${REPO_ROOT}/infra/terraform.tfvars"; then
    PRIVATE_SQL="true"
  else
    PRIVATE_SQL="false"
  fi
}

# ─── Prerequisites check ──────────────────────────────────────────────────────
check_prereqs() {
  info "Checking prerequisites..."
  for cmd in gcloud docker terraform; do
    command -v "$cmd" &>/dev/null || error "$cmd is required but not installed."
  done

  resolve_private_sql

  # The schema migrations run through cloud-sql-proxy + psql ON THE HOST only on
  # the PUBLIC-IP path (Terraform local-exec provisioners). On the private-IP path
  # (PRIVATE_SQL=true) they run from an in-VPC Cloud Run Job (run-migrations-job.sh)
  # where psql executes in-cloud — so the host needs NEITHER tool. Only require them
  # for the public path.
  if [[ "$PRIVATE_SQL" != "true" ]]; then
    command -v cloud-sql-proxy &>/dev/null \
      || error "cloud-sql-proxy is required for schema migrations (public-IP Cloud SQL) but not installed. Install: https://cloud.google.com/sql/docs/postgres/sql-proxy#install (Linux: curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.11.0/cloud-sql-proxy.linux.amd64 && chmod +x cloud-sql-proxy && sudo mv cloud-sql-proxy /usr/local/bin/)."
    command -v psql &>/dev/null \
      || error "psql (PostgreSQL client) is required for schema migrations but not installed. Install: sudo apt-get install -y postgresql-client"
  else
    info "private_cloud_sql=true — host psql/cloud-sql-proxy not required (migrations run from an in-VPC Cloud Run Job)"
  fi

  # Direct VPC egress for the Cloud Run services that mount the Cloud SQL socket
  # (langfuse-svc, token-proxy, grafana-svc). On a PRIVATE-IP instance the socket
  # connector must dial 10.x:3307, which is only routable with VPC egress — without
  # it the container gets "connection to Cloud SQL instance at 10.x:3307 timed out"
  # and (for langfuse, which migrates at boot) fails its PORT startup probe.
  # Empty on the public-IP path so the OSS deploy is unchanged.
  VPC_EGRESS_FLAGS=""
  if [[ "$PRIVATE_SQL" == "true" ]]; then
    VPC_EGRESS_FLAGS="--network=default --subnet=default --vpc-egress=private-ranges-only"
    info "private_cloud_sql=true — DB-connected services get Direct VPC egress (${VPC_EGRESS_FLAGS})"
  fi

  gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q "@" \
    || error "Not authenticated. Run: gcloud auth login"

  # ADC — Terraform's google provider AND the Cloud SQL Auth Proxy (host-side on the
  # public path, in the Cloud Run migration Job on the private path) authenticate with
  # Application Default Credentials, which are SEPARATE from `gcloud auth login`.
  # Without this check a missing ADC surfaces only mid-`terraform apply` as a cryptic
  # provider/proxy auth error.
  gcloud auth application-default print-access-token &>/dev/null \
    || error "Application Default Credentials not set. Run: gcloud auth application-default login"

  if [[ -z "$PROJECT_ID" ]]; then
    PROJECT_ID="${GCP_PROJECT_ID:-}"
  fi
  if [[ -z "$PROJECT_ID" ]]; then
    PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
    [[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"
  fi

  if [[ -z "$REGION" ]]; then
    REGION="${GCP_REGION:-asia-south1}"
  fi

  success "Prerequisites OK — project: ${PROJECT_ID}, region: ${REGION}"
}

# ─── Ensure Terraform remote state bucket exists ───────────────────────────────
ensure_tf_state_bucket() {
  local bucket_name="${PROJECT_ID}-tf-state"
  if gsutil ls -b "gs://${bucket_name}" &>/dev/null; then
    success "Terraform state bucket exists: gs://${bucket_name}"
  else
    info "Creating Terraform remote state bucket: gs://${bucket_name}"
    gsutil mb -p "$PROJECT_ID" "gs://${bucket_name}" \
      || error "Failed to create Terraform state bucket. Check permissions."
    success "Terraform state bucket created"
  fi
}

# ─── Step 1: Terraform infra ──────────────────────────────────────────────────
provision_infra() {
  info "Provisioning GCP infrastructure with Terraform..."
  cd "${REPO_ROOT}/infra"

  if [[ ! -f terraform.tfvars ]]; then
    cp terraform.tfvars.template terraform.tfvars
    sed -i "s/YOUR_GCP_PROJECT_ID/${PROJECT_ID}/" terraform.tfvars
    sed -i "s/region *= *\"us-central1\"/region = \"${REGION}\"/" terraform.tfvars
    warn "Created infra/terraform.tfvars — review before continuing"
  fi

  ensure_tf_state_bucket

  terraform init -upgrade -reconfigure \
    -backend-config="bucket=${PROJECT_ID}-tf-state" \
    -backend-config="prefix=token-opt" 2>/dev/null \
    || terraform init -upgrade

  terraform plan -out=tfplan
  terraform apply -auto-approve tfplan

  # Export Terraform outputs for use in subsequent steps
  REGISTRY_URL=$(terraform output -raw artifact_registry_url)
  CONFIG_BUCKET=$(terraform output -raw config_bucket_name)
  DB_CONNECTION=$(terraform output -raw db_instance_connection_name)
  PROXY_SA=$(terraform output -raw proxy_service_account_email)
  QDRANT_URL=$(terraform output -raw qdrant_service_url)
  REDIS_HOST=$(terraform output -raw redis_host 2>/dev/null || echo "")
  if [[ -n "$REDIS_HOST" ]]; then
    REDIS_URL="redis://${REDIS_HOST}:6379/0"
    success "Redis URL constructed from Terraform output: ${REDIS_URL}"
  fi

  success "Infrastructure provisioned"
  cd "${REPO_ROOT}"
}

# ─── Step 2: Provision LLM keys to Secret Manager from config/keys.yaml ──
provision_llm_keys() {
  # SKIP_PLATFORM_KEYS=true (strict-BYOK commercial deploy) → skip the TENANT-SERVING
  # platform provider keys (llm-key-<provider>) so no platform key that could answer a
  # tenant request ever exists in the project. It deliberately does NOT skip the RouteLLM
  # embeddings key or Langfuse keys below — those are INFRA credentials (routellm-sidecar
  # embeddings / observability), never a tenant-answer path. Default (unset) = OSS behaviour.
  local skip_platform="${SKIP_PLATFORM_KEYS:-false}"
  info "Provisioning LLM API keys from ${REPO_ROOT}/config/keys.yaml..."
  local keys_file="${REPO_ROOT}/config/keys.yaml"
  # Under strict BYOK the file may legitimately have no provider keys — only require it to
  # exist when we actually need to read provider keys from it.
  if [[ "$skip_platform" != "true" && ! -f "$keys_file" ]]; then
    error "Keys file not found: ${keys_file}\nCopy config/keys.yaml.template → config/keys.yaml and fill in real values."
  fi

  parse_yaml_value() {
    local field="$1"
    # Tolerate a missing keys_file (strict-BYOK deploy may have none) — yield empty, not an error.
    [[ -f "$keys_file" ]] || return 0
    grep -E "^\s+${field}:" "$keys_file" | sed -E 's/.*:\s*\"?([^\"#]+)\"?.*/\1/' | tr -d '[:space:]'
  }

  store_key() {
    local secret_name="$1" yaml_field="$2" key_value
    key_value="$(parse_yaml_value "$yaml_field")"

    if [[ -z "$key_value" || "$key_value" == "sk-..."* || "$key_value" == "AI..."* || "$key_value" == "..."* ]]; then
      warn "Skipping ${secret_name}: value not set in ${keys_file}"
      return 1
    fi

    if timeout 30 gcloud secrets describe "$secret_name" --project="$PROJECT_ID" &>/dev/null; then
      echo -n "$key_value" | timeout 30 gcloud secrets versions add "$secret_name" \
        --data-file=- --project="$PROJECT_ID" &>/dev/null \
        || warn "Failed to update ${secret_name} — continuing"
      success "Updated  → ${secret_name}"
    else
      timeout 30 gcloud secrets create "$secret_name" \
        --project="$PROJECT_ID" \
        --replication-policy=automatic \
        --data-file=<(echo -n "$key_value") &>/dev/null \
        || warn "Failed to create ${secret_name} — continuing"
      success "Created  → ${secret_name}"
    fi
  }

  if [[ "$skip_platform" == "true" ]]; then
    info "SKIP_PLATFORM_KEYS=true — strict BYOK: NOT seeding any tenant-serving platform provider key (llm-key-*). Tenants supply their own."
  else
    # Mandatory key — the configured default provider (proxy.default_provider). No longer
    # hardcoded to OpenAI: set DEFAULT_PROVIDER to match your config so e.g. an Anthropic- or
    # Gemini-first deployment isn't forced to supply an OpenAI key.
    local default_provider="${DEFAULT_PROVIDER:-openai}"
    if ! store_key "llm-key-${default_provider}" "${default_provider}"; then
      error "Mandatory key missing in ${keys_file}: '${default_provider}' (your proxy.default_provider) is required.\nFill it in, or set DEFAULT_PROVIDER to a provider you have a key for."
    fi

    # Optional LLM provider keys — skip if not set. (Bedrock uses AWS SigV4 creds, not a key.)
    store_key "llm-key-anthropic"   "anthropic" || true
    store_key "llm-key-google"      "google"    || true
    store_key "llm-key-gemini"      "gemini"    || true
    store_key "llm-key-mistral"     "mistral"   || true
    store_key "llm-key-cohere"      "cohere"    || true
    store_key "llm-key-deepseek"    "deepseek"  || true
    store_key "llm-key-xai"         "xai"       || true
    store_key "llm-key-groq"        "groq"      || true
    store_key "llm-key-azure"       "azure"     || true
    store_key "llm-key-openrouter"  "openrouter" || true
    store_key "llm-key-opencode"    "opencode"  || true
  fi

  # RouteLLM embeddings key — INFRA credential for the routellm-sidecar (G06 mf/sw_ranking
  # routers call OpenAI embeddings). NOT a tenant-answer path, so it is seeded even under
  # strict BYOK. Provisioned when present in keys.yaml; a warning otherwise. `store_key`
  # tolerates a missing keys_file (parse yields empty → skip), so this is safe under BYOK.
  store_key "routellm-openai-key" "routellm" \
    || warn "routellm key not set — G06 mf/sw_ranking routers need an OpenAI key for embeddings; G06 routing will degrade. Disable G06 or use the causal_llm router if you don't use OpenAI."

  # Langfuse keys — only available after first deploy + manual UI step
  # These live under langfuse_keys: in keys.yaml (not llm_keys:)
  parse_langfuse_value() {
    local field="$1"
    grep -A5 'langfuse_keys:' "$keys_file" | grep -E "^\s+${field}:" \
      | sed -E 's/.*:\s*"?([^"#]+)"?.*/\1/' | tr -d '[:space:]'
  }

  store_langfuse_key() {
    local secret_name="$1" yaml_field="$2" key_value
    key_value="$(parse_langfuse_value "$yaml_field")"
    if [[ -z "$key_value" || "$key_value" == "pk-lf-..." || "$key_value" == "sk-lf-..." ]]; then
      warn "Skipping ${secret_name}: not yet set. Complete post-deploy Langfuse setup first (see keys.yaml.template)."
      return
    fi
    if timeout 30 gcloud secrets describe "$secret_name" --project="$PROJECT_ID" &>/dev/null; then
      echo -n "$key_value" | timeout 30 gcloud secrets versions add "$secret_name" \
        --data-file=- --project="$PROJECT_ID" &>/dev/null \
        || warn "Failed to update ${secret_name} — continuing"
      success "Updated  → ${secret_name}"
    else
      timeout 30 gcloud secrets create "$secret_name" \
        --project="$PROJECT_ID" \
        --replication-policy=automatic \
        --data-file=<(echo -n "$key_value") &>/dev/null \
        || warn "Failed to create ${secret_name} — continuing"
      success "Created  → ${secret_name}"
    fi
  }

  store_langfuse_key "langfuse-public-key" "public_key"
  store_langfuse_key "langfuse-secret-key" "secret_key"

  success "Secret provisioning complete"
}

# ─── Step 3: Upload config to GCS ────────────────────────────────────────────
upload_config() {
  info "Uploading configuration files to GCS..."

  # Upload config.yaml directly (contains our G1-G24 tuning; template is only the default skeleton)
  # If config.yaml is missing, fall back to generating from template
  if [[ -f "${REPO_ROOT}/config/config.yaml" ]]; then
    gsutil cp "${REPO_ROOT}/config/config.yaml" "gs://${CONFIG_BUCKET}/config/config.yaml"
    cp "${REPO_ROOT}/config/config.yaml" /tmp/config.yaml
  else
    warn "config/config.yaml not found — generating from template (G1-G24 tuning may be missing)"
    sed "s|\${CONFIG_GCS_BUCKET}|${CONFIG_BUCKET}|g;s|REPLACE_WITH_CONFIG_BUCKET|${CONFIG_BUCKET}|g" \
      "${REPO_ROOT}/config/config.yaml.template" > /tmp/config.yaml
    gsutil cp /tmp/config.yaml "gs://${CONFIG_BUCKET}/config/config.yaml"
  fi
  gsutil cp "${REPO_ROOT}/config/bypass-rules.yaml"   "gs://${CONFIG_BUCKET}/config/bypass-rules.yaml"
  [[ -f "${REPO_ROOT}/config/tool-registry.yaml" ]] && \
    gsutil cp "${REPO_ROOT}/config/tool-registry.yaml"  "gs://${CONFIG_BUCKET}/config/tool-registry.yaml" || \
    warn "config/tool-registry.yaml not found — skipping"
  [[ -f "${REPO_ROOT}/config/adaptive_bypass_rules.yaml" ]] && \
    gsutil cp "${REPO_ROOT}/config/adaptive_bypass_rules.yaml" \
      "gs://${CONFIG_BUCKET}/config/adaptive_bypass_rules.yaml" || \
    warn "config/adaptive_bypass_rules.yaml not found — G24 will have no bypass rules in GCP"
  # /tmp/config.yaml is kept for use by deploy_services; cleaned up after deploy

  success "Config uploaded to gs://${CONFIG_BUCKET}/config/"
}

# ─── Helper: ensure /tmp/config.yaml is available for deploy_services ───────────
prepare_config_yaml() {
  if [[ ! -f /tmp/config.yaml ]]; then
    if [[ -f "${REPO_ROOT}/config/config.yaml" ]]; then
      info "Copying config/config.yaml to /tmp/config.yaml (--skip-infra path)..."
      cp "${REPO_ROOT}/config/config.yaml" /tmp/config.yaml
    else
      info "Generating /tmp/config.yaml from template (--skip-infra path)..."
      sed "s|\${CONFIG_GCS_BUCKET}|${CONFIG_BUCKET}|g;s|REPLACE_WITH_CONFIG_BUCKET|${CONFIG_BUCKET}|g" \
        "${REPO_ROOT}/config/config.yaml.template" > /tmp/config.yaml
    fi
  fi
}

# ─── Step 3.5: G02 prompt-template token-budget gate ────────────────────────
# Fails the deploy before any image is built if a registered prompt template
# exceeds its budget (config.yaml.template → groups.G2_template_registry.budgets).
validate_templates() {
  info "Validating G02 prompt-template token budgets..."
  bash "${REPO_ROOT}/scripts/ci/validate-templates.sh" \
    || error "G02 template budget gate failed — fix the template or its budget before deploying"
  success "G02 template budgets OK"
}

# ─── Optional post-deploy steps (opt-in via --promptfoo / --dspy) ───────────────
# Non-fatal: the deploy has already succeeded, so a failure here is reported but
# does not fail the overall run.
run_promptfoo_eval() {
  if [[ -z "${PROXY_URL:-}" ]]; then
    warn "--promptfoo: PROXY_URL not available (deploy may have been skipped) — skipping eval"
    return 0
  fi
  info "Running Promptfoo quality eval against ${PROXY_URL} ..."
  warn "Promptfoo needs a valid proxy API key in PROXY_API_KEY (falls back to OPENAI_API_KEY)."
  PROXY_URL="${PROXY_URL}" bash "${REPO_ROOT}/ci/promptfoo-eval.sh" \
    || warn "promptfoo eval reported failures (deploy already completed successfully)"
}

run_dspy_optimize() {
  info "Running DSPy prompt-template optimisation (local transform) ..."
  bash "${REPO_ROOT}/ci/dspy-optimize.sh" \
    || warn "dspy optimisation reported issues (deploy already completed successfully)"
}

# ─── Step 4: Build and push Docker images (locally via Docker, then push) ────
# All images are built locally with --platform=linux/amd64 so there are zero
# Cloud Build minutes consumed and no waiting for remote queue slots.
# This satisfies the rule: "all builds local, only final deploy touches GCP."
build_and_push() {
  info "Configuring Docker authentication for Artifact Registry..."
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

  build_service_local() {
    local name="$1" ctx="$2"
    local image="${REGISTRY_URL}/${name}:latest"
    info "Building ${name} locally (docker build --platform=linux/amd64)..."
    docker build --platform=linux/amd64 -t "${image}" "${ctx}" \
      || error "docker build failed for ${name}"
    info "Pushing ${name} to Artifact Registry..."
    docker push "${image}" \
      || error "docker push failed for ${name}"
    success "Built and pushed ${image} (local build)"
  }

  build_service_local "proxy"             "${REPO_ROOT}/src/proxy"
  build_service_local "llmlingua-sidecar" "${REPO_ROOT}/src/llmlingua-sidecar"
  build_service_local "doc-pipeline"      "${REPO_ROOT}/src/doc-pipeline"
  build_service_local "finetune-pipeline" "${REPO_ROOT}/src/finetune-pipeline"

  # routellm-sidecar is optional — only build if the directory exists
  if [[ -d "${REPO_ROOT}/src/routellm-sidecar" ]]; then
    build_service_local "routellm-sidecar" "${REPO_ROOT}/src/routellm-sidecar"
  else
    warn "src/routellm-sidecar not found — skipping routellm-sidecar build"
  fi

  # Build tika-sidecar only if directory exists (optional component for G03 doc extraction)
  if [[ -d "${REPO_ROOT}/src/tika-sidecar" ]]; then
    build_service_local "tika-sidecar" "${REPO_ROOT}/src/tika-sidecar"
  else
    warn "src/tika-sidecar not found — skipping tika-sidecar build (G03 doc extraction will use fallback)"
  fi
}

# ─── Step 5: Deploy Cloud Run services ───────────────────────────────────────
deploy_services() {
  info "Deploying Cloud Run services..."

  # Redis URL — prefer GCP Memorystore (auto-constructed from Terraform output)
  # Falls back to REDIS_URL from .env (e.g. Upstash for local dev or staging)
  REDIS_URL="${REDIS_URL:-}"
  [[ -z "$REDIS_URL" ]] && warn "REDIS_URL is empty — check that Terraform provisioned Redis and redis_host output is available."

  # Cloud SQL socket path (via built-in Cloud Run Cloud SQL connector)
  LANGFUSE_SECRET=$(gcloud secrets versions access latest \
    --secret="token-opt-db-password" --project="$PROJECT_ID" 2>/dev/null || echo "")
  if [[ -z "$LANGFUSE_SECRET" ]]; then
    warn "Could not read DB password from Secret Manager — DB_URL may be invalid"
  fi
  # Prisma (used by Langfuse v2) requires the host to be non-empty.
  # Cloud SQL Unix socket path must be set via DIRECT_URL or as host param with explicit value.
  # Database: the DEDICATED `langfuse` DB (google_sql_database.langfuse) — NOT the proxy's
  # token_opt. Pointing Langfuse at token_opt makes Prisma hit P3005 ("schema is not empty")
  # on first boot once the proxy schema migrations have run, and grafana's Langfuse
  # datasource already expects LANGFUSE_DB_NAME=langfuse.
  DB_URL="postgresql://token_opt_app:${LANGFUSE_SECRET}@localhost/langfuse?host=/cloudsql/${DB_CONNECTION}"

  # Deploy LLMLingua-2 sidecar (internal only, allow unauthenticated for proxy SA access)
  gcloud run deploy llmlingua-svc \
    --image="${REGISTRY_URL}/llmlingua-sidecar:latest" \
    --platform=managed \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$PROXY_SA" \
    --allow-unauthenticated \
    --memory=2Gi --cpu=2 \
    --cpu-boost \
    --max-instances=1 \
    --timeout=60 \
    --set-env-vars="LOG_LEVEL=INFO" \
    --quiet

  LLMLINGUA_URL=$(gcloud run services describe llmlingua-svc \
    --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")

  # Read RouteLLM model names from config.yaml (already in GCS; local copy at /tmp/config.yaml)
  ROUTELLM_STRONG=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('/tmp/config.yaml'))
print(cfg.get('groups',{}).get('G6_routing',{}).get('routellm',{}).get('strong_model',''))
" 2>/dev/null || echo "")
  ROUTELLM_WEAK=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('/tmp/config.yaml'))
print(cfg.get('groups',{}).get('G6_routing',{}).get('routellm',{}).get('weak_model',''))
" 2>/dev/null || echo "")

  [[ -z "$ROUTELLM_STRONG" ]] && { warn "G6_routing.routellm.strong_model not set — defaulting to gpt-4o"; ROUTELLM_STRONG="gpt-4o"; }
  [[ -z "$ROUTELLM_WEAK"   ]] && { warn "G6_routing.routellm.weak_model not set — defaulting to gpt-4o-mini"; ROUTELLM_WEAK="gpt-4o-mini"; }
  info "RouteLLM models: strong=${ROUTELLM_STRONG} weak=${ROUTELLM_WEAK}"

  # Deploy RouteLLM sidecar if it was built (internal only, allow unauthenticated for proxy SA access)
  ROUTELLM_URL=""
  if gcloud artifacts docker images describe "${REGISTRY_URL}/routellm-sidecar:latest" \
       --project="$PROJECT_ID" &>/dev/null 2>&1; then
    gcloud run deploy routellm-svc \
      --image="${REGISTRY_URL}/routellm-sidecar:latest" \
      --platform=managed \
      --region="$REGION" \
      --project="$PROJECT_ID" \
      --service-account="routellm-sidecar-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
      --ingress=internal \
      --no-allow-unauthenticated \
      --memory=2Gi --cpu=2 \
      --cpu-boost \
      --max-instances=1 \
      --timeout=60 \
      --set-env-vars="LOG_LEVEL=INFO,ROUTELLM_STRONG_MODEL=${ROUTELLM_STRONG},ROUTELLM_WEAK_MODEL=${ROUTELLM_WEAK}" \
      --set-secrets="OPENAI_API_KEY=routellm-openai-key:latest" \
      --quiet

    # Scrub the legacy allUsers invoker binding left by earlier deploys that used
    # --allow-unauthenticated. Harmless behind internal ingress today, but a landmine:
    # if anyone ever flips this service's ingress, allUsers would make it fully public.
    gcloud run services remove-iam-policy-binding routellm-svc \
      --region="$REGION" --project="$PROJECT_ID" \
      --member="allUsers" --role="roles/run.invoker" --quiet &>/dev/null || true

    ROUTELLM_URL=$(gcloud run services describe routellm-svc \
      --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")
    success "routellm-svc deployed: ${ROUTELLM_URL}"
  else
    warn "routellm-sidecar image not found — skipping routellm-svc deploy (G6 RouteLLM tier disabled)"
  fi

  # Ensure secret has a version — handles Terraform-pre-created shells (no version) and fresh creates
  ensure_secret_has_version() {
    local secret_name="$1" secret_value="$2"
    if ! timeout 30 gcloud secrets versions access latest --secret="$secret_name" --project="$PROJECT_ID" &>/dev/null; then
      if timeout 30 gcloud secrets describe "$secret_name" --project="$PROJECT_ID" &>/dev/null; then
        echo -n "$secret_value" | timeout 30 gcloud secrets versions add "$secret_name" \
          --data-file=- --project="$PROJECT_ID" &>/dev/null \
          || warn "Failed to add version to ${secret_name} — continuing"
      else
        echo -n "$secret_value" | timeout 30 gcloud secrets create "$secret_name" \
          --project="$PROJECT_ID" --replication-policy=automatic --data-file=- &>/dev/null \
          || warn "Failed to create ${secret_name} — continuing"
      fi
      success "Provisioned → ${secret_name}"
    fi
  }

  ensure_secret_has_version "langfuse-nextauth-secret" "$(openssl rand -hex 32)"
  ensure_secret_has_version "langfuse-salt"            "$(openssl rand -hex 32)"
  ensure_secret_has_version "grafana-admin-password"   "$(openssl rand -base64 12)"

  # Under least-privilege secret IAM (TF_VAR_least_privilege_secret_iam=true) the proxy SA
  # has NO project-wide secretAccessor — only per-secret bindings from Terraform's
  # proxy_secret_ids list. langfuse-salt is created HERE by this script (not Terraform),
  # so it cannot be in that list; bind it explicitly or langfuse-svc (which runs as the
  # proxy SA and mounts SALT=langfuse-salt) fails to deploy with "Permission denied on
  # secret". Idempotent; must run even when the secret already existed. No-op without
  # least-priv (the project-wide grant covers it).
  if [[ "${TF_VAR_least_privilege_secret_iam:-false}" == "true" && -n "${PROXY_SA:-}" ]]; then
    timeout 30 gcloud secrets add-iam-policy-binding "langfuse-salt" \
      --member="serviceAccount:${PROXY_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project="$PROJECT_ID" &>/dev/null \
      && success "least-priv: proxy SA granted accessor on langfuse-salt" \
      || warn "Could not bind proxy SA on langfuse-salt — langfuse-svc deploy may fail (grant manually: gcloud secrets add-iam-policy-binding langfuse-salt --member=serviceAccount:${PROXY_SA} --role=roles/secretmanager.secretAccessor)"
  fi

  # Issue an initial proxy API key for the 'admin' user if none exists yet.
  # Delegates to issue-key.sh which correctly sha256-hashes the raw key before storing.
  if ! timeout 30 gcloud secrets versions access latest --secret="token-proxy-api-keys" \
       --project="$PROJECT_ID" &>/dev/null; then
    info "No proxy API keys found — issuing initial admin key via issue-key.sh..."
    bash "${SCRIPT_DIR}/../issue-key.sh" issue --tenant admin --tier enterprise --admin --project "$PROJECT_ID" \
      || warn "Failed to issue proxy API key — run: scripts/issue-key.sh issue --tenant admin --tier enterprise --admin"
  else
    info "Proxy API keys already exist — skipping key issuance (use scripts/issue-key.sh to add/revoke)"
  fi

  # Deploy Langfuse FIRST to capture its real URL before deploying the proxy
  # Default: private (require authentication). Set LANGFUSE_UI_PUBLIC=1 in .env.gcp
  # to allow unauthenticated access (e.g. behind IAP or for pitch demos).
  if [[ "${LANGFUSE_UI_PUBLIC:-0}" == "1" ]]; then
    LF_AUTH_FLAG="--allow-unauthenticated"
  else
    LF_AUTH_FLAG="--no-allow-unauthenticated"
  fi
  gcloud run deploy langfuse-svc \
    --image="langfuse/langfuse:2" \
    --platform=managed \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$PROXY_SA" \
    --add-cloudsql-instances="${DB_CONNECTION}" \
    ${VPC_EGRESS_FLAGS} \
    ${LF_AUTH_FLAG} \
    --memory=1Gi --cpu=1 \
    --cpu-boost \
    --max-instances=1 \
    --port=3000 \
    --set-env-vars="DATABASE_URL=${DB_URL},NEXTAUTH_URL=https://placeholder.invalid" \
    --set-secrets="NEXTAUTH_SECRET=langfuse-nextauth-secret:latest,SALT=langfuse-salt:latest,LANGFUSE_DB_PASSWORD=token-opt-db-password:latest" \
    --quiet

  LANGFUSE_URL=$(gcloud run services describe langfuse-svc \
    --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")

  # Ensure QDRANT_URL is set
  [[ -z "$QDRANT_URL" ]] && error "QDRANT_URL is empty — ensure Qdrant service is deployed"

  # Pre-compute optional Langfuse secrets flag to avoid fragile subshell in gcloud args
  LANGFUSE_SECRETS_FLAG=""
  if timeout 30 gcloud secrets versions access latest --secret="langfuse-public-key" --project="$PROJECT_ID" &>/dev/null; then
    LANGFUSE_SECRETS_FLAG="--set-secrets=LANGFUSE_PUBLIC_KEY=langfuse-public-key:latest,LANGFUSE_SECRET_KEY=langfuse-secret-key:latest"
  fi

  # H2: inject the /metrics scrape token only if the secret exists (Terraform
  # creates token-opt-metrics-scrape-token). Without it, /metrics stays open.
  METRICS_SECRET_FLAG=""
  if timeout 30 gcloud secrets versions access latest --secret="token-opt-metrics-scrape-token" --project="$PROJECT_ID" &>/dev/null; then
    METRICS_SECRET_FLAG="--set-secrets=METRICS_SCRAPE_TOKEN=token-opt-metrics-scrape-token:latest"
  fi

  # AWS Bedrock SigV4 creds — mount only if populated (Bedrock is optional). litellm reads
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION_NAME from the environment.
  # Populate the secrets manually or via your secret pipeline; AWS_REGION_NAME is set below.
  AWS_SECRETS_FLAG=""
  if timeout 30 gcloud secrets versions access latest --secret="aws-access-key-id" --project="$PROJECT_ID" &>/dev/null; then
    AWS_SECRETS_FLAG="--set-secrets=AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest"
  fi

  # Qdrant app-layer API key (Terraform-generated `qdrant-api-key`): layer 2 behind
  # Cloud Run IAM. The proxy sends it as the `api-key` header on every Qdrant call
  # (plus a GCP identity token as `Authorization: Bearer` for the IAM layer — see
  # ml_models.qdrant_client_kwargs). Mounted only if the secret exists so older
  # provisions without the Terraform resource still deploy.
  QDRANT_KEY_SECRET_FLAG=""
  if timeout 30 gcloud secrets versions access latest --secret="qdrant-api-key" --project="$PROJECT_ID" &>/dev/null; then
    QDRANT_KEY_SECRET_FLAG="--set-secrets=QDRANT_API_KEY=qdrant-api-key:latest"
  fi

  # Deploy main proxy — LANGFUSE_URL now available
  gcloud run deploy token-proxy \
    --image="${REGISTRY_URL}/proxy:latest" \
    --platform=managed \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$PROXY_SA" \
    --add-cloudsql-instances="${DB_CONNECTION}" \
    ${VPC_EGRESS_FLAGS} \
    --allow-unauthenticated \
    --memory=4Gi --cpu=2 \
    --cpu-boost \
    --min-instances=0 --max-instances="${PROXY_MAX_INSTANCES:-1}" \
    --timeout=120 \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},\
CONFIG_GCS_BUCKET=${CONFIG_BUCKET},\
CONFIG_GCS_BLOB=config/config.yaml,\
REDIS_URL=${REDIS_URL},\
QDRANT_URL=${QDRANT_URL},\
GCP_REGION=${REGION},\
DOC_PIPELINE_JOB_NAME=doc-pipeline-job,\
FINETUNE_PIPELINE_JOB_NAME=finetune-pipeline-job,\
INGEST_REQUIRE_OIDC=true,\
INGEST_PUSH_SA_EMAIL=token-opt-ingest-push-sa@${PROJECT_ID}.iam.gserviceaccount.com,\
INGEST_OIDC_AUDIENCE=${PROXY_URL:-}/ingest-doc,\
DOC_PIPELINE_SA_EMAIL=${PROXY_SA},\
LANGFUSE_HOST=${LANGFUSE_URL},\
AWS_REGION_NAME=${AWS_REGION_NAME:-us-east-1},\
LOG_LEVEL=INFO" \
    --set-secrets="DB_PASSWORD=token-opt-db-password:latest" \
    ${LANGFUSE_SECRETS_FLAG} \
    ${METRICS_SECRET_FLAG} \
    ${AWS_SECRETS_FLAG} \
    ${QDRANT_KEY_SECRET_FLAG} \
    --quiet

  PROXY_URL=$(gcloud run services describe token-proxy \
    --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")

  # Patch the ingest OIDC audience now that the proxy URL is known (must equal the audience
  # Terraform sets on the Pub/Sub push subscription: <proxy_url>/ingest-doc).
  if [[ -n "$PROXY_URL" ]]; then
    gcloud run services update token-proxy \
      --region="$REGION" --project="$PROJECT_ID" \
      --update-env-vars="INGEST_OIDC_AUDIENCE=${PROXY_URL}/ingest-doc" \
      --quiet || warn "Could not patch INGEST_OIDC_AUDIENCE"
  fi

  # Patch Langfuse NEXTAUTH_URL to Langfuse's own public URL (required for OAuth session callbacks)
  gcloud run services update langfuse-svc \
    --region="$REGION" --project="$PROJECT_ID" \
    --update-env-vars="NEXTAUTH_URL=${LANGFUSE_URL}" \
    --quiet

  # Register Grafana OSS (skipped when self-hosted observability is disabled —
  # e.g. the commercial lean deploy uses Cloud Monitoring instead)
  GRAFANA_URL=""
  if [[ "${ENABLE_SELF_HOSTED_OBS:-true}" == "true" ]]; then
  gcloud run deploy grafana-svc \
    --image="grafana/grafana-oss:10.4.0" \
    --platform=managed \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$PROXY_SA" \
    --add-cloudsql-instances="${DB_CONNECTION}" \
    ${VPC_EGRESS_FLAGS} \
    --allow-unauthenticated \
    --memory=512Mi --cpu=1 \
    --cpu-boost \
    --max-instances=1 \
    --set-env-vars="GF_SERVER_HTTP_PORT=8080,\
GF_PATHS_PROVISIONING=//etc/grafana/provisioning,\
LANGFUSE_DB_NAME=langfuse,\
LANGFUSE_DB_USER=token_opt_app" \
    --set-secrets="GF_SECURITY_ADMIN_PASSWORD=grafana-admin-password:latest,LANGFUSE_DB_PASSWORD=token-opt-db-password:latest" \
    --quiet

  GRAFANA_URL=$(gcloud run services describe grafana-svc \
    --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")
  else
    info "Self-hosted observability disabled (ENABLE_SELF_HOSTED_OBS) — skipping Grafana"
  fi

  # Patch config.yaml with real sidecar URLs and re-upload to GCS.
  # We patch the GCS copy directly using gsutil + python — avoids clobbering the
  # carefully-tuned config/config.yaml on disk while still updating sidecar URLs.
  info "Patching config.yaml in GCS with real sidecar URLs..."

  # Deploy tika-sidecar if it was built (internal-only, used by doc-pipeline G03)
  TIKA_URL=""
  if gcloud artifacts docker images describe "${REGISTRY_URL}/tika-sidecar:latest" \
       --project="$PROJECT_ID" &>/dev/null 2>&1; then
    gcloud run deploy tika-svc \
      --image="${REGISTRY_URL}/tika-sidecar:latest" \
      --platform=managed \
      --region="$REGION" \
      --project="$PROJECT_ID" \
      --service-account="$PROXY_SA" \
      --allow-unauthenticated \
      --memory=1Gi --cpu=1 \
      --cpu-boost \
      --max-instances=1 \
      --timeout=60 \
      --port=9998 \
      --set-env-vars="LOG_LEVEL=INFO" \
      --quiet
    TIKA_URL=$(gcloud run services describe tika-svc \
      --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)")
    success "tika-svc deployed: ${TIKA_URL}"
  fi

  # Use python3 to patch YAML values directly — safe for any config.yaml structure
  python3 - <<PYEOF
import yaml, sys, os

path = '/tmp/config.yaml'
if not os.path.exists(path):
    sys.exit(0)

with open(path) as f:
    cfg = yaml.safe_load(f)

changed = False
groups = cfg.get('groups', {})

# G01 LLMLingua sidecar URL
llmlingua_url = '${LLMLINGUA_URL}'
if llmlingua_url:
    g1 = groups.get('G1_compression') or groups.get('g1_compression', {})
    if g1 and g1.get('sidecar_url', '').startswith('http://llmlingua-svc'):
        g1['sidecar_url'] = llmlingua_url
        changed = True

# G06 RouteLLM sidecar URL
routellm_url = '${ROUTELLM_URL}'
if routellm_url:
    g6 = groups.get('G6_routing') or groups.get('g6_routing', {})
    rl = g6.get('routellm', {})
    if rl and rl.get('url', '').startswith('http://routellm-svc'):
        rl['url'] = routellm_url
        changed = True

# G03 Tika sidecar URL
tika_url = '${TIKA_URL}'
if tika_url:
    g3 = groups.get('G3_doc_pipeline') or groups.get('g3_doc_pipeline', {})
    if g3 and g3.get('tika_url', '').startswith('http://tika-svc'):
        g3['tika_url'] = tika_url
        changed = True

if changed:
    with open(path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    print('Sidecar URLs patched in /tmp/config.yaml')
else:
    print('No placeholder sidecar URLs found — config.yaml already has real URLs or sidecars not configured')
PYEOF

  gsutil cp /tmp/config.yaml "gs://${CONFIG_BUCKET}/config/config.yaml" &>/dev/null
  success "config.yaml re-uploaded to GCS with sidecar URLs"

  rm -f /tmp/config.yaml
  success "All services deployed"
  echo ""
  echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║        Token Optimisation Proxy — DEPLOYED           ║${NC}"
  echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
  echo -e "${GREEN}║${NC} Proxy endpoint:  ${PROXY_URL}"
  GRAFANA_PASSWORD=$(gcloud secrets versions access latest --secret="grafana-admin-password" --project="$PROJECT_ID" 2>/dev/null || echo "<retrieval_failed>")
  echo -e "${GREEN}║${NC} Grafana:         ${GRAFANA_URL}  (admin/${GRAFANA_PASSWORD})"
  echo -e "${GREEN}║${NC} Langfuse:      ${LANGFUSE_URL}"
  if [[ "${LANGFUSE_UI_PUBLIC:-0}" != "1" ]]; then
    echo -e "${GREEN}║${NC}   (private — tunnel via: gcloud run services proxy langfuse-svc --region=${REGION})"
  fi
  echo -e "${GREEN}║${NC}"
  echo -e "${GREEN}║${NC} Developer integration (one-line change):"
  echo -e "${GREEN}║${NC}   base_url = ${PROXY_URL}/v1"
  echo -e "${GREEN}║${NC}   api_key  = <proxy-key>  # issue via: scripts/issue-key.sh"
  echo -e "${GREEN}║${NC}"
  echo -e "${GREEN}║${NC} G3 batch jobs (run on demand):"
  echo -e "${GREEN}║${NC}   gcloud run jobs execute doc-pipeline-job --region=${REGION} \\"
  echo -e "${GREEN}║${NC}     --update-env-vars=GCS_BUCKET=<bucket>,GCS_OBJECT=<path>"
  echo -e "${GREEN}║${NC}   gcloud run jobs execute finetune-pipeline-job --region=${REGION} \\"
  echo -e "${GREEN}║${NC}     --update-env-vars=DOMAIN=<domain>,PROVIDER=<openai|vertex_ai>"
  echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
  echo ""
  info "Docs: https://github.com/sumitdevgupto/TokenLean/blob/main/docs/developer-onboarding.md"
}

# ─── Step 5b: Deploy G3 Cloud Run Jobs (doc-pipeline, finetune-pipeline) ─────
deploy_jobs() {
  info "Deploying G3 Cloud Run Jobs (doc-pipeline, finetune-pipeline)..."

  gcloud run jobs deploy doc-pipeline-job \
    --image="${REGISTRY_URL}/doc-pipeline:latest" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$PROXY_SA" \
    --set-env-vars="QDRANT_URL=${QDRANT_URL},GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION}" \
    --max-retries=1 \
    --task-timeout=600 \
    --quiet
  success "doc-pipeline-job deployed"

  # REDIS_URL + GCS_BUCKET are required for a real finetune run (job-tracking + training
  # export); per-run TENANT_ID/QDRANT_COLLECTION/DOMAIN/TENANT_PROVIDER_KEY come as container
  # overrides from the trigger. Job name matches FINETUNE_PIPELINE_JOB_NAME on the proxy.
  gcloud run jobs deploy finetune-pipeline-job \
    --image="${REGISTRY_URL}/finetune-pipeline:latest" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --service-account="$PROXY_SA" \
    --set-env-vars="QDRANT_URL=${QDRANT_URL},REDIS_URL=${REDIS_URL},GCS_BUCKET=${CONFIG_BUCKET},GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION}" \
    --max-retries=1 \
    --task-timeout=1800 \
    --quiet
  success "finetune-pipeline-job deployed"
}

# ─── Step 6: Seed Qdrant with demo documents ────────────────────────────────
# Temporarily opens Qdrant ingress to all, seeds the rag_docs collection (named
# dense+sparse vectors, matching the doc-pipeline/G07 read path), then reverts to
# internal-only ingress. Requires sentence-transformers locally.
seed_qdrant() {
  if [[ "${ENABLE_QDRANT:-true}" != "true" ]]; then
    info "Qdrant disabled (ENABLE_QDRANT) — using pgvector; skipping Qdrant seed"
    return 0
  fi
  # WHY always seed on deploy:
  # Qdrant runs on Cloud Run with ephemeral (container) storage. Every new revision
  # (every gcp-deploy.sh run that pushes a new image) wipes the filesystem, so any
  # previously seeded data is gone. We must re-seed after every deploy.
  # We check first — if docs are already present (e.g. --skip-infra config-only run
  # with no image change), we skip the expensive open/close ingress cycle.

  info "Checking Qdrant rag_docs collection..."

  # Check python3 and sentence-transformers available
  if ! command -v python3 &>/dev/null; then
    warn "python3 not found — skipping Qdrant seeding. Run manually: ./scripts/seed-data.sh --qdrant-url <QDRANT_URL>"
    return 0
  fi
  if ! python3 -c "import sentence_transformers" &>/dev/null; then
    warn "sentence-transformers not installed — installing..."
    pip3 install sentence-transformers --quiet \
      || { warn "Install failed — skipping Qdrant seeding"; return 0; }
  fi

  # Get public URL
  local qdrant_public_url
  qdrant_public_url=$(gcloud run services describe token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")
  if [[ -z "$qdrant_public_url" ]]; then
    warn "Qdrant service not found — skipping seeding"
    return 0
  fi

  # Qdrant steady state is ingress=ALL + IAM (run.invoker) + an app-layer api-key —
  # NOT internal-only (internal ingress rejected even in-VPC Cloud Run callers using the
  # run.app URL under private-ranges-only egress, silently breaking G07/docs-chat).
  # For this off-VPC seeding window we add a TEMPORARY allUsers invoker (removed below);
  # during the window the app-layer api-key still gates every request.
  info "Opening Qdrant IAM temporarily for off-VPC seeding..."
  gcloud run services add-iam-policy-binding token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" \
    --member=allUsers --role=roles/run.invoker &>/dev/null || true
  sleep 10

  # App-layer key: required on every request once QDRANT__SERVICE__API_KEY is set.
  local qdrant_api_key
  qdrant_api_key=$(timeout 30 gcloud secrets versions access latest --secret="qdrant-api-key" \
    --project="$PROJECT_ID" 2>/dev/null || echo "")

  # Check if already seeded (rag_docs is the default/single-tenant collection the proxy reads)
  local points_count
  points_count=$(QDRANT_API_KEY="$qdrant_api_key" python3 -c "
import os, urllib.request, json
try:
    req = urllib.request.Request('${qdrant_public_url}/collections/rag_docs')
    key = os.getenv('QDRANT_API_KEY', '')
    if key:
        req.add_header('api-key', key)
    r = urllib.request.urlopen(req, timeout=10)
    d = json.loads(r.read())
    print(d.get('result', {}).get('points_count', 0))
except:
    print(0)
" 2>/dev/null || echo "0")

  if [[ "$points_count" -gt 0 ]] 2>/dev/null; then
    success "Qdrant rag_docs already has ${points_count} docs — skipping seed (no image change detected)"
  else
    info "Qdrant rag_docs is empty (new revision wipes storage) — seeding now..."
    QDRANT_API_KEY="$qdrant_api_key" "${REPO_ROOT}/scripts/seed-data.sh" --qdrant-url "$qdrant_public_url" \
      && success "Qdrant seeded successfully" \
      || warn "Qdrant seeding failed — run manually: QDRANT_API_KEY=<qdrant-api-key secret> ./scripts/seed-data.sh --qdrant-url ${qdrant_public_url}"
  fi

  # Always remove the temporary allUsers grant (ingress stays ALL — the steady state;
  # IAM + the app-layer key are the two standing gates).
  info "Removing temporary Qdrant allUsers IAM grant..."
  gcloud run services remove-iam-policy-binding token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" \
    --member=allUsers --role=roles/run.invoker &>/dev/null \
    || warn "Could not remove Qdrant allUsers grant — run manually: gcloud run services remove-iam-policy-binding token-opt-qdrant --member=allUsers --role=roles/run.invoker --region=${REGION}"
  success "Qdrant IAM restored (invoker-only, ingress stays ALL + api-key)"
}

# ─── Step 7: Patch Prometheus/Alertmanager with real service URLs ────────────
# W4 fix: proxy_service_url and alertmanager_url are only known after Cloud Run
# deploy. This step writes them back into terraform.tfvars and re-applies only
# the two resources that consume them (prometheus config secret + alertmanager
# config secret), then restarts those Cloud Run services to pick up the change.
patch_prometheus() {
  if [[ "${ENABLE_SELF_HOSTED_OBS:-true}" != "true" ]]; then
    info "Self-hosted observability disabled (ENABLE_SELF_HOSTED_OBS) — using Cloud Monitoring; skipping Prometheus patch"
    return 0
  fi
  info "Patching Prometheus/Alertmanager config with real service URLs..."

  local tfvars_file="${REPO_ROOT}/infra/terraform.tfvars"

  # Resolve Alertmanager URL
  local alertmanager_url
  alertmanager_url=$(gcloud run services describe token-opt-alertmanager \
    --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)" 2>/dev/null || echo "")

  if [[ -z "$PROXY_URL" ]] || [[ -z "$alertmanager_url" ]]; then
    warn "patch_prometheus: PROXY_URL or alertmanager URL not available — skipping Prometheus patch."
    warn "  Re-run with --skip-infra after a successful first deploy to apply Prometheus config."
    return 0
  fi

  # Update (or append) proxy_service_url in terraform.tfvars
  if grep -q '^proxy_service_url' "$tfvars_file" 2>/dev/null; then
    sed -i "s|^proxy_service_url.*|proxy_service_url = \"${PROXY_URL}\"|" "$tfvars_file"
  else
    echo "proxy_service_url = \"${PROXY_URL}\"" >> "$tfvars_file"
  fi

  # Update (or append) alertmanager_url in terraform.tfvars
  if grep -q '^alertmanager_url' "$tfvars_file" 2>/dev/null; then
    sed -i "s|^alertmanager_url.*|alertmanager_url = \"${alertmanager_url}\"|" "$tfvars_file"
  else
    echo "alertmanager_url = \"${alertmanager_url}\"" >> "$tfvars_file"
  fi

  success "terraform.tfvars updated:"
  info "  proxy_service_url = ${PROXY_URL}"
  info "  alertmanager_url  = ${alertmanager_url}"

  # Re-apply secret versions so Terraform state reflects the new content
  cd "${REPO_ROOT}/infra"
  terraform apply -auto-approve \
    -target=google_secret_manager_secret_version.prometheus_config \
    -target=google_secret_manager_secret_version.alertmanager_config \
    2>&1 | tail -5
  cd "${REPO_ROOT}"

  # Force new Cloud Run revisions so the services mount the updated secret versions.
  # Terraform won't redeploy them automatically when only secret content changes.
  gcloud run services update token-opt-prometheus \
    --region="$REGION" --project="$PROJECT_ID" \
    --update-env-vars="PROMETHEUS_RELOAD_TS=$(date +%s)" \
    --quiet
  gcloud run services update token-opt-alertmanager \
    --region="$REGION" --project="$PROJECT_ID" \
    --update-env-vars="ALERTMANAGER_RELOAD_TS=$(date +%s)" \
    --quiet

  success "Prometheus and Alertmanager updated with real service URLs"
}

# ─── Load Terraform outputs if --skip-infra ───────────────────────────────────
load_infra_outputs() {
  info "Loading infrastructure outputs from Terraform state..."
  local tf_bucket
  # Check .env.gcp first (preferred), then .env as fallback
  tf_bucket=$(grep 'TF_STATE_BUCKET' "${REPO_ROOT}/.env.gcp" 2>/dev/null | cut -d= -f2 | tr -d '"' || \
              grep 'TF_STATE_BUCKET' "${REPO_ROOT}/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "")
  cd "${REPO_ROOT}/infra"
  if [[ -n "$tf_bucket" ]]; then
    terraform init -upgrade -backend-config="bucket=${tf_bucket}" &>/dev/null || true
  else
    terraform init -upgrade &>/dev/null || true
  fi
  REGISTRY_URL=$(terraform output -raw artifact_registry_url 2>/dev/null) || error "Run without --skip-infra first to create infrastructure"
  CONFIG_BUCKET=$(terraform output -raw config_bucket_name)
  DB_CONNECTION=$(terraform output -raw db_instance_connection_name)
  PROXY_SA=$(terraform output -raw proxy_service_account_email)
  QDRANT_URL=$(terraform output -raw qdrant_service_url 2>/dev/null || echo "")
  REDIS_HOST=$(terraform output -raw redis_host 2>/dev/null || echo "")
  if [[ -n "$REDIS_HOST" ]]; then
    REDIS_URL="redis://${REDIS_HOST}:6379/0"
    success "Redis URL constructed from Terraform output: ${REDIS_URL}"
  fi
  cd "${REPO_ROOT}"
  success "Infrastructure outputs loaded"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
  echo -e "${BLUE}"
  echo "╔══════════════════════════════════════════════════════╗"
  echo "║   TokenLean — Token Optimisation Framework           ║"
  echo "║   GCP Deploy                                         ║"
  echo "╚══════════════════════════════════════════════════════╝"
  echo -e "${NC}"

  check_prereqs

  if [[ "$SKIP_INFRA" == "true" ]]; then
    warn "--skip-infra: skipping Terraform, loading existing outputs"
    load_infra_outputs
  else
    provision_infra
    # Private-IP path: the Terraform local-exec schema migrations are count=0
    # (the off-VPC host cannot reach the private instance), so apply them from an
    # in-VPC Cloud Run Job now that the instance/db/user/password-secret exist.
    # Public-IP path: local-exec already ran during `terraform apply` — no-op here.
    if [[ "${PRIVATE_SQL:-false}" == "true" ]]; then
      info "Applying Cloud SQL schema migrations via in-VPC Cloud Run Job (private_cloud_sql)..."
      bash "${SCRIPT_DIR}/run-migrations-job.sh" --project "$PROJECT_ID" --region "$REGION" \
        || error "In-VPC schema migration job failed — see logs above."
    fi
  fi

  # Always upload config — ensures GCS stays in sync whether or not Terraform ran
  upload_config

  # Always call provision_llm_keys — it internally gates the TENANT-SERVING platform
  # provider keys (llm-key-*) on SKIP_PLATFORM_KEYS (set by the commercial deploy under
  # strict BYOK: every tenant supplies its own key, so no platform key that could answer
  # a tenant request may exist in the project) but unconditionally seeds the RouteLLM
  # embeddings key + Langfuse keys, which are INFRA credentials, not a tenant-answer path.
  # A prior version skipped this whole call under SKIP_PLATFORM_KEYS=true, which also
  # silently skipped those two infra-only seedings on every strict-BYOK GCP deploy.
  provision_llm_keys

  prepare_config_yaml
  if [[ "$SKIP_BUILD" != "true" ]]; then
    validate_templates
    build_and_push
  else
    info "--skip-build: skipping image build (images already pushed by Cloud Build)"
  fi
  deploy_services
  deploy_jobs
  seed_qdrant
  patch_prometheus

  # Optional post-deploy steps (opt-in). PROXY_URL is set by deploy_services.
  if [[ "$RUN_PROMPTFOO" == "true" ]]; then run_promptfoo_eval; fi
  if [[ "$RUN_DSPY" == "true" ]]; then run_dspy_optimize; fi
}

main "$@"
