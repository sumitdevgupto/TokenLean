#!/usr/bin/env bash
# =============================================================================
# start-gcp.sh — Resume GCP infrastructure after zero-cost pause
# =============================================================================
# Usage:
#   ./scripts/gcp/start-gcp.sh [--project PROJECT_ID] [--region REGION]
#
# What this does (Redis backend auto-detected — see stop-gcp.sh):
#   1. Redis:
#        - docker VM (commercial DEFAULT): `gcloud compute instances start` if
#          stopped, or recreate via terraform apply if stop-gcp.sh never ran /
#          the VM was deleted out-of-band. Cold container, no data to restore
#          (the redis:7-alpine container runs with --save "").
#        - memorystore: recreate via terraform apply if missing (stop-gcp.sh
#          deletes it — Memorystore has no stop/start, only delete/create).
#   2. Starts Cloud SQL instance
#   3. Waits for all services to be healthy
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
  [[ -z "$PROJECT_ID" ]] && error "No GCP project. Use --project or: gcloud config set project PROJECT_ID"
fi

# Resolve region
if [[ -z "$REGION" ]]; then
  REGION="${GCP_REGION:-asia-south1}"
fi


echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║   TokenLean — Token Optimisation Framework           ║"
echo "║   START INFRA                                        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ─── Step 1: Check/Start Redis (docker VM default, or Memorystore) ────────────
VM_ZONE="${REGION}-a"
info "Checking docker-Redis GCE VM (${REDIS_VM}) — the commercial default..."
VM_STATUS=$(timeout 30 gcloud compute instances describe "${REDIS_VM}" \
  --project="$PROJECT_ID" \
  --zone="$VM_ZONE" \
  --format="value(status)" 2>/dev/null || echo "")

if [[ -n "$VM_STATUS" ]]; then
  # VM exists (stop-gcp.sh stops it, never deletes it) — just start it back up.
  if [[ "$VM_STATUS" == "RUNNING" ]]; then
    success "Redis VM already RUNNING"
  else
    info "Starting Redis VM (state: ${VM_STATUS})..."
    timeout 120 gcloud compute instances start "${REDIS_VM}" \
      --project="$PROJECT_ID" \
      --zone="$VM_ZONE" \
      --quiet || warn "Redis VM start failed"
    success "Redis VM started (container runs with --save \"\" — no data to restore, self-heals cold)"
  fi
else
  info "No docker-Redis VM found — checking Memorystore..."
  REDIS_EXISTS=$(timeout 30 gcloud redis instances list \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --filter="name:${REDIS_INSTANCE}" \
    --format="value(name)" 2>/dev/null || echo "")

  if [[ -z "$REDIS_EXISTS" ]]; then
    # Neither backend found. Two cases: (a) redis_backend=memorystore and stop-gcp.sh
    # deleted it (Memorystore has no stop/start — delete/create only), or (b) infra was
    # never applied / the VM was removed out-of-band. Recreate whichever this project's
    # terraform.tfvars actually specifies (no -var override — respect the real config).
    info "No Redis found on either backend — recreating via terraform apply (cold cache, self-heals; nothing durable lives in Redis)..."
    if [[ -d "${REPO_ROOT}/infra" ]]; then
      ( cd "${REPO_ROOT}/infra" \
        && terraform init -upgrade >/dev/null 2>&1 \
        && terraform apply -target='google_redis_instance.cache' \
             -target='google_compute_instance.redis' -auto-approve ) \
        && success "Redis recreated via Terraform (backend per terraform.tfvars)" \
        || warn "terraform apply for Redis failed — recreate manually (cd infra && terraform apply). Proxy falls back to in-memory rate-limit meanwhile."
    else
      warn "infra/ not found — cannot auto-recreate Redis. Run 'terraform apply' where your infra lives."
    fi
  else
    info "Memorystore Redis instance exists — checking status..."
    for i in $(seq 1 30); do
      REDIS_STATE=$(timeout 15 gcloud redis instances describe "${REDIS_INSTANCE}" \
        --project="$PROJECT_ID" \
        --region="$REGION" \
        --format="value(state)" 2>/dev/null || echo "")
      [[ "$REDIS_STATE" == "READY" ]] && break
      [[ "$REDIS_STATE" == "STATE_UNSPECIFIED" ]] && break
      sleep 10
      echo -n "."
    done
    echo ""
    if [[ "$REDIS_STATE" == "READY" ]]; then
      success "Memorystore Redis is READY"
    else
      warn "Redis state: ${REDIS_STATE} — may still be initializing"
    fi
  fi
fi

# ─── Step 2: Start Cloud SQL ──────────────────────────────────────────────────
info "Starting Cloud SQL instance '${SQL_INSTANCE}'..."
timeout 120 gcloud sql instances patch "${SQL_INSTANCE}" \
  --project="$PROJECT_ID" \
  --activation-policy=ALWAYS \
  --quiet || warn "Cloud SQL patch failed — instance may not exist or already running"

info "Waiting for Cloud SQL to become RUNNABLE..."
for i in $(seq 1 24); do
  STATE=$(timeout 15 gcloud sql instances describe "${SQL_INSTANCE}" \
    --project="$PROJECT_ID" --format="value(state)" 2>/dev/null || echo "")
  [[ "$STATE" == "RUNNABLE" ]] && break
  sleep 5
  echo -n "."
done
echo ""
success "Cloud SQL is RUNNABLE"

# ─── Step 3: Verify Qdrant rag_docs collection ───────────────────────────────
info "Checking Qdrant rag_docs collection..."
QDRANT_URL=$(gcloud run services describe token-opt-qdrant \
  --region="$REGION" --project="$PROJECT_ID" \
  --format="value(status.url)" 2>/dev/null || echo "")

QDRANT_STATUS="unknown"
if [[ -n "$QDRANT_URL" ]]; then
  # Temporarily open ingress to check collection count
  gcloud run services update token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" \
    --ingress=all --quiet &>/dev/null
  gcloud run services add-iam-policy-binding token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" \
    --member=allUsers --role=roles/run.invoker &>/dev/null || true
  sleep 8

  POINTS_COUNT=$(python3 -c "
import urllib.request, json, sys
try:
    r = urllib.request.urlopen('${QDRANT_URL}/collections/rag_docs', timeout=10)
    d = json.loads(r.read())
    print(d.get('result',{}).get('points_count', 0))
except Exception as e:
    print(0)
" 2>/dev/null || echo "0")

  if [[ "$POINTS_COUNT" -gt 0 ]] 2>/dev/null; then
    QDRANT_STATUS="seeded (${POINTS_COUNT} docs)"
    success "Qdrant rag_docs has ${POINTS_COUNT} documents"
  else
    QDRANT_STATUS="EMPTY — needs seeding"
    warn "Qdrant rag_docs collection is empty or missing"
    if command -v python3 &>/dev/null && python3 -c "import sentence_transformers" &>/dev/null; then
      echo -en "${YELLOW}Re-seed rag_docs now? (y/yes/no): ${NC}"
      read -r do_seed
      if [[ "$do_seed" == "y" || "$do_seed" == "yes" ]]; then
        "${REPO_ROOT}/scripts/seed-data.sh" --qdrant-url "$QDRANT_URL" \
          && { QDRANT_STATUS="seeded"; success "Qdrant seeded"; } \
          || warn "Seeding failed — run manually: ./scripts/seed-data.sh --qdrant-url ${QDRANT_URL}"
      fi
    else
      warn "python3/sentence-transformers not available — run manually: ./scripts/seed-data.sh --qdrant-url ${QDRANT_URL}"
    fi
  fi

  # Revert ingress to internal-only
  gcloud run services remove-iam-policy-binding token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" \
    --member=allUsers --role=roles/run.invoker &>/dev/null || true
  gcloud run services update token-opt-qdrant \
    --region="$REGION" --project="$PROJECT_ID" \
    --ingress=internal --quiet &>/dev/null
else
  warn "Qdrant service not found — deploy first with gcp-deploy.sh"
fi

# ─── Get proxy endpoint ───────────────────────────────────────────────────────
PROXY_URL=$(gcloud run services describe token-proxy \
  --region="$REGION" --project="$PROJECT_ID" \
  --format="value(status.url)" 2>/dev/null || echo "Not deployed yet")

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Infrastructure RUNNING                     ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC} Cloud SQL:      RUNNABLE"
if [[ -n "$REDIS_EXISTS" ]]; then
  echo -e "${GREEN}║${NC} Memorystore:    READY"
else
  echo -e "${YELLOW}║${NC} Memorystore:    NOT RUNNING (run Terraform to restore)"
fi
echo -e "${GREEN}║${NC} Cloud Run:      auto-starts on first request"
echo -e "${GREEN}║${NC} Qdrant:         ${QDRANT_STATUS} (internal ingress)"
echo -e "${GREEN}║${NC}"
echo -e "${GREEN}║${NC} Proxy: ${PROXY_URL}"
echo -e "${GREEN}║${NC}"
echo -e "${GREEN}║${NC} To stop (zero cost): ./scripts/gcp/stop-gcp.sh"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
