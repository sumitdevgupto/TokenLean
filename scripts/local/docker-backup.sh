#!/usr/bin/env bash
# =============================================================================
# docker-backup.sh — Backup local Docker data to GCS
# =============================================================================
# Usage:
#   ./scripts/local/docker-backup.sh [--project ID] [--bucket NAME]
#
# Backs up Redis, PostgreSQL, and Qdrant to gs://BUCKET/backups/<timestamp>/. The bucket is
# --bucket or CONFIG_GCS_BUCKET from .env, and it must be in your project (it is created there
# when missing): the dumps hold every tenant's usage and audit rows, portal password hashes and
# encrypted provider keys, so they never go to a bucket someone else owns.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
CONFIG_BUCKET=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --bucket)  CONFIG_BUCKET="$2"; shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# Load .env file if it exists
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null || echo "")
fi
[[ -n "$PROJECT_ID" ]] || error "No GCP project: pass --project ID or run: gcloud config set project ID"
if [[ -z "$CONFIG_BUCKET" ]]; then
  CONFIG_BUCKET="${CONFIG_GCS_BUCKET:-}"
fi
[[ -n "$CONFIG_BUCKET" ]] \
  || error "No backup bucket: pass --bucket NAME or set CONFIG_GCS_BUCKET in .env (a bucket in project ${PROJECT_ID})"

# Bucket names are global, so the name proves nothing: the bucket must be among this project's.
in_project() {
  gcloud storage buckets list --project="$PROJECT_ID" --format="value(name)" 2>/dev/null \
    | grep -qxF "$CONFIG_BUCKET"
}
if ! in_project; then
  info "Creating gs://${CONFIG_BUCKET} in ${PROJECT_ID}..."
  gcloud storage buckets create "gs://${CONFIG_BUCKET}" --project="$PROJECT_ID" \
    --uniform-bucket-level-access &>/dev/null || true
  in_project || error "gs://${CONFIG_BUCKET} is not in project ${PROJECT_ID} and could not be created there (the name may belong to someone else): refusing to upload backups to it"
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
DEST="gs://${CONFIG_BUCKET}/backups/${TIMESTAMP}/"
# The dumps go to a temp dir only you can read, removed however the script ends.
WORK=$(mktemp -d)
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT
info "Backing up to ${DEST}..."

# Backup Redis
if docker ps --filter "name=token-opt-redis" --format "{{.Names}}" | grep -q redis; then
  info "Backing up Redis..."
  docker exec token-opt-redis redis-cli BGSAVE 2>/dev/null || true
  sleep 2
  docker cp token-opt-redis:/data/dump.rdb "${WORK}/redis-${TIMESTAMP}.rdb" 2>/dev/null && \
    gcloud storage cp "${WORK}/redis-${TIMESTAMP}.rdb" "$DEST" && \
    success "Redis backed up"
else
  warn "Redis container not running, skipping"
fi

# Backup PostgreSQL
if docker ps --filter "name=token-opt-postgres" --format "{{.Names}}" | grep -q postgres; then
  info "Backing up PostgreSQL..."
  docker exec token-opt-postgres pg_dumpall -U token_opt > "${WORK}/postgres-${TIMESTAMP}.sql" 2>/dev/null && \
    gcloud storage cp "${WORK}/postgres-${TIMESTAMP}.sql" "$DEST" && \
    success "PostgreSQL backed up"
else
  warn "PostgreSQL container not running, skipping"
fi

# Backup Qdrant (the archive is streamed out, so nothing is left in the container)
if docker ps --filter "name=token-opt-qdrant" --format "{{.Names}}" | grep -q qdrant; then
  info "Backing up Qdrant..."
  docker exec token-opt-qdrant tar czf - -C /qdrant/storage . > "${WORK}/qdrant-${TIMESTAMP}.tar.gz" 2>/dev/null && \
    gcloud storage cp "${WORK}/qdrant-${TIMESTAMP}.tar.gz" "$DEST" && \
    success "Qdrant backed up"
else
  warn "Qdrant container not running, skipping"
fi

success "Backups complete: ${DEST}"
