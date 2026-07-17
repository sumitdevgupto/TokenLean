#!/usr/bin/env bash
# =============================================================================
# run-migrations-job.sh — apply Cloud SQL schema migrations from an in-VPC
#                         Cloud Run Job (private-IP Cloud SQL support)
# =============================================================================
# WHY: the 5 schema migrations (billing / tenant_configs / audit_events / rls /
# pgvector) historically ran as Terraform local-exec provisioners that tunnel
# cloud-sql-proxy + psql from the deploy host. That FAILS when Cloud SQL is
# private-IP-only (private_cloud_sql=true — the commercial default): the off-VPC
# laptop/WSL host cannot reach the instance's private 10.x IP, so even
# `cloud-sql-proxy --private-ip` times out (dial tcp 10.x:3307: i/o timeout).
#
# This script runs the SAME .sql files (infra/migrations/*.sql — byte-identical
# to the old heredocs) from a Cloud Run Job that lives IN the VPC (Direct VPC
# egress), which CAN reach the private instance. The job connects over the Cloud
# SQL Auth Proxy socket (/cloudsql/<conn>) mounted by --set-cloudsql-instances,
# so it works for BOTH public and private instances and needs no private IP.
#
# It is invoked by scripts/gcp/gcp-deploy.sh after `terraform apply` (once the
# instance + db + user + password secret exist) on the private-IP path. It can
# also be run standalone for a re-migration.
#
# Usage:
#   ./scripts/gcp/run-migrations-job.sh [--project ID] [--region REGION]
#
# Reads .env.gcp / .env (like the other gcp scripts) for GCP_PROJECT_ID /
# GCP_REGION / ENABLE_QDRANT. Fails LOUDLY if any migration errors.
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
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2";     shift 2 ;;
    --help) sed -n '/^# Usage:/,/^# ===/p' "$0"; exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

[[ -n "$PROJECT_ID" ]] || PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$PROJECT_ID" ]] || error "No GCP project set. Use --project or set GCP_PROJECT_ID."
[[ -n "$REGION" ]] || REGION="${GCP_REGION:-asia-south1}"

for cmd in gcloud docker terraform; do
  command -v "$cmd" &>/dev/null || error "$cmd is required but not installed."
done

# ─── Resolve deploy inputs from Terraform outputs ─────────────────────────────
cd "${REPO_ROOT}/infra"
REGISTRY_URL=$(terraform output -raw artifact_registry_url 2>/dev/null) \
  || error "could not read artifact_registry_url from Terraform — has infra been provisioned (terraform apply)?"
DB_CONNECTION=$(terraform output -raw db_instance_connection_name 2>/dev/null) \
  || error "could not read db_instance_connection_name from Terraform."
MIGRATE_SA=$(terraform output -raw proxy_service_account_email 2>/dev/null) \
  || error "could not read proxy_service_account_email from Terraform."
cd "${REPO_ROOT}"

# pgvector.sql ALWAYS runs: it was originally gated on Qdrant being disabled (G07
# pgvector fallback), but the G05 L2 semantic cache stores its embeddings in pgvector
# REGARDLESS of the G07 backend — on a Qdrant-enabled stack the missing extension
# surfaced as `G05 L2 pgvector error: type "vector" does not exist` on every request
# (L2 cache silently dead). CREATE EXTENSION IF NOT EXISTS is idempotent and free.
RUN_PGVECTOR="true"

MIGRATE_IMAGE="${REGISTRY_URL}/schema-migrate:latest"
JOB_NAME="schema-migrate-job"

info "Migration job: project=${PROJECT_ID} region=${REGION}"
info "  db=${DB_CONNECTION}  pgvector=${RUN_PGVECTOR}  image=${MIGRATE_IMAGE}"

# ─── 1. Build + push the tiny psql-capable migration image ────────────────────
# postgres:15-alpine has psql (the proxy-commercial python image does NOT); we bake
# the .sql files + run.sh on top. Built for linux/amd64 (Cloud Run).
info "Building migration image (postgres:15-alpine + infra/migrations/*.sql)..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build --platform=linux/amd64 \
  -f "${REPO_ROOT}/infra/migrations/Dockerfile.migrate" \
  -t "$MIGRATE_IMAGE" \
  "${REPO_ROOT}/infra/migrations" \
  || error "migration image build failed"
docker push "$MIGRATE_IMAGE" || error "migration image push failed"
success "Migration image pushed"

# ─── 2. Deploy + execute the in-VPC Cloud Run Job ─────────────────────────────
# --network/--subnet/--vpc-egress: run inside the VPC so the private-IP Cloud SQL
#   instance is reachable (Direct VPC egress). Same pattern as docs-seed-job.
# --set-cloudsql-instances: mount the Cloud SQL Auth Proxy socket at /cloudsql/<conn>;
#   works for public AND private instances (no private IP address needed).
# --set-secrets PGPASSWORD: the DB password from Secret Manager, never inlined.
# --max-retries=1: a partial failure re-runs cleanly (all SQL is idempotent — IF NOT
#   EXISTS / DROP POLICY IF EXISTS).
info "Deploying migration Cloud Run Job (${JOB_NAME})..."
gcloud run jobs deploy "$JOB_NAME" \
  --image="$MIGRATE_IMAGE" \
  --region="$REGION" --project="$PROJECT_ID" \
  --service-account="$MIGRATE_SA" \
  --network=default --subnet=default --vpc-egress=private-ranges-only \
  --set-cloudsql-instances="$DB_CONNECTION" \
  --set-secrets="PGPASSWORD=token-opt-db-password:latest" \
  --set-env-vars="DB_CONNECTION_NAME=${DB_CONNECTION},RUN_PGVECTOR=${RUN_PGVECTOR}" \
  --max-retries=1 --task-timeout=600 --quiet \
  || error "migration job deploy failed"

info "Executing migration job (waiting for completion)..."
# Execute ASYNC + self-poll, NOT `execute --wait`: the gcloud --wait CLI has repeatedly WEDGED
# (spins "0/1 complete" long after the task finished server-side). Poll succeededCount/
# failedCount ourselves with a hard wall-clock cap so a stuck CLI can never hang the deploy.
EXEC_NAME="$(gcloud run jobs execute "$JOB_NAME" \
  --region="$REGION" --project="$PROJECT_ID" --format="value(metadata.name)" 2>/dev/null || true)"
[[ -n "$EXEC_NAME" ]] || error "could not start migration job execution — check IAM / job deploy."

MIG_OK=false
for _i in $(seq 1 40); do   # 40 × 10s = ~6.5 min cap (task-timeout is 600s)
  ST="$(gcloud run jobs executions describe "$EXEC_NAME" --region="$REGION" --project="$PROJECT_ID" \
          --format="value(status.succeededCount,status.failedCount)" 2>/dev/null || echo "")"
  case "$ST" in
    1*)  MIG_OK=true; break ;;                     # succeededCount>=1
    *1)  break ;;                                  # failedCount>=1 → fail fast
  esac
  sleep 10
done
$MIG_OK || error "MIGRATION JOB FAILED or timed out (execution ${EXEC_NAME}) — inspect logs: gcloud run jobs executions logs read ${EXEC_NAME} --region=${REGION} --project=${PROJECT_ID}"

success "Schema migrations applied via in-VPC Cloud Run Job"
