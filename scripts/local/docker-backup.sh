#!/usr/bin/env bash
# =============================================================================
# docker-backup.sh — Backup local Docker data to GCS
# =============================================================================
# Usage:
#   ./scripts/local/docker-backup.sh [--project ID] [--bucket NAME]
#
# Backs up Redis, PostgreSQL, and Qdrant to GCS for persistence.
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
if [[ -z "$CONFIG_BUCKET" ]]; then
  CONFIG_BUCKET="${CONFIG_GCS_BUCKET:-token-opt-config}"
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
info "Backing up to gs://${CONFIG_BUCKET}/backups/${TIMESTAMP}/..."

# Ensure bucket exists
if [[ -n "$PROJECT_ID" ]]; then
  gsutil mb -p "$PROJECT_ID" "gs://${CONFIG_BUCKET}" 2>/dev/null || true
fi

# Backup Redis
if docker ps --filter "name=token-opt-redis" --format "{{.Names}}" | grep -q redis; then
  info "Backing up Redis..."
  docker exec token-opt-redis redis-cli BGSAVE 2>/dev/null || true
  sleep 2
  docker cp token-opt-redis:/data/dump.rdb "/tmp/redis-${TIMESTAMP}.rdb" 2>/dev/null && \
    gsutil cp "/tmp/redis-${TIMESTAMP}.rdb" "gs://${CONFIG_BUCKET}/backups/" && \
    success "Redis backed up"
else
  warn "Redis container not running, skipping"
fi

# Backup PostgreSQL
if docker ps --filter "name=token-opt-postgres" --format "{{.Names}}" | grep -q postgres; then
  info "Backing up PostgreSQL..."
  docker exec token-opt-postgres pg_dumpall -U token_opt > "/tmp/postgres-${TIMESTAMP}.sql" 2>/dev/null && \
    gsutil cp "/tmp/postgres-${TIMESTAMP}.sql" "gs://${CONFIG_BUCKET}/backups/" && \
    success "PostgreSQL backed up"
else
  warn "PostgreSQL container not running, skipping"
fi

# Backup Qdrant
if docker ps --filter "name=token-opt-qdrant" --format "{{.Names}}" | grep -q qdrant; then
  info "Backing up Qdrant..."
  docker exec token-opt-qdrant tar czf "/tmp/qdrant-${TIMESTAMP}.tar.gz" -C /qdrant/storage . 2>/dev/null && \
    docker cp "token-opt-qdrant:/tmp/qdrant-${TIMESTAMP}.tar.gz" "/tmp/" && \
    gsutil cp "/tmp/qdrant-${TIMESTAMP}.tar.gz" "gs://${CONFIG_BUCKET}/backups/" && \
    success "Qdrant backed up"
else
  warn "Qdrant container not running, skipping"
fi

success "Backups complete: gs://${CONFIG_BUCKET}/backups/"
