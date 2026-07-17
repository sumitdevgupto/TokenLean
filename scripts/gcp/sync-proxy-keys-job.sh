#!/usr/bin/env bash
# =============================================================================
# sync-proxy-keys-job.sh — upsert local-keys.json into the Cloud SQL proxy_keys
#                          table from an in-VPC Cloud Run Job (private-IP support)
# =============================================================================
# WHY: the commercial proxy runs PROXY_KEYS_BACKEND=postgres and validates proxy
# API keys against Cloud SQL's proxy_keys table. The Postgres backend only ingests
# the GCS key blob ONCE (import_blob_store_once() is guarded on an empty table), so
# harness/tenant keys minted AFTER the first deploy never reach proxy_keys and every
# such request 401s. Cloud SQL is private-IP-only on the commercial default, so the
# off-VPC deploy host cannot upsert directly.
#
# This runs infra/migrations/sync_proxy_keys.py from a Cloud Run Job that lives IN
# the VPC (Direct VPC egress), which CAN reach the private instance. The job connects
# over the Cloud SQL Auth Proxy socket (/cloudsql/<conn>) mounted by
# --set-cloudsql-instances — works for BOTH public and private instances. The upsert
# is idempotent (INSERT ... ON CONFLICT DO UPDATE), so re-runs are safe.
#
# It is invoked by scripts/commercial/deploy-commercial-gcp.sh in the PTP block after
# harness keys are minted. It can also be run standalone to re-sync keys.
#
# Usage:
#   ./scripts/gcp/sync-proxy-keys-job.sh [--project ID] [--region REGION] [--keys-file PATH]
#
# Reads .env.gcp / .env (like the other gcp scripts) for GCP_PROJECT_ID / GCP_REGION.
# Fails LOUDLY if the sync errors.
# =============================================================================
set -euo pipefail
# ── Host-shell guard ────────────────────────────────────────────────────────────
# GCP deploy-family operations must run from WSL Ubuntu / Linux / Cloud Shell — NEVER
# Git Bash or any Windows shell (Terraform local-exec via cmd.exe, psql/docker host
# tooling, CRLF-corrupted env sourcing). Abort up front with the fix. (Read-only checks
# like check-gcp-status.sh / post-deploy-check.sh are intentionally NOT guarded — they
# work from any shell.)
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "ERROR: run this from WSL Ubuntu (or Linux/Cloud Shell), NOT Git Bash/Windows." >&2
    echo "  In WSL:  cd /mnt/d/token-optimisation && bash $0 $*" >&2
    exit 2 ;;
esac

PROJECT_ID=""
REGION=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
KEYS_FILE="${REPO_ROOT}/config/local-keys.json"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Load .env.gcp / .env ─────────────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env.gcp"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  # Strip CRLF on the fly: env files edited on Windows carry \r, which bash would
  # append to every value (e.g. GCP_REGION=asia-south1$'\r') and corrupt gcloud args.
  set -a; source <(tr -d '\r' < "$ENV_FILE"); set +a
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)   PROJECT_ID="$2"; shift 2 ;;
    --region)    REGION="$2";     shift 2 ;;
    --keys-file) KEYS_FILE="$2";  shift 2 ;;
    --help) sed -n '/^# Usage:/,/^# ===/p' "$0"; exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

[[ -n "$PROJECT_ID" ]] || PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$PROJECT_ID" ]] || error "No GCP project set. Use --project or set GCP_PROJECT_ID."
[[ -n "$REGION" ]] || REGION="${GCP_REGION:-asia-south1}"

[[ -f "$KEYS_FILE" ]] || error "keys file not found: $KEYS_FILE (pass --keys-file)"

for cmd in gcloud docker terraform; do
  command -v "$cmd" &>/dev/null || error "$cmd is required but not installed."
done

# ─── Resolve deploy inputs from Terraform outputs ─────────────────────────────
cd "${REPO_ROOT}/infra"
REGISTRY_URL=$(terraform output -raw artifact_registry_url 2>/dev/null) \
  || error "could not read artifact_registry_url from Terraform — has infra been provisioned (terraform apply)?"
DB_CONNECTION=$(terraform output -raw db_instance_connection_name 2>/dev/null) \
  || error "could not read db_instance_connection_name from Terraform."
SYNC_SA=$(terraform output -raw proxy_service_account_email 2>/dev/null) \
  || error "could not read proxy_service_account_email from Terraform."
cd "${REPO_ROOT}"

# DB user + database: mirror infra/migrations/run.sh verbatim (token_opt_app / token_opt).
PGUSER="token_opt_app"
PGDATABASE="token_opt"

SYNC_IMAGE="${REGISTRY_URL}/sync-proxy-keys:latest"
JOB_NAME="sync-proxy-keys-job"
BUILD_DIR="${REPO_ROOT}/infra/migrations"
STAGED_KEYS="${BUILD_DIR}/local-keys.json"

info "Key-sync job: project=${PROJECT_ID} region=${REGION}"
info "  db=${DB_CONNECTION}  keys=${KEYS_FILE}  image=${SYNC_IMAGE}"

# ─── 1. Build + push the tiny asyncpg key-sync image ──────────────────────────
# The keys file holds key hashes — stage it into the build context ONLY for the
# `docker build` and remove it immediately (a trap guarantees cleanup even on error,
# so it never lingers in the repo working tree).
cleanup_staged() { rm -f "$STAGED_KEYS" 2>/dev/null || true; }
trap cleanup_staged EXIT

cp "$KEYS_FILE" "$STAGED_KEYS" || error "could not stage keys file into build context"

info "Building key-sync image (python:3.11-slim + asyncpg + sync_proxy_keys.py)..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build --platform=linux/amd64 \
  -f "${BUILD_DIR}/Dockerfile.synckeys" \
  -t "$SYNC_IMAGE" \
  "$BUILD_DIR" \
  || error "key-sync image build failed"
docker push "$SYNC_IMAGE" || error "key-sync image push failed"
cleanup_staged   # remove the staged keys file the moment the image no longer needs it
success "Key-sync image pushed"

# ─── 2. Deploy + execute the in-VPC Cloud Run Job ─────────────────────────────
# --network/--subnet/--vpc-egress: run inside the VPC so the private-IP Cloud SQL
#   instance is reachable (Direct VPC egress). Same pattern as run-migrations-job.sh.
# --set-cloudsql-instances: mount the Cloud SQL Auth Proxy socket at /cloudsql/<conn>;
#   works for public AND private instances (no private IP address needed).
# --set-secrets PGPASSWORD: the DB password from Secret Manager, never inlined.
# --max-retries=1: the upsert is idempotent (ON CONFLICT DO UPDATE), so a re-run is safe.
info "Deploying key-sync Cloud Run Job (${JOB_NAME})..."
gcloud run jobs deploy "$JOB_NAME" \
  --image="$SYNC_IMAGE" \
  --region="$REGION" --project="$PROJECT_ID" \
  --service-account="$SYNC_SA" \
  --network=default --subnet=default --vpc-egress=private-ranges-only \
  --set-cloudsql-instances="$DB_CONNECTION" \
  --set-secrets="PGPASSWORD=token-opt-db-password:latest" \
  --set-env-vars="DB_CONNECTION_NAME=${DB_CONNECTION},PGUSER=${PGUSER},PGDATABASE=${PGDATABASE},KEYS_JSON_PATH=/app/local-keys.json" \
  --max-retries=1 --task-timeout=600 --quiet \
  || error "key-sync job deploy failed"

info "Executing key-sync job (waiting for completion)..."
# Execute ASYNC + self-poll, NOT `execute --wait`: the gcloud --wait CLI has repeatedly WEDGED
# (spins "0/1 complete" long after the task finished server-side). Poll succeededCount/
# failedCount ourselves with a hard wall-clock cap so a stuck CLI can never hang the deploy.
EXEC_NAME="$(gcloud run jobs execute "$JOB_NAME" \
  --region="$REGION" --project="$PROJECT_ID" --format="value(metadata.name)" 2>/dev/null || true)"
[[ -n "$EXEC_NAME" ]] || error "could not start key-sync job execution — check IAM / job deploy."

SYNC_OK=false
for _i in $(seq 1 40); do   # 40 × 10s = ~6.5 min cap (task-timeout is 600s)
  ST="$(gcloud run jobs executions describe "$EXEC_NAME" --region="$REGION" --project="$PROJECT_ID" \
          --format="value(status.succeededCount,status.failedCount)" 2>/dev/null || echo "")"
  case "$ST" in
    1*)  SYNC_OK=true; break ;;                     # succeededCount>=1
    *1)  break ;;                                   # failedCount>=1 → fail fast
  esac
  sleep 10
done
$SYNC_OK || error "KEY-SYNC JOB FAILED or timed out (execution ${EXEC_NAME}) — inspect logs: gcloud run jobs executions logs read ${EXEC_NAME} --region=${REGION} --project=${PROJECT_ID}"

success "Proxy keys synced into proxy_keys via in-VPC Cloud Run Job"
