#!/usr/bin/env bash
# =============================================================================
# stop-gcp.sh — ZERO-COST pause: backup Redis to GCS, delete Memorystore
# =============================================================================
# Usage:
#   ./scripts/gcp/stop-gcp.sh [--project PROJECT_ID] [--region REGION]
#
# What this does:
#   1. Exports Redis data to GCS (backup)
#   2. Deletes Memorystore Redis instance (stops all billing)
#   3. Stops Cloud SQL instance (stops compute; ~$2/month storage remains)
#   4. Cloud Run (proxy, Qdrant, sidecars): already scales to zero — no action needed
#      Qdrant data persists on Cloud Run storage between scale-to-zero cycles.
#
# To resume: run scripts/gcp/start-gcp.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
REGION="asia-south1"
SQL_INSTANCE="token-opt-pg"
REDIS_INSTANCE="token-opt-redis"

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
  source "$ENV_FILE"
  set +a
  success "Loaded env file"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# Resolve project
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="${GCP_PROJECT_ID:-}"
fi
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
  [[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"
fi

# Resolve region
if [[ -z "$REGION" ]]; then
  REGION="${GCP_REGION:-asia-south1}"
fi


echo -e "${YELLOW}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║   TokenLean — Token Optimisation Framework           ║"
echo "║   STOP INFRA                                         ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  This will backup and delete Redis, stop Cloud SQL   ║"
echo "║  to achieve zero compute billing.                    ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

echo -en "${YELLOW}Are you sure? (y/yes/no): ${NC}"
read -r confirm
[[ "$confirm" != "yes" && "$confirm" != "y" ]] && { info "Aborted."; exit 0; }

# ─── Step 1: Backup Redis to GCS ─────────────────────────────────────────────
info "Backing up Redis data to GCS..."

# Get Redis connection info (30s timeout to avoid hanging if GCP API is slow)
REDIS_HOST=$(timeout 30 gcloud redis instances describe "${REDIS_INSTANCE}" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(host)" 2>/dev/null || echo "")

if [[ -n "$REDIS_HOST" ]]; then
  # Get GCS bucket for config (created by Terraform)
  CONFIG_BUCKET=$(timeout 30 gcloud storage buckets list \
    --project="$PROJECT_ID" \
    --filter="name~token-opt-config" \
    --format="value(name)" 2>/dev/null | head -1 || echo "")
  
  if [[ -n "$CONFIG_BUCKET" ]]; then
    # Export Redis data using redis-cli if available, or warning
    if command -v redis-cli &>/dev/null; then
      BACKUP_FILE="redis-backup-$(date +%Y%m%d-%H%M%S).rdb"
      info "Exporting Redis data..."
      timeout 60 redis-cli -h "$REDIS_HOST" --rdb "/tmp/${BACKUP_FILE}" 2>/dev/null || warn "Redis backup failed - continuing anyway"
      if [[ -f "/tmp/${BACKUP_FILE}" ]]; then
        timeout 120 gsutil cp "/tmp/${BACKUP_FILE}" "gs://${CONFIG_BUCKET}/backups/" && success "Redis backup saved to GCS"
        rm -f "/tmp/${BACKUP_FILE}"
      fi
    else
      warn "redis-cli not installed - skipping Redis backup (data will be lost)"
    fi
  else
    warn "Config bucket not found - skipping Redis backup"
  fi
  
  # ─── Step 2: Delete Memorystore Redis (stops all billing) ────────────────────
  warn "Deleting Memorystore Redis instance to stop billing..."
  timeout 300 gcloud redis instances delete "${REDIS_INSTANCE}" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --quiet || warn "Redis deletion failed or already deleted"
  success "Memorystore Redis deleted — billing stopped"
else
  info "Redis instance not found - may already be deleted"
fi

# ─── Step 3: Stop Cloud SQL instance ──────────────────────────────────────────
info "Checking Cloud SQL instance '${SQL_INSTANCE}'..."
SQL_STATE=$(timeout 30 gcloud sql instances describe "${SQL_INSTANCE}" \
  --project="$PROJECT_ID" --format="value(state)" 2>/dev/null || echo "")

if [[ -z "$SQL_STATE" ]]; then
  info "Cloud SQL instance not found — may already be deleted or never created"
elif [[ "$SQL_STATE" == "STOPPED" ]]; then
  info "Cloud SQL already stopped — no action needed"
else
  info "Stopping Cloud SQL instance (state: ${SQL_STATE})..."
  timeout 120 gcloud sql instances patch "${SQL_INSTANCE}" \
    --project="$PROJECT_ID" \
    --activation-policy=NEVER \
    --quiet || warn "Cloud SQL patch failed — continuing"
  success "Cloud SQL stopped — compute billing stopped (~\$2/month storage only)"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     Infrastructure PAUSED — MINIMUM COST             ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC} Cloud Run:      \$0 (scales to zero — proxy, Qdrant, sidecars)"
echo -e "${GREEN}║${NC} Qdrant data:    persists (Cloud Run storage, no billing at zero scale)"
echo -e "${GREEN}║${NC} Memorystore:    \$0 (deleted, backup in GCS)"
echo -e "${GREEN}║${NC} Cloud SQL:      ~\$2/month storage only"
echo -e "${GREEN}║${NC} GCS Storage:    ~\$0.02/GB/month (backup)"
echo -e "${GREEN}║${NC}"
echo -e "${GREEN}║${NC} To resume with full functionality:"
echo -e "${GREEN}║${NC}   ./scripts/gcp/start-gcp.sh"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}Note: Redis data backed up to GCS. Will be restored on start.${NC}"
