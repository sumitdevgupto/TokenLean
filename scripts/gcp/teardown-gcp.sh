#!/bin/bash
# =============================================================================
# teardown-gcp.sh — Complete GCP teardown (base + commercial)
# =============================================================================
# Usage: ./scripts/gcp/teardown-gcp.sh [--project PROJECT_ID] [--region REGION]
#
# Deletes all GCP resources created by gcp-deploy.sh AND deploy-commercial-gcp.sh:
#   - Cloud SQL instance
#   - Redis — BOTH backends (docker GCE VM, the commercial DEFAULT; Memorystore, opt-in)
#   - All Cloud Run services, incl. portal-svc (commercial)
#   - Cloud Run Jobs (doc-pipeline, finetune-pipeline, docs-seed)
#   - KMS key ring (if BYOK hardening was enabled) — key itself has prevent_destroy,
#     so this only removes the ring binding/ring if empty; see Step 5 note
#   - Artifact Registry images (optional)
#
# Keeps GCS bucket for config backups + Secret Manager secrets (minimal cost)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
REGION=""   # resolved below: --region flag > GCP_REGION (.env.gcp) > asia-south1
SQL_INSTANCE="token-opt-pg"
REDIS_INSTANCE="token-opt-redis"        # Memorystore instance name (redis_backend=memorystore)
REDIS_VM="token-opt-redis-vm"           # GCE VM name (redis_backend=docker, THE DEFAULT)
DELETE_IMAGES=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Load .env.gcp (fallback .env) — SAME source of GCP_PROJECT_ID / GCP_REGION as
#     the deploy scripts, so a teardown without flags targets the SAME project/region
#     the stack was deployed into (else it silently defaults to asia-south1 and misses
#     resources in another region). ─────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env.gcp"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)  PROJECT_ID="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    --delete-images) DELETE_IMAGES=true; shift ;;
    --help)
      echo "Usage: ./scripts/gcp/teardown-gcp.sh [--project PROJECT_ID] [--region REGION] [--delete-images]"
      echo ""
      echo "Options:"
      echo "  --project ID       GCP project ID (default: GCP_PROJECT_ID in .env.gcp, else gcloud config)"
      echo "  --region REGION    GCP region (default: GCP_REGION in .env.gcp, else asia-south1)"
      echo "  --delete-images    Also delete Docker images from Artifact Registry"
      echo "  --help             Show this help"
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Resolve project (flag > GCP_PROJECT_ID > gcloud config) ──────────────────
[[ -z "$PROJECT_ID" ]] && PROJECT_ID="${GCP_PROJECT_ID:-}"
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
  [[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project, set GCP_PROJECT_ID in .env.gcp, or: gcloud config set project PROJECT_ID"
fi
# ─── Resolve region (flag > GCP_REGION > asia-south1) ─────────────────────────
[[ -z "$REGION" ]] && REGION="${GCP_REGION:-asia-south1}"

echo -e "${RED}"
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   ⚠️  TEARDOWN: DELETE ALL GCP RESOURCES ⚠️                  ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║  This will PERMANENTLY DELETE:                                 ║"
echo "║    • Cloud SQL database (token-opt-pg)                        ║"
echo "║    • Redis — docker VM or Memorystore, whichever exists       ║"
echo "║    • All Cloud Run services (incl. portal-svc)                ║"
echo "║    • Cloud Run Jobs (doc-pipeline, finetune, docs-seed)        ║"
echo "║                                                                ║"
echo "║  ★ GCS bucket, Secret Manager, KMS key material KEPT           ║"
echo "║    (minimal cost; KMS key has prevent_destroy — see script)   ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "Project: ${PROJECT_ID}"
echo "Region:  ${REGION}"
echo ""

# ─── Confirm ──────────────────────────────────────────────────────────────────
echo -en "${YELLOW}Are you ABSOLUTELY SURE? Type 'destroy' to confirm: ${NC}"
read -r confirm
[[ "$confirm" != "destroy" ]] && { info "Aborted."; exit 0; }

# ─── Step 1: Delete Cloud Run Services ───────────────────────────────────────
echo ""
info "Step 1/5: Deleting Cloud Run services..."

SERVICES=(
  "token-proxy"
  "portal-svc"
  "llmlingua-svc"
  "routellm-svc"
  "langfuse-svc"
  "grafana-svc"
  "tika-svc"
  "token-opt-qdrant"
  "token-opt-prometheus"
  "token-opt-alertmanager"
)

for service in "${SERVICES[@]}"; do
  info "  → Deleting ${service}..."
  if gcloud run services describe "${service}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud run services delete "${service}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --quiet 2>/dev/null && success "    Deleted ${service}" || warn "    Failed to delete ${service}"
  else
    success "    ${service} not found (already deleted)"
  fi
done

# ─── Step 1b: Delete Cloud Run Jobs ───────────────────────────────────────────
info "Step 1b/5: Deleting Cloud Run Jobs..."

JOBS=("doc-pipeline-job" "finetune-pipeline-job" "docs-seed-job")
for job in "${JOBS[@]}"; do
  info "  → Deleting ${job}..."
  if gcloud run jobs describe "${job}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud run jobs delete "${job}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --quiet 2>/dev/null && success "    Deleted ${job}" || warn "    Failed to delete ${job}"
  else
    success "    ${job} not found (already deleted)"
  fi
done

# ─── Step 2: Delete Cloud SQL ─────────────────────────────────────────────────
echo ""
info "Step 2/5: Deleting Cloud SQL..."

if gcloud sql instances describe "${SQL_INSTANCE}" --project="${PROJECT_ID}" &>/dev/null; then
  info "  → Disabling deletion protection..."
  gcloud sql instances patch "${SQL_INSTANCE}" \
    --no-deletion-protection \
    --project="${PROJECT_ID}" \
    --quiet 2>/dev/null || warn "    Could not disable protection (may already be disabled)"

  info "  → Deleting SQL instance..."
  gcloud sql instances delete "${SQL_INSTANCE}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Cloud SQL" || warn "  Failed to delete Cloud SQL"
else
  success "  Cloud SQL not found (already deleted)"
fi

# ─── Step 3: Delete Redis — BOTH backends (docker VM is the commercial default) ─
echo ""
info "Step 3/5: Deleting Redis (docker VM + Memorystore, whichever exists)..."

VM_ZONE="${REGION}-a"
if gcloud compute instances describe "${REDIS_VM}" --zone="${VM_ZONE}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud compute instances delete "${REDIS_VM}" \
    --zone="${VM_ZONE}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Redis VM (${REDIS_VM})" || warn "  Failed to delete Redis VM"
else
  success "  Redis VM not found (already deleted, or Memorystore backend in use)"
fi

if gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud redis instances delete "${REDIS_INSTANCE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Memorystore Redis" || warn "  Failed to delete Memorystore Redis"
else
  success "  Memorystore Redis not found (already deleted, or docker VM backend in use)"
fi

# ─── Step 3b: KMS — BYOK master-key envelope (if hardening was enabled) ────────
# The crypto key itself has `prevent_destroy = true` in Terraform (losing it makes
# every stored tenant provider key permanently unrecoverable) — teardown deliberately
# does NOT delete google_kms_crypto_key.master_key. This step only reports its
# presence so it's not mistaken for an orphaned resource; delete it explicitly and
# knowingly via `terraform destroy` (after removing prevent_destroy) if truly needed.
echo ""
info "Step 3c/5: Checking KMS BYOK key ring (not deleted — see note)..."
if gcloud kms keys list --location="${REGION}" --keyring=token-opt-byok --project="${PROJECT_ID}" &>/dev/null; then
  warn "  KMS key ring 'token-opt-byok' present — KEPT intentionally (prevent_destroy on the crypto key; deleting it makes all stored BYOK provider keys unrecoverable). Minimal cost (~\$0.06/key-version/month)."
else
  success "  No KMS key ring found (hardening was not enabled, or already removed)"
fi

# ─── Step 4: Delete Artifact Registry Images (Optional) ────────────────────────
if [[ "$DELETE_IMAGES" == true ]]; then
  echo ""
  info "Step 5/5: Deleting Artifact Registry images..."

  REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/token-opt"

  for image in proxy proxy-commercial portal llmlingua-sidecar doc-pipeline finetune-pipeline routellm-sidecar tika-sidecar; do
    info "  → Deleting ${image}..."
    gcloud artifacts docker images delete "${REGISTRY}/${image}" \
      --delete-tags \
      --quiet 2>/dev/null && success "    Deleted ${image}" || warn "    Failed or not found: ${image}"
  done
else
  echo ""
  info "Step 5/5: Skipping Artifact Registry (use --delete-images to remove)"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   TEARDOWN COMPLETE                                            ║${NC}"
echo -e "${GREEN}╠════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC} Remaining GCP resources (minimal cost):                        ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   • GCS bucket (config backups)                               ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   • Artifact Registry images (optional)                       ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   • Secret Manager secrets (API keys)                         ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                               ${GREEN}║${NC}"
echo -e "${GREEN}║${NC} Next steps:                                                   ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   1. Deploy Docker locally: docker-compose up                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   2. Or deploy to cheap VPS (Hetzner, etc.)                   ${GREEN}║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Verify ───────────────────────────────────────────────────────────────────
echo "Verification:"
echo "------------"
RUNNING_SQL=$(gcloud sql instances list --project="${PROJECT_ID}" --filter="name:${SQL_INSTANCE}" --format="value(name)" 2>/dev/null || echo "")
RUNNING_REDIS_MS=$(gcloud redis instances list --region="${REGION}" --project="${PROJECT_ID}" --filter="name:${REDIS_INSTANCE}" --format="value(name)" 2>/dev/null || echo "")
RUNNING_REDIS_VM=$(gcloud compute instances list --project="${PROJECT_ID}" --filter="name:${REDIS_VM}" --format="value(name)" 2>/dev/null || echo "")
RUNNING_SERVICES=$(gcloud run services list --region="${REGION}" --project="${PROJECT_ID}" --format="value(SERVICE)" 2>/dev/null | wc -l || echo "0")
RUNNING_JOBS=$(gcloud run jobs list --region="${REGION}" --project="${PROJECT_ID}" --format="value(JOB)" 2>/dev/null | wc -l || echo "0")

[[ -z "$RUNNING_SQL" ]] && echo -e "${GREEN}✅ Cloud SQL: Deleted${NC}" || echo -e "${RED}❌ Cloud SQL: Still exists${NC}"
[[ -z "$RUNNING_REDIS_MS" && -z "$RUNNING_REDIS_VM" ]] && echo -e "${GREEN}✅ Redis (both backends): Deleted${NC}" || echo -e "${RED}❌ Redis: Still exists (Memorystore=${RUNNING_REDIS_MS:-none} VM=${RUNNING_REDIS_VM:-none})${NC}"
[[ "$RUNNING_SERVICES" -eq 0 ]] && echo -e "${GREEN}✅ Cloud Run services: All deleted${NC}" || echo -e "${RED}❌ Cloud Run services: $RUNNING_SERVICES still exist${NC}"
[[ "$RUNNING_JOBS" -eq 0 ]] && echo -e "${GREEN}✅ Cloud Run jobs: All deleted${NC}" || echo -e "${RED}❌ Cloud Run jobs: $RUNNING_JOBS still exist${NC}"
