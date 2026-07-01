#!/bin/bash
# =============================================================================
# teardown-gcp.sh — Complete GCP teardown for Docker migration
# =============================================================================
# Usage: ./scripts/gcp/teardown-gcp.sh [--project PROJECT_ID] [--region REGION]
#
# Deletes all GCP resources created by gcp-deploy.sh:
#   - Cloud SQL instance
#   - Memorystore Redis
#   - All Cloud Run services
#   - Artifact Registry images (optional)
#
# Keeps GCS bucket for config backups (minimal cost)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
REGION="asia-south1"
SQL_INSTANCE="token-opt-pg"
REDIS_INSTANCE="token-opt-redis"
DELETE_IMAGES=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

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
      echo "  --project ID       GCP project ID (default: current gcloud config)"
      echo "  --region REGION    GCP region (default: asia-south1)"
      echo "  --delete-images    Also delete Docker images from Artifact Registry"
      echo "  --help             Show this help"
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Resolve project ──────────────────────────────────────────────────────────
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
  [[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"
fi

echo -e "${RED}"
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   ⚠️  TEARDOWN: DELETE ALL GCP RESOURCES ⚠️                  ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║  This will PERMANENTLY DELETE:                                 ║"
echo "║    • Cloud SQL database (token-opt-pg)                        ║"
echo "║    • Memorystore Redis (token-opt-redis)                      ║"
echo "║    • All Cloud Run services (9 services)                      ║"
echo "║                                                                ║"
echo "║  ★ GCS bucket with backups will be KEPT (minimal cost)        ║"
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
info "Step 1/4: Deleting Cloud Run services..."

SERVICES=(
  "token-proxy"
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

# ─── Step 2: Delete Cloud SQL ─────────────────────────────────────────────────
echo ""
info "Step 2/4: Deleting Cloud SQL..."

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

# ─── Step 3: Delete Memorystore Redis ───────────────────────────────────────
echo ""
info "Step 3/4: Deleting Memorystore Redis..."

if gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud redis instances delete "${REDIS_INSTANCE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Redis" || warn "  Failed to delete Redis"
else
  success "  Redis not found (already deleted)"
fi

# ─── Step 4: Delete Artifact Registry Images (Optional) ────────────────────────
if [[ "$DELETE_IMAGES" == true ]]; then
  echo ""
  info "Step 4/4: Deleting Artifact Registry images..."
  
  REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/token-opt"
  
  for image in proxy llmlingua-sidecar doc-pipeline routellm-sidecar tika-sidecar; do
    info "  → Deleting ${image}..."
    gcloud artifacts docker images delete "${REGISTRY}/${image}" \
      --delete-tags \
      --quiet 2>/dev/null && success "    Deleted ${image}" || warn "    Failed or not found: ${image}"
  done
else
  echo ""
  info "Step 4/4: Skipping Artifact Registry (use --delete-images to remove)"
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
RUNNING_REDIS=$(gcloud redis instances list --region="${REGION}" --project="${PROJECT_ID}" --filter="name:${REDIS_INSTANCE}" --format="value(name)" 2>/dev/null || echo "")
RUNNING_SERVICES=$(gcloud run services list --region="${REGION}" --project="${PROJECT_ID}" --format="value(SERVICE)" 2>/dev/null | wc -l || echo "0")

[[ -z "$RUNNING_SQL" ]] && echo -e "${GREEN}✅ Cloud SQL: Deleted${NC}" || echo -e "${RED}❌ Cloud SQL: Still exists${NC}"
[[ -z "$RUNNING_REDIS" ]] && echo -e "${GREEN}✅ Redis: Deleted${NC}" || echo -e "${RED}❌ Redis: Still exists${NC}"
[[ "$RUNNING_SERVICES" -eq 0 ]] && echo -e "${GREEN}✅ Cloud Run: All services deleted${NC}" || echo -e "${RED}❌ Cloud Run: $RUNNING_SERVICES services still exist${NC}"
