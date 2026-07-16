#!/usr/bin/env bash
# =============================================================================
# pre-deploy-check.sh — Comprehensive pre-deployment validation (GCP)
# =============================================================================
# Usage:
#   ./scripts/gcp/pre-deploy-check.sh [--project PROJECT_ID] [--region REGION]
#
# LOCAL DEPENDENCIES:
#   1. Required CLI tools (gcloud, docker, terraform)
#   2. Python with PyYAML
#   3. redis-cli (optional, for backup/restore)
#   4. Docker daemon running and can access Google registries
#   5. Network connectivity to GCP
#
# GCP CONFIGURATION:
#   6. GCP authentication and project access
#   7. Billing enabled for project
#   8. Required IAM roles granted
#   9. Required APIs enabled (or will be auto-enabled)
#
# PROJECT FILES:
#   10. terraform.tfvars exists and configured
#   11. config/keys.yaml exists with real API keys
#   12. Required config files present
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
REGION=""   # resolved below: --region flag > GCP_REGION (.env.gcp) > asia-south1

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ─── Load .env file if exists ───────────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  info "Loading environment variables from .env..."
  set -a
  source "$ENV_FILE"
  set +a
  success "Loaded .env file"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    *) error "Unknown option: $1" ;;
  esac
done

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║   TokenLean — Token Optimisation Framework           ║"
echo "║   Pre-Deployment                                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

ERRORS=0
WARNINGS=0

# ─── Check 1: Required CLI tools ─────────────────────────────────────────────
info "Checking required CLI tools..."
for cmd in gcloud docker terraform; do
  if command -v "$cmd" &>/dev/null; then
    success "$cmd installed"
  else
    error "$cmd is required but not installed"
    ERRORS=$((ERRORS + 1))
  fi
done
# Check Python (python or python3)
if command -v python3 &>/dev/null; then
  success "python3 installed"
elif command -v python &>/dev/null; then
  success "python installed"
else
  error "Python is required but not installed"
  ERRORS=$((ERRORS + 1))
fi

# ─── Check 2: GCP authentication ────────────────────────────────────────────
info "Checking GCP authentication..."
if gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q "@"; then
  ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
  success "Authenticated as: ${ACCOUNT}"
else
  error "Not authenticated. Run: gcloud auth login"
  ERRORS=$((ERRORS + 1))
fi

# ─── Check 2b: Application Default Credentials ───────────────────────────────
# Terraform's google provider AND the Cloud SQL Auth Proxy (used by the schema
# migrations, host-side on the public path / in the Cloud Run Job on the private
# path) authenticate with ADC — which is SEPARATE from `gcloud auth login`. A
# missing ADC otherwise surfaces only mid-`terraform apply` as a cryptic proxy
# auth error.
info "Checking Application Default Credentials (ADC)..."
if gcloud auth application-default print-access-token &>/dev/null; then
  success "ADC present (Terraform + Cloud SQL Auth Proxy can authenticate)"
else
  error "ADC not set. Run: gcloud auth application-default login"
  ERRORS=$((ERRORS + 1))
fi

# ─── Check 3: GCP project ──────────────────────────────────────────────────
info "Checking GCP project configuration..."
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="${GCP_PROJECT_ID:-}"
fi
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null || echo "")
fi

# ─── Check region ───────────────────────────────────────────────────────────
if [[ -z "$REGION" ]]; then
  REGION="${GCP_REGION:-asia-south1}"
fi

if [[ -z "$PROJECT_ID" ]]; then
  error "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"
  ERRORS=$((ERRORS + 1))
else
  success "Project ID: ${PROJECT_ID}"
  
  # Verify project access
  if gcloud projects describe "$PROJECT_ID" &>/dev/null; then
    success "Project accessible"
  else
    error "Project ${PROJECT_ID} not accessible or does not exist"
    ERRORS=$((ERRORS + 1))
  fi
fi

# ─── Check 4: Required IAM roles ────────────────────────────────────────────
info "Checking required IAM roles for current user..."
REQUIRED_ROLES=(
  "roles/editor"
  "roles/cloudsql.admin"
  "roles/storage.admin"
  "roles/secretmanager.admin"
  "roles/run.admin"
  "roles/cloudbuild.builds.builder"
  "roles/serviceusage.serviceUsageAdmin"
  "roles/artifactregistry.admin"
)

ACCOUNT_EMAIL=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null || echo "")
if [[ -n "$ACCOUNT_EMAIL" ]]; then
  MISSING_ROLES=()
  for role in "${REQUIRED_ROLES[@]}"; do
    if gcloud projects get-iam-policy "$PROJECT_ID" \
      --filter="bindings.members:user:${ACCOUNT_EMAIL}" \
      --format="value(bindings.role)" 2>/dev/null | grep -q "$role"; then
      : # Role present
    else
      MISSING_ROLES+=("$role")
    fi
  done
  
  if [[ ${#MISSING_ROLES[@]} -eq 0 ]]; then
    success "All required IAM roles present"
  else
    warn "Missing IAM roles (may cause deployment failures):"
    for role in "${MISSING_ROLES[@]}"; do
      warn "  - $role"
    done
    WARNINGS=$((WARNINGS + 1))
  fi
fi

# ─── Check 4b: GCP Billing Enabled ──────────────────────────────────────────
info "Checking GCP billing status..."
if gcloud billing projects describe "$PROJECT_ID" --format="value(billingEnabled)" 2>/dev/null | grep -q "True"; then
  success "Billing enabled for project"
else
  error "Billing NOT enabled for project ${PROJECT_ID}"
  error "Enable billing at: https://console.cloud.google.com/billing"
  ERRORS=$((ERRORS + 1))
fi

# ─── Check 4c: Required GCP APIs Enabled ────────────────────────────────────
info "Checking required GCP APIs..."
REQUIRED_APIS=(
  "run.googleapis.com"
  "sqladmin.googleapis.com"
  "redis.googleapis.com"
  "storage.googleapis.com"
  "secretmanager.googleapis.com"
  "cloudtasks.googleapis.com"
  "artifactregistry.googleapis.com"
  "cloudbuild.googleapis.com"
  "vpcaccess.googleapis.com"
  "servicenetworking.googleapis.com"
)

DISABLED_APIS=()
for api in "${REQUIRED_APIS[@]}"; do
  API_STATE=$(gcloud services list --project="$PROJECT_ID" --enabled 2>/dev/null | grep "^${api}" || echo "")
  if [[ -z "$API_STATE" ]]; then
    DISABLED_APIS+=("$api")
  fi
done

if [[ ${#DISABLED_APIS[@]} -eq 0 ]]; then
  success "All required GCP APIs are enabled"
else
  warn "Some GCP APIs are not enabled (will be auto-enabled by Terraform, but may delay deployment):"
  for api in "${DISABLED_APIS[@]}"; do
    warn "  - $api"
  done
  warn "To enable manually: gcloud services enable ${DISABLED_APIS[0]} --project=${PROJECT_ID}"
  WARNINGS=$((WARNINGS + 1))
fi

# ─── Check 5: terraform.tfvars ───────────────────────────────────────────────
info "Checking terraform.tfvars..."
TFVARS_FILE="${REPO_ROOT}/infra/terraform.tfvars"

if [[ ! -f "$TFVARS_FILE" ]]; then
  error "terraform.tfvars not found. Copy infra/terraform.tfvars.template → infra/terraform.tfvars"
  ERRORS=$((ERRORS + 1))
else
  success "terraform.tfvars exists"
  
  # Check for placeholder project_id
  TF_PROJECT_ID=$(grep "^project_id" "$TFVARS_FILE" | sed 's/.*"\(.*\)".*/\1/' || echo "")
  if [[ "$TF_PROJECT_ID" == "YOUR_GCP_PROJECT_ID" ]] || [[ -z "$TF_PROJECT_ID" ]]; then
    error "terraform.tfvars has placeholder project_id. Set it to: ${PROJECT_ID}"
    ERRORS=$((ERRORS + 1))
  elif [[ "$TF_PROJECT_ID" != "$PROJECT_ID" ]]; then
    warn "terraform.tfvars project_id (${TF_PROJECT_ID}) differs from gcloud config (${PROJECT_ID})"
    WARNINGS=$((WARNINGS + 1))
  else
    success "project_id correctly set: ${TF_PROJECT_ID}"
  fi
  
  # Check region
  TF_REGION=$(grep "^region" "$TFVARS_FILE" | sed 's/.*"\(.*\)".*/\1/' || echo "")
  if [[ -n "$TF_REGION" ]]; then
    success "region set: ${TF_REGION}"
  fi
fi

# ─── Check 6: config/keys.yaml ───────────────────────────────────────────────
info "Checking config/keys.yaml..."
KEYS_FILE="${REPO_ROOT}/config/keys.yaml"

if [[ ! -f "$KEYS_FILE" ]]; then
  error "config/keys.yaml not found. Copy config/keys.yaml.template → config/keys.yaml"
  ERRORS=$((ERRORS + 1))
else
  success "config/keys.yaml exists"
  
  # Check for placeholder keys — ignore commented lines
  if grep -vE "^\s*#" "$KEYS_FILE" | grep -qE 'sk-\.\.\.|"\\.\\.\\."'; then
    warn "Some LLM keys still have placeholder values in config/keys.yaml"
    WARNINGS=$((WARNINGS + 1))
  else
    success "No placeholder keys detected"
  fi
  
  # Check required key fields (ignore commented-out lines)
  REQUIRED_KEYS=("openai" "anthropic" "google" "mistral" "routellm")
  for key in "${REQUIRED_KEYS[@]}"; do
    if grep -vE "^\s*#" "$KEYS_FILE" | grep -q "^\s*${key}:"; then
      success "Key field present: ${key}"
    else
      warn "Key field missing or commented out: ${key}"
      WARNINGS=$((WARNINGS + 1))
    fi
  done
fi

# ─── Check 7: Required config files ─────────────────────────────────────────
info "Checking required config files..."
REQUIRED_CONFIGS=(
  "config/config.yaml.template"
  "config/bypass-rules.yaml"
  "config/tool-registry.yaml"
)

for config in "${REQUIRED_CONFIGS[@]}"; do
  if [[ -f "${REPO_ROOT}/${config}" ]]; then
    success "${config} exists"
  else
    error "${config} not found"
    ERRORS=$((ERRORS + 1))
  fi
done

# ─── Check 7b: config/config.yaml (the live config uploaded to GCS) ──────────
info "Checking config/config.yaml (live config, not just template)..."
if [[ -f "${REPO_ROOT}/config/config.yaml" ]]; then
  success "config/config.yaml exists"
else
  warn "config/config.yaml not found — deploy will generate from template (G22/G23 tuning may differ)"
  WARNINGS=$((WARNINGS + 1))
fi

# ─── Check 7c: adaptive_bypass_rules.yaml ────────────────────────────────────
info "Checking config/adaptive_bypass_rules.yaml..."
if [[ -f "${REPO_ROOT}/config/adaptive_bypass_rules.yaml" ]]; then
  success "adaptive_bypass_rules.yaml exists"
else
  warn "adaptive_bypass_rules.yaml not found — G24 will run with no bypass rules in GCP"
  WARNINGS=$((WARNINGS + 1))
fi

# ─── Check 8: Docker daemon and permissions ─────────────────────────────────
info "Checking Docker daemon..."
if docker info &>/dev/null; then
  success "Docker daemon running"

  # Verify --platform flag support (requires Docker >= 20.10)
  if docker build --platform=linux/amd64 --help &>/dev/null 2>&1; then
    success "Docker supports --platform=linux/amd64"
  else
    error "Docker does not support --platform flag — upgrade Docker Desktop to >= 20.10"
    ERRORS=$((ERRORS + 1))
  fi

  # Check Docker can pull from Google Artifact Registry
  info "Checking Docker Artifact Registry access..."
  if docker pull gcr.io/cloud-builders/gcloud:latest &>/dev/null; then
    success "Docker can pull from Google registries"
  else
    warn "Docker may have issues pulling from Google Artifact Registry"
    warn "Run 'gcloud auth configure-docker' if pushes fail during deployment"
    WARNINGS=$((WARNINGS + 1))
  fi
else
  error "Docker daemon not running. Start Docker Desktop or Docker service"
  ERRORS=$((ERRORS + 1))
fi

# ─── Check 8b: Network connectivity to GCP ────────────────────────────────────
info "Checking network connectivity to GCP..."
if curl -s -o /dev/null -w "%{http_code}" https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID} --max-time 5 2>/dev/null | grep -q "200\|401\|403"; then
  success "Can reach GCP APIs"
else
  warn "Cannot reach GCP APIs (network issue or firewall blocking)"
  warn "Check internet connection and proxy settings"
  WARNINGS=$((WARNINGS + 1))
fi

# ─── Check 9: Python dependencies ───────────────────────────────────────────
info "Checking Python dependencies..."
if python3 -c "import yaml" &>/dev/null; then
  success "PyYAML installed"
else
  warn "PyYAML not installed. Run: pip install pyyaml"
  WARNINGS=$((WARNINGS + 1))
fi

# ─── Check 10: redis-cli (needed for backup/restore) ──────────────────────────
info "Checking redis-cli (for Redis backup/restore)..."
if command -v redis-cli &>/dev/null; then
  success "redis-cli installed"
else
  warn "redis-cli not installed. Install Redis tools for full backup/restore:"
  warn "  macOS: brew install redis"
  warn "  Ubuntu/Debian: sudo apt-get install redis-tools"
  warn "  Or download from: https://redis.io/download"
  WARNINGS=$((WARNINGS + 1))
fi

# ─── Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}══════════════════════════════════════════════════════${NC}"
if [[ $ERRORS -eq 0 ]]; then
  echo -e "${GREEN}✓ VALIDATION PASSED${NC}"
  echo -e "${GREEN}  Errors: 0${NC}"
  echo -e "${GREEN}  Warnings: ${WARNINGS}${NC}"
  echo ""
  echo -e "${GREEN}You can proceed with deployment:${NC}"
  echo -e "${GREEN}  ./scripts/gcp/gcp-deploy.sh --project ${PROJECT_ID} --region ${REGION}${NC}"
  exit 0
else
  echo -e "${RED}✗ VALIDATION FAILED${NC}"
  echo -e "${RED}  Errors: ${ERRORS}${NC}"
  echo -e "${YELLOW}  Warnings: ${WARNINGS}${NC}"
  echo ""
  echo -e "${RED}Please fix the errors above before running deployment.${NC}"
  exit 1
fi
