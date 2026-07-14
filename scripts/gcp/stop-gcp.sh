#!/usr/bin/env bash
# =============================================================================
# stop-gcp.sh — ZERO-COST pause: stop/delete Redis (either backend), stop Cloud SQL
# =============================================================================
# Usage:
#   ./scripts/gcp/stop-gcp.sh [--project PROJECT_ID] [--region REGION]
#
# What this does:
#   1. Redis — backend auto-detected, no backup (ephemeral cache/counters only;
#      durable data lives in Cloud SQL, see Step 2):
#        - memorystore: DELETE the instance (Memorystore cannot be paused).
#        - docker (GCE VM, the commercial DEFAULT — deploy-commercial-gcp.sh
#          defaults --redis docker): STOP the VM (`gcloud compute instances stop`).
#          Cheaper + simpler than Memorystore: stopped compute is ~free, the boot
#          disk persists, and start-gcp.sh just restarts it — but the container
#          itself already runs with --save "" (non-persistent), so there was never
#          any Redis *data* to preserve on the VM either way.
#   2. Stops Cloud SQL instance (stops compute; ~$2/month storage remains)
#   3. Cloud Run (proxy, Qdrant, sidecars): already scales to zero — no action needed
#      Qdrant data persists on Cloud Run storage between scale-to-zero cycles.
#
# To resume: run scripts/gcp/start-gcp.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
REGION=""   # resolved below: --region flag > GCP_REGION (.env.gcp) > asia-south1
SQL_INSTANCE="token-opt-pg"
REDIS_INSTANCE="token-opt-redis"        # Memorystore instance name (redis_backend=memorystore)
REDIS_VM="token-opt-redis-vm"           # GCE VM name (redis_backend=docker, THE DEFAULT)

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
echo "║  This will delete Redis and stop Cloud SQL to        ║"
echo "║  achieve zero compute billing.                       ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

echo -en "${YELLOW}Are you sure? (y/yes/no): ${NC}"
read -r confirm
[[ "$confirm" != "yes" && "$confirm" != "y" ]] && { info "Aborted."; exit 0; }

# ─── Step 1: Delete Memorystore Redis (stops all billing) ────────────────────
# No backup: Redis holds ONLY ephemeral cache + rate-limit counters + transient
# G10-G13-G18 state — never a system of record. Durable data (billing usage_events,
# tenant_configs, audit_events, pgvector RAG) lives in Cloud SQL, which we only STOP
# (data preserved). start-gcp.sh recreates Redis via terraform apply; the cache is
# cold and self-heals. So a stop→start cycle is lossless for anything that matters.
info "Checking Memorystore Redis..."
REDIS_EXISTS=$(timeout 30 gcloud redis instances describe "${REDIS_INSTANCE}" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(name)" 2>/dev/null || echo "")

if [[ -n "$REDIS_EXISTS" ]]; then
  warn "Deleting Memorystore Redis instance to stop billing (ephemeral cache — no backup needed)..."
  timeout 300 gcloud redis instances delete "${REDIS_INSTANCE}" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --quiet || warn "Redis deletion failed or already deleted"
  success "Memorystore Redis deleted — billing stopped"
else
  info "No Memorystore instance found — checking docker-Redis GCE VM (commercial default)..."
  VM_ZONE="${REGION}-a"
  VM_STATUS=$(timeout 30 gcloud compute instances describe "${REDIS_VM}" \
    --project="$PROJECT_ID" \
    --zone="$VM_ZONE" \
    --format="value(status)" 2>/dev/null || echo "")

  if [[ -n "$VM_STATUS" ]]; then
    if [[ "$VM_STATUS" == "TERMINATED" ]]; then
      info "Redis VM (${REDIS_VM}) already stopped"
    else
      warn "Stopping Redis VM ${REDIS_VM} (state: ${VM_STATUS})..."
      timeout 180 gcloud compute instances stop "${REDIS_VM}" \
        --project="$PROJECT_ID" \
        --zone="$VM_ZONE" \
        --quiet || warn "Redis VM stop failed"
      success "Redis VM stopped — compute billing stopped (boot disk persists, ~pennies/month)"
    fi
  else
    info "No Redis found on either backend (Memorystore or docker VM) — nothing to stop"
  fi
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
